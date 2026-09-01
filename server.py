"""Small WebSocket relay for consent-based desktop sessions.

The relay never captures screens or executes commands. It only pairs an Agent
with a Master and forwards already-authenticated session messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

from websockets.server import WebSocketServerProtocol, serve

from .key_store import KeyStore

LOG = logging.getLogger("remote-relay")
MAX_CLIENTS = 32
SCREEN_KEY_TTL_SECONDS = 24 * 60 * 60
PAIR_REQUEST_TTL_SECONDS = 60


def new_code() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))


def sendable(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


@dataclass
class Client:
    websocket: WebSocketServerProtocol
    role: str
    client_id: str
    name: str
    key_hash: str
    key_id: int
    connected_at: float
    screen_key_hash: str | None = None


@dataclass
class PairCode:
    code: str
    agent: Client
    expires_at: float


class Relay:
    def __init__(self, key_store: KeyStore | None = None) -> None:
        self.clients: dict[WebSocketServerProtocol, Client] = {}
        self.codes: dict[str, PairCode] = {}
        self.sessions: dict[str, tuple[Client, Client]] = {}
        self.pending_requests: dict[str, tuple[Client, Client, float]] = {}
        self.lock = asyncio.Lock()
        self.key_store = key_store or KeyStore()

    async def send(self, client: Client, payload: dict[str, Any]) -> None:
        try:
            await client.websocket.send(sendable(payload))
        except Exception:
            LOG.debug("Could not send to %s", client.client_id, exc_info=True)

    async def cleanup(self, websocket: WebSocketServerProtocol) -> None:
        async with self.lock:
            client = self.clients.pop(websocket, None)
            if not client:
                return

            for code, pair in list(self.codes.items()):
                if pair.agent.websocket is websocket:
                    self.codes.pop(code, None)
            for request_id, (master, agent, _expires_at) in list(self.pending_requests.items()):
                if master.websocket is websocket or agent.websocket is websocket:
                    self.pending_requests.pop(request_id, None)

            affected = [
                (session_id, master, agent)
                for session_id, (master, agent) in self.sessions.items()
                if master.websocket is websocket or agent.websocket is websocket
            ]
            for session_id, master, agent in affected:
                self.sessions.pop(session_id, None)
                other = agent if master.websocket is websocket else master
                await self.send(other, {
                    "type": "session_closed",
                    "session_id": session_id,
                    "reason": "peer_disconnected",
                })

        LOG.info("%s disconnected", client.client_id)

    async def register(self, websocket: WebSocketServerProtocol, message: dict[str, Any]) -> Client:
        requested_role = str(message.get("role", "auto"))
        if requested_role not in {"master", "agent", "auto"}:
            raise ValueError("role must be master, agent, or auto")
        client_id = str(message.get("client_id", ""))[:80]
        if not client_id:
            raise ValueError("client_id is required")
        session_key = str(message.get("session_key", ""))
        auth = self.key_store.authenticate(
            session_key,
            None if requested_role == "auto" else requested_role,
        )
        if not auth:
            raise ValueError("invalid or revoked session key")
        role = str(auth["role"])
        if role not in {"master", "agent"}:
            raise ValueError("login key has no valid application role")
        name = str(message.get("name", role.title()))[:80]
        screen_key_hash: str | None = None
        is_probe = bool(message.get("probe"))
        if role == "agent" and not is_probe:
            screen_key = str(message.get("screen_key", ""))
            if len(screen_key) < 20 or len(screen_key) > 200:
                raise ValueError("agent must provide a screen key")
            screen_key_hash = self.key_store._hash(screen_key)
        client = Client(
            websocket,
            role,
            client_id,
            name,
            auth["key_hash"],
            int(auth["id"]),
            time.time(),
            screen_key_hash,
        )
        async with self.lock:
            if len(self.clients) >= MAX_CLIENTS:
                raise ValueError("relay is at capacity")
            self.clients[websocket] = client
            if role == "agent" and screen_key_hash is not None:
                assert screen_key_hash is not None
                for existing in self.clients.values():
                    if (
                        existing.websocket is not websocket
                        and existing.screen_key_hash == screen_key_hash
                    ):
                        raise ValueError("screen key is already in use")
                self.codes[screen_key_hash] = PairCode(
                    code=screen_key_hash,
                    agent=client,
                    expires_at=asyncio.get_running_loop().time() + SCREEN_KEY_TTL_SECONDS,
                )
        await self.send(client, {
            "type": "registered",
            "client_id": client_id,
            "role": role,
            "key_id": int(auth["id"]),
            "key_label": auth["label"],
            "screen_key_active": role == "agent",
        })
        LOG.info("%s connected as %s", client_id, role)
        return client

    async def pair_request(self, master: Client, message: dict[str, Any]) -> None:
        screen_key = str(message.get("screen_key", ""))
        screen_key_hash = self.key_store._hash(screen_key)
        async with self.lock:
            pair = self.codes.get(screen_key_hash)
            if not pair or pair.expires_at < asyncio.get_running_loop().time():
                self.codes.pop(screen_key_hash, None)
                await self.send(master, {
                    "type": "screen_result",
                    "accepted": False,
                    "reason": "screen key is invalid or expired",
                })
                return
            request_id = secrets.token_urlsafe(12)
            self.pending_requests[request_id] = (
                master,
                pair.agent,
                asyncio.get_running_loop().time() + PAIR_REQUEST_TTL_SECONDS,
            )
            request = {
                "type": "screen_request",
                "request_id": request_id,
                "master_id": master.client_id,
                "master_name": master.name,
            }
            await self.send(pair.agent, request)

    async def pair_response(self, agent: Client, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        accepted = bool(message.get("accepted"))
        master: Client | None = None
        async with self.lock:
            pending = self.pending_requests.pop(request_id, None)
            if not pending:
                return
            master, expected_agent, expires_at = pending
            if (
                expected_agent.websocket is not agent.websocket
                or expires_at < asyncio.get_running_loop().time()
            ):
                return
            if not accepted:
                await self.send(master, {
                    "type": "screen_result",
                    "accepted": False,
                    "reason": "remote user declined the connection",
                })
                return
            session_id = secrets.token_urlsafe(18)
            self.sessions[session_id] = (master, agent)
            for code, pair in list(self.codes.items()):
                if pair.agent.websocket is agent.websocket:
                    self.codes.pop(code, None)
        await self.send(master, {
            "type": "screen_result",
            "accepted": True,
            "session_id": session_id,
            "agent_id": agent.client_id,
            "agent_name": agent.name,
        })
        await self.send(agent, {
            "type": "session_started",
            "session_id": session_id,
            "master_id": master.client_id,
            "master_name": master.name,
        })

    async def route_session(self, sender: Client, message: dict[str, Any]) -> None:
        session_id = str(message.get("session_id", ""))
        async with self.lock:
            peers = self.sessions.get(session_id)
            if not peers:
                return
            master, agent = peers
            if sender.websocket is master.websocket:
                recipient = agent
            elif sender.websocket is agent.websocket:
                recipient = master
            else:
                return
        await self.send(recipient, message)
        if message.get("type") == "close_session":
            async with self.lock:
                self.sessions.pop(session_id, None)

    def active_devices(self) -> list[dict[str, Any]]:
        """Return a non-secret snapshot for the owner's Telegram panel."""
        now = time.time()
        devices = []
        for client in list(self.clients.values()):
            session_count = sum(
                1
                for master, agent in self.sessions.values()
                if master.websocket is client.websocket or agent.websocket is client.websocket
            )
            devices.append({
                "client_id": client.client_id,
                "role": client.role,
                "name": client.name,
                "key_id": client.key_id,
                "connected_seconds": max(0, int(now - client.connected_at)),
                "sessions": session_count,
                "screen_hint": (
                    f"…{client.screen_key_hash[-8:]}"
                    if client.screen_key_hash else None
                ),
            })
        return devices

    def active_key_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for client in list(self.clients.values()):
            counts[client.key_id] = counts.get(client.key_id, 0) + 1
        return counts

    async def disconnect_key(self, key_id: int) -> int:
        """Immediately disconnect every device authenticated by a revoked key."""
        targets = [
            client for client in list(self.clients.values())
            if client.key_id == key_id
        ]
        for client in targets:
            try:
                await client.websocket.close(code=4003, reason="login key revoked")
            except Exception:
                LOG.debug("Could not close revoked client %s", client.client_id, exc_info=True)
        return len(targets)

    async def handle(self, websocket: WebSocketServerProtocol) -> None:
        client: Client | None = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            message = json.loads(raw)
            if message.get("type") != "hello":
                raise ValueError("first message must be hello")
            client = await self.register(websocket, message)
            async for raw in websocket:
                if not isinstance(raw, str):
                    continue
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "screen_request" and client.role == "master":
                    await self.pair_request(client, message)
                elif kind == "pair_response" and client.role == "agent":
                    await self.pair_response(client, message)
                elif kind in {"session", "close_session"}:
                    await self.route_session(client, message)
        except (ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Rejected client: %s", exc)
            await self.send(client, {"type": "error", "message": str(exc)}) if client else None
        except Exception:
            LOG.info("Client connection ended", exc_info=True)
        finally:
            await self.cleanup(websocket)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    relay = Relay(KeyStore())
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    async with serve(relay.handle, host, port, max_size=8 * 1024 * 1024, ping_interval=20):
        LOG.info("Remote Control relay listening on %s:%s", host, port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())