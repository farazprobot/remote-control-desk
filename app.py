"""Windows desktop UI for the Remote Control Desk MVP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import queue
import secrets
import socket
import string
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Any, Callable

try:
    from PIL import Image, ImageGrab, ImageTk
    if sys.platform == "win32":
        import pyautogui
        import pyperclip
    else:
        pyautogui = None
        pyperclip = None
except ImportError as exc:
    raise SystemExit(
        "Desktop dependencies are missing. Run: python -m pip install -r desktop/requirements.txt"
    ) from exc

import websockets


def make_client_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def make_screen_key() -> str:
    return "SCR-" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")


def valid_relay_url(value: str) -> bool:
    return value.startswith(("ws://", "wss://")) and len(value) > 5


class NetworkClient:
    """Runs the WebSocket connection away from Tk's main thread."""

    def __init__(self, relay_url: str, hello: dict[str, Any], on_message: Callable[[dict[str, Any]], None],
                 on_status: Callable[[str], None]) -> None:
        self.relay_url = relay_url
        self.hello = hello
        self.on_message = on_message
        self.on_status = on_status
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[str] | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

    def _thread_main(self) -> None:
        asyncio.run(self._main())

    async def _sender(self, websocket: websockets.WebSocketClientProtocol) -> None:
        assert self.queue is not None
        while not self.stopping.is_set():
            await websocket.send(await self.queue.get())

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        try:
            self.on_status("Connecting to relay…")
            async with websockets.connect(
                self.relay_url,
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                open_timeout=15,
            ) as websocket:
                await websocket.send(json.dumps(self.hello, separators=(",", ":")))
                self.on_status("Connected to relay")
                sender = asyncio.create_task(self._sender(websocket))
                try:
                    async for raw in websocket:
                        if isinstance(raw, str):
                            self.on_message(json.loads(raw))
                finally:
                    sender.cancel()
        except Exception as exc:
            self.on_status(f"Relay connection stopped: {exc}")

    def send(self, payload: dict[str, Any]) -> None:
        if not self.loop or not self.queue:
            return
        raw = json.dumps(payload, separators=(",", ":"))
        self.loop.call_soon_threadsafe(self.queue.put_nowait, raw)

    def stop(self) -> None:
        self.stopping.set()


KEY_MAP = {
    "Return": "enter", "Escape": "esc", "BackSpace": "backspace",
    "Tab": "tab", "space": "space", "Delete": "delete", "Insert": "insert",
    "Home": "home", "End": "end", "Prior": "pageup", "Next": "pagedown",
    "Left": "left", "Right": "right", "Up": "up", "Down": "down",
    "Shift_L": "shift", "Shift_R": "shift", "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt", "Win_L": "win", "Win_R": "win",
}


def key_name(keysym: str) -> str | None:
    if keysym in KEY_MAP:
        return KEY_MAP[keysym]
    if len(keysym) == 1 and keysym.isprintable():
        return keysym.lower()
    if len(keysym) >= 2 and keysym[0] == "F" and keysym[1:].isdigit():
        return keysym.lower()
    return None


def make_dpi_aware() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


class RoleProbe:
    """Validates a key once and asks the relay which UI should open."""

    def __init__(
        self,
        relay_url: str,
        session_key: str,
        on_role: Callable[[str], None],
        on_error: Callable[[str], None],
    ) -> None:
        self.relay_url = relay_url
        self.session_key = session_key
        self.on_role = on_role
        self.on_error = on_error
        self.stopping = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._thread_main, daemon=True).start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            if not self.stopping.is_set():
                self.on_error(str(exc))

    async def _main(self) -> None:
        async with websockets.connect(
            self.relay_url,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            open_timeout=15,
        ) as websocket:
            await websocket.send(json.dumps({
                "type": "hello",
                "role": "auto",
                "probe": True,
                "client_id": make_client_id("login"),
                "name": "Remote Control Desk Login",
                "session_key": self.session_key,
            }, separators=(",", ":")))
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            message = json.loads(raw)
            if message.get("type") != "registered":
                raise RuntimeError("The relay did not accept this login key.")
            role = str(message.get("role", ""))
            if role not in {"master", "agent"}:
                raise RuntimeError("The relay returned an unknown account role.")
            if not self.stopping.is_set():
                self.on_role(role)

    def stop(self) -> None:
        self.stopping.set()


class LoginApp:
    """Single startup UI that discovers Master vs Remote Agent automatically."""

    def __init__(self, root: tk.Tk, relay_url: str, session_key: str = "") -> None:
        self.root = root
        self.root.title("Remote Control Desk — Sign in")
        self.root.geometry("540x430")
        self.root.minsize(500, 390)
        self.relay_url = relay_url
        self.session_key = session_key.strip()
        self.relay_url_var = tk.StringVar(value=relay_url)
        self.session_key_var = tk.StringVar(value=self.session_key)
        self.status_var = tk.StringVar(value="Enter the login key issued by the owner.")
        self.login_button: ttk.Button | None = None
        self.probe: RoleProbe | None = None
        self.closed = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.session_key:
            self.root.after(250, self.login)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=34)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Remote Control Desk",
            font=("Segoe UI", 24, "bold"),
        ).pack(pady=(20, 4))
        ttk.Label(
            frame,
            text="Secure sign in",
            foreground="#245",
            font=("Segoe UI", 11),
        ).pack()
        ttk.Label(
            frame,
            text="Use one owner-issued key. The app automatically opens the correct Master or Remote Agent panel.",
            wraplength=430,
            justify="center",
        ).pack(pady=(14, 22))

        fields = ttk.LabelFrame(frame, text="Connection", padding=16)
        fields.pack(fill="x")
        ttk.Label(fields, text="Relay server URL").pack(anchor="w")
        ttk.Entry(fields, textvariable=self.relay_url_var).pack(fill="x", pady=(4, 12))
        ttk.Label(fields, text="Login key").pack(anchor="w")
        ttk.Entry(fields, textvariable=self.session_key_var, show="*").pack(fill="x", pady=(4, 0))

        self.login_button = ttk.Button(frame, text="Sign in", command=self.login)
        self.login_button.pack(pady=(18, 10), ipadx=18)
        ttk.Label(frame, textvariable=self.status_var, foreground="#555").pack()
        ttk.Label(
            frame,
            text="Developed by @Farazumar",
            foreground="#666",
        ).pack(pady=(28, 0))

    def login(self) -> None:
        if self.probe:
            return
        relay_url = self.relay_url_var.get().strip()
        session_key = self.session_key_var.get().strip()
        if not valid_relay_url(relay_url):
            messagebox.showwarning(
                "Relay URL required",
                "Enter a valid relay URL beginning with ws:// or wss://.",
                parent=self.root,
            )
            return
        if not session_key:
            messagebox.showwarning(
                "Login key required",
                "Enter the key issued by the owner Telegram bot.",
                parent=self.root,
            )
            return
        self.relay_url = relay_url
        self.session_key = session_key
        self.status_var.set("Verifying login key…")
        if self.login_button:
            self.login_button.configure(state="disabled")
        self.probe = RoleProbe(
            relay_url,
            session_key,
            lambda role: self.root.after(0, lambda: self._open_role(role)),
            lambda error: self.root.after(0, lambda: self._login_error(error)),
        )
        self.probe.start()

    def _login_error(self, error: str) -> None:
        self.probe = None
        self.status_var.set("Login failed. Check the relay URL and key.")
        if self.login_button:
            self.login_button.configure(state="normal")
        messagebox.showerror("Login failed", error or "The login key was rejected.", parent=self.root)

    def _open_role(self, role: str) -> None:
        if self.closed:
            return
        self.probe = None
        for child in self.root.winfo_children():
            child.destroy()
        if role == "agent":
            AgentApp(self.root, self.relay_url, self.session_key)
        else:
            MasterApp(self.root, self.relay_url, self.session_key)

    def close(self) -> None:
        self.closed = True
        if self.probe:
            self.probe.stop()
        self.root.destroy()


class AgentApp:
    def __init__(self, root: tk.Tk, relay_url: str, session_key: str = "") -> None:
        self.root = root
        self.root.title("Remote Control Desk — Remote Agent")
        self.root.geometry("560x410")
        self.root.minsize(500, 360)
        self.relay_url = relay_url
        self.relay_url_var = tk.StringVar(value=self.relay_url)
        self.session_key = session_key.strip()
        self.client_id = make_client_id("agent")
        self.screen_key = make_screen_key()
        self.session_id: str | None = None
        self.network: NetworkClient | None = None
        self.running = True
        self.last_clipboard = ""
        self.status_var = tk.StringVar(value="Starting…")
        self.screen_key_var = tk.StringVar(value=self.screen_key)
        self.session_key_var = tk.StringVar(value=self.session_key)
        self._build_ui()
        if self.session_key:
            self._connect()
        else:
            self.status_var.set("Enter the session key from the Telegram bot.")
        self.root.after(700, self._poll_clipboard)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=24)
        self.panel_frame = outer
        ttk.Label(outer, text="Remote Agent", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Developed by @Farazumar", foreground="#666").pack(anchor="w", pady=(2, 0))
        ttk.Label(
            outer,
            text="Keep this window visible. Share the Screen Key only with the owner. You approve every connection.",
            wraplength=490,
        ).pack(anchor="w", pady=(6, 18))
        auth = ttk.LabelFrame(outer, text="Owner session key", padding=10)
        auth.pack(fill="x", pady=(0, 12))
        ttk.Entry(auth, textvariable=self.session_key_var, show="*", width=35).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(auth, text="Connect", command=self.connect_session).pack(side="left", padx=(8, 0))
        card = ttk.LabelFrame(outer, text="Screen Key for the Master", padding=18)
        card.pack(fill="x")
        ttk.Entry(
            card,
            textvariable=self.screen_key_var,
            state="readonly",
            justify="center",
            font=("Consolas", 15),
        ).pack(fill="x")
        ttk.Button(card, text="Generate new Screen Key", command=self.new_screen_key).pack(pady=(12, 0))
        self.session_label = ttk.Label(outer, text="No Master connected.", foreground="#555")
        self.session_label.pack(anchor="w", pady=(22, 4))
        ttk.Label(outer, textvariable=self.status_var, foreground="#245").pack(anchor="w")
        ttk.Label(
            outer,
            text="Screen, mouse, keyboard, and clipboard are shared only after you approve a pairing request.",
            wraplength=490,
            foreground="#555",
        ).pack(anchor="w", pady=(18, 0))
        self.login_frame = ttk.Frame(self.root, padding=30)
        ttk.Label(
            self.login_frame,
            text="Remote Agent Login",
            font=("Segoe UI", 22, "bold"),
        ).pack(pady=(35, 8))
        ttk.Label(
            self.login_frame,
            text="Enter the Remote Agent Login Key issued privately by the owner Telegram bot.",
            wraplength=420,
            justify="center",
        ).pack(pady=(0, 14))
        ttk.Label(self.login_frame, text="Relay server URL").pack(anchor="w", padx=58)
        ttk.Entry(self.login_frame, textvariable=self.relay_url_var, width=42).pack(pady=(3, 10))
        ttk.Label(
            self.login_frame,
            text="Use wss:// for a public relay.",
            foreground="#666",
        ).pack(pady=(0, 8))
        ttk.Entry(self.login_frame, textvariable=self.session_key_var, show="*", width=42).pack()
        ttk.Button(self.login_frame, text="Login", command=self.connect_session).pack(pady=12)
        ttk.Label(self.login_frame, text="Developed by @Farazumar", foreground="#666").pack(pady=(20, 0))
        outer.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def _open_panel(self) -> None:
        self.login_frame.pack_forget()
        self.panel_frame.pack(fill="both", expand=True)
        self.status_var.set("Remote Agent panel ready.")

    def _connect(self) -> None:
        if not self.session_key:
            return
        self.network = NetworkClient(
            self.relay_url,
            {
                "type": "hello",
                "role": "agent",
                "client_id": self.client_id,
                "name": socket.gethostname(),
                "screen_key": self.screen_key,
                "session_key": self.session_key,
            },
            self._message,
            self._status,
        )
        self.network.start()

    def connect_session(self) -> None:
        relay_url = self.relay_url_var.get().strip()
        if not valid_relay_url(relay_url):
            messagebox.showwarning(
                "Relay URL required",
                "Enter a valid relay URL beginning with ws:// or wss://.",
            )
            return
        key = self.session_key_var.get().strip()
        if not key:
            messagebox.showwarning("Session key required", "Enter the key issued by the Telegram bot.")
            return
        self.relay_url = relay_url
        self.session_key = key
        if self.network:
            self.network.stop()
        self.status_var.set("Connecting with session key…")
        self._connect()

    def _status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def _message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "registered":
            self.root.after(0, self._open_panel)
        elif kind == "screen_request":
            self.root.after(0, lambda: self._ask_pair(message))
        elif kind == "session_started":
            self.session_id = str(message["session_id"])
            self.root.after(0, lambda: self.session_label.configure(
                text=f"Connected to {message.get('master_name', 'Master')}. You can disconnect at any time.",
                foreground="#176b3a",
            ))
        elif kind in {"session", "close_session"}:
            self._handle_session(message.get("payload") or {})
        elif kind == "session_closed":
            self.session_id = None
            self.root.after(0, lambda: self.session_label.configure(
                text="Master disconnected.", foreground="#555"
            ))

    def _ask_pair(self, message: dict[str, Any]) -> None:
        accepted = messagebox.askyesno(
            "Approve remote connection?",
            f"{message.get('master_name', 'A Master')} wants to connect.\n\n"
            "If you approve, they can view this screen, control mouse/keyboard, "
            "and sync clipboard until disconnected.",
            parent=self.root,
        )
        if self.network:
            self.network.send({
                "type": "pair_response",
                "request_id": message.get("request_id"),
                "master_id": message.get("master_id"),
                "accepted": accepted,
            })
        if not accepted:
            self.status_var.set("Connection declined.")

    def new_screen_key(self) -> None:
        self.screen_key = make_screen_key()
        self.screen_key_var.set(self.screen_key)
        if self.network:
            self.network.stop()
        if self.session_key:
            self._connect()
        self.status_var.set("New Screen Key generated.")

    # Compatibility for older local launchers.
    new_code = new_screen_key

    def _handle_session(self, payload: dict[str, Any]) -> None:
        if payload.get("action") == "mouse_move":
            self._move_mouse(payload)
        elif payload.get("action") == "mouse_button":
            self._mouse_button(payload)
        elif payload.get("action") == "key":
            self._key(payload)
        elif payload.get("action") == "clipboard_set":
            self._set_clipboard(str(payload.get("text", "")))
        elif payload.get("action") == "request_frame":
            self.send_frame()
        elif payload.get("action") == "close":
            self.session_id = None
            self.root.after(0, lambda: self.session_label.configure(
                text="Master disconnected.", foreground="#555"
            ))

    def _move_mouse(self, payload: dict[str, Any]) -> None:
        try:
            pyautogui.moveTo(int(payload["x"]), int(payload["y"]), duration=0)
        except Exception:
            pass

    def _mouse_button(self, payload: dict[str, Any]) -> None:
        try:
            button = str(payload.get("button", "left"))
            if payload.get("pressed"):
                pyautogui.mouseDown(button=button)
            else:
                pyautogui.mouseUp(button=button)
        except Exception:
            pass

    def _key(self, payload: dict[str, Any]) -> None:
        name = key_name(str(payload.get("keysym", "")))
        if not name:
            return
        try:
            if payload.get("pressed"):
                pyautogui.keyDown(name)
            else:
                pyautogui.keyUp(name)
        except Exception:
            pass

    def _set_clipboard(self, text: str) -> None:
        try:
            pyperclip.copy(text)
            self.last_clipboard = text
        except Exception:
            pass

    def _poll_clipboard(self) -> None:
        if self.running:
            try:
                text = pyperclip.paste()
                if text != self.last_clipboard:
                    self.last_clipboard = text
                    if self.session_id and self.network:
                        self.network.send(self._session({"action": "clipboard", "text": text}))
            except Exception:
                pass
            self.root.after(700, self._poll_clipboard)

    def send_frame(self) -> None:
        if not self.session_id or not self.network:
            return
        try:
            image = ImageGrab.grab(all_screens=True)
            source_w, source_h = image.size
            left, top = 0, 0
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                left = user32.GetSystemMetrics(76)
                top = user32.GetSystemMetrics(77)
            preview = image.convert("RGB")
            preview.thumbnail((1800, 1100), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=58, optimize=True)
            self.network.send(self._session({
                "action": "frame",
                "jpeg": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "source_width": source_w,
                "source_height": source_h,
                "screen_left": left,
                "screen_top": top,
            }))
        except Exception as exc:
            self._status(f"Screen capture error: {exc}")

    def _session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"type": "session", "session_id": self.session_id, "payload": payload}

    def close(self) -> None:
        self.running = False
        if self.network:
            self.network.stop()
        self.root.destroy()


@dataclass
class Device:
    session_id: str
    name: str
    status: str = "Connected"
    source_width: int = 1
    source_height: int = 1
    screen_left: int = 0
    screen_top: int = 0


class MasterApp:
    MAX_DEVICES = 10

    def __init__(self, root: tk.Tk, relay_url: str, session_key: str = "") -> None:
        self.root = root
        self.root.title("Remote Control Desk — Master")
        self.root.geometry("1100x760")
        self.root.minsize(850, 600)
        self.relay_url = relay_url
        self.relay_url_var = tk.StringVar(value=self.relay_url)
        self.session_key = session_key.strip()
        self.client_id = make_client_id("master")
        self.network: NetworkClient | None = None
        self.devices: dict[str, Device] = {}
        self.selected_session: str | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.frame_image: Image.Image | None = None
        self.image_bounds = (0, 0, 1, 1)
        self.running = True
        self.last_clipboard = ""
        self.status_var = tk.StringVar(value="Starting…")
        self.screen_key_var = tk.StringVar()
        self.session_key_var = tk.StringVar(value=self.session_key)
        self.clipboard_var = tk.BooleanVar(value=True)
        self._build_ui()
        if self.session_key:
            self._connect()
        else:
            self.status_var.set("Enter the session key from the Telegram bot.")
        self.root.after(700, self._poll_clipboard)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, padding=14)
        self.panel_frame = root_frame
        header = ttk.Frame(root_frame)
        header.pack(fill="x")
        ttk.Label(header, text="Remote Control Desk", font=("Segoe UI", 20, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status_var, foreground="#245").pack(side="right", pady=5)
        ttk.Label(
            root_frame,
            text="Developed by @Farazumar",
            foreground="#666",
        ).pack(anchor="w", pady=(2, 0))
        controls = ttk.LabelFrame(root_frame, text="Connect a remote PC", padding=10)
        controls.pack(fill="x", pady=(12, 10))
        ttk.Label(controls, text="Session key:").pack(side="left")
        ttk.Entry(controls, textvariable=self.session_key_var, show="*", width=20).pack(
            side="left", padx=(8, 6)
        )
        ttk.Button(controls, text="Authorize", command=self.connect_session).pack(side="left", padx=(0, 14))
        ttk.Label(controls, text="Screen Key:").pack(side="left")
        entry = ttk.Entry(controls, textvariable=self.screen_key_var, width=28, font=("Consolas", 11))
        entry.pack(side="left", padx=(8, 8))
        entry.bind("<Return>", lambda _event: self.connect_code())
        ttk.Button(controls, text="Connect", command=self.connect_code).pack(side="left")
        ttk.Checkbutton(
            controls, text="Sync clipboard", variable=self.clipboard_var
        ).pack(side="right")
        body = ttk.Panedwindow(root_frame, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 10, 0))
        right = ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)
        ttk.Label(left, text="Remote PCs (up to 10)", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.device_list = tk.Listbox(left, width=28, height=25, activestyle="none")
        self.device_list.pack(fill="y", expand=True, pady=(8, 8))
        self.device_list.bind("<<ListboxSelect>>", self._select_device)
        ttk.Button(left, text="Disconnect selected", command=self.disconnect_selected).pack(fill="x")
        self.canvas = tk.Canvas(right, background="#111", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        for sequence, handler in [
            ("<ButtonPress-1>", lambda e: self._mouse_button(e, "left", True)),
            ("<ButtonRelease-1>", lambda e: self._mouse_button(e, "left", False)),
            ("<ButtonPress-2>", lambda e: self._mouse_button(e, "middle", True)),
            ("<ButtonRelease-2>", lambda e: self._mouse_button(e, "middle", False)),
            ("<ButtonPress-3>", lambda e: self._mouse_button(e, "right", True)),
            ("<ButtonRelease-3>", lambda e: self._mouse_button(e, "right", False)),
            ("<Motion>", self._mouse_move),
            ("<KeyPress>", lambda e: self._key(e, True)),
            ("<KeyRelease>", lambda e: self._key(e, False)),
        ]:
            self.canvas.bind(sequence, handler)
        self.canvas.bind("<Configure>", lambda _e: self._render())
        ttk.Label(
            right,
            text="Click the screen to focus it. The view automatically fits the window while clicks map to the remote PC's native resolution.",
            foreground="#555",
        ).pack(anchor="w", pady=(8, 0))
        self.login_frame = ttk.Frame(self.root, padding=30)
        ttk.Label(
            self.login_frame,
            text="Master Login",
            font=("Segoe UI", 22, "bold"),
        ).pack(pady=(45, 8))
        ttk.Label(
            self.login_frame,
            text="Enter the Master Login Key issued privately by the owner Telegram bot.",
            wraplength=440,
            justify="center",
        ).pack(pady=(0, 14))
        ttk.Label(self.login_frame, text="Relay server URL").pack(anchor="w", padx=70)
        ttk.Entry(self.login_frame, textvariable=self.relay_url_var, width=42).pack(pady=(3, 10))
        ttk.Label(
            self.login_frame,
            text="Use wss:// for a public relay.",
            foreground="#666",
        ).pack(pady=(0, 8))
        ttk.Entry(self.login_frame, textvariable=self.session_key_var, show="*", width=42).pack()
        ttk.Button(self.login_frame, text="Login", command=self.connect_session).pack(pady=12)
        ttk.Label(self.login_frame, text="Developed by @Farazumar", foreground="#666").pack(pady=(20, 0))
        root_frame.pack_forget()
        self.login_frame.pack(fill="both", expand=True)

    def _open_panel(self) -> None:
        self.login_frame.pack_forget()
        self.panel_frame.pack(fill="both", expand=True)
        self.status_var.set("Master panel ready. Enter a Remote Agent Screen Key.")

    def _connect(self) -> None:
        if not self.session_key:
            return
        self.network = NetworkClient(
            self.relay_url,
            {
                "type": "hello",
                "role": "master",
                "client_id": self.client_id,
                "name": socket.gethostname(),
                "session_key": self.session_key,
            },
            self._message,
            self._status,
        )
        self.network.start()

    def connect_session(self) -> None:
        relay_url = self.relay_url_var.get().strip()
        if not valid_relay_url(relay_url):
            messagebox.showwarning(
                "Relay URL required",
                "Enter a valid relay URL beginning with ws:// or wss://.",
            )
            return
        key = self.session_key_var.get().strip()
        if not key:
            messagebox.showwarning("Session key required", "Enter the key issued by the Telegram bot.")
            return
        self.relay_url = relay_url
        self.session_key = key
        if self.network:
            self.network.stop()
        self.status_var.set("Connecting with session key…")
        self._connect()

    def _status(self, text: str) -> None:
        self.root.after(0, lambda: self.status_var.set(text))

    def connect_code(self) -> None:
        screen_key = self.screen_key_var.get().strip()
        if len(screen_key) < 20:
            messagebox.showwarning("Invalid Screen Key", "Enter the Screen Key shown in the Remote Agent panel.")
            return
        if not self.network:
            messagebox.showwarning("Not authorized", "Enter the session key and click Authorize first.")
            return
        if len(self.devices) >= self.MAX_DEVICES:
            messagebox.showwarning("Limit reached", "A Master can manage up to 10 remote PCs.")
            return
        if self.network:
            self.network.send({"type": "screen_request", "screen_key": screen_key})
        self.status_var.set("Waiting for approval on the remote PC…")
        self.screen_key_var.set("")

    def _message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "registered":
            self.root.after(0, self._open_panel)
        elif kind in {"screen_result", "pair_result"}:
            self.root.after(0, lambda: self._pair_result(message))
        elif kind == "session":
            self.root.after(0, lambda: self._handle_session(message))
        elif kind == "session_closed":
            self.root.after(0, lambda: self._session_closed(str(message.get("session_id", ""))))

    def _pair_result(self, message: dict[str, Any]) -> None:
        if not message.get("accepted"):
            self.status_var.set(f"Connection declined: {message.get('reason', 'unknown reason')}")
            return
        session_id = str(message["session_id"])
        device = Device(session_id, str(message.get("agent_name", "Remote PC")))
        self.devices[session_id] = device
        self.device_list.insert("end", f"● {device.name}")
        self.device_list.selection_clear(0, "end")
        self.device_list.selection_set("end")
        self.selected_session = session_id
        self.status_var.set(f"Connected to {device.name}")
        self._send({"action": "request_frame"})

    def _session_closed(self, session_id: str) -> None:
        if session_id in self.devices:
            index = list(self.devices).index(session_id)
            self.devices.pop(session_id)
            self.device_list.delete(index)
            if self.selected_session == session_id:
                self.selected_session = next(iter(self.devices), None)
                if self.selected_session:
                    self.device_list.selection_set(list(self.devices).index(self.selected_session))
                else:
                    self.canvas.delete("all")
            self.status_var.set("Remote PC disconnected.")

    def _select_device(self, _event: Any = None) -> None:
        selection = self.device_list.curselection()
        if not selection:
            return
        self.selected_session = list(self.devices)[selection[0]]
        self.frame_image = None
        self.canvas.delete("all")
        self._send({"action": "request_frame"})

    def disconnect_selected(self) -> None:
        if self.selected_session:
            if self.network:
                self.network.send({
                    "type": "close_session",
                    "session_id": self.selected_session,
                    "payload": {"action": "close"},
                })

    def _send(self, payload: dict[str, Any]) -> None:
        if self.selected_session and self.network:
            self.network.send({"type": "session", "session_id": self.selected_session, "payload": payload})

    def _handle_session(self, message: dict[str, Any]) -> None:
        if message.get("session_id") != self.selected_session:
            return
        payload = message.get("payload") or {}
        if payload.get("action") == "frame":
            try:
                raw = base64.b64decode(payload["jpeg"])
                self.frame_image = Image.open(io.BytesIO(raw)).convert("RGB")
                device = self.devices.get(self.selected_session or "")
                if device:
                    device.source_width = int(payload["source_width"])
                    device.source_height = int(payload["source_height"])
                    device.screen_left = int(payload.get("screen_left", 0))
                    device.screen_top = int(payload.get("screen_top", 0))
                self._render()
                self._send({"action": "request_frame"})
            except Exception as exc:
                self.status_var.set(f"Frame error: {exc}")
        elif payload.get("action") == "clipboard":
            if self.clipboard_var.get():
                text = str(payload.get("text", ""))
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(text)
                    self.last_clipboard = text
                    self.status_var.set("Clipboard received from remote PC.")
                except tk.TclError:
                    pass

    def _render(self) -> None:
        if not self.frame_image:
            return
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        image = self.frame_image.copy()
        image.thumbnail((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        x = (width - image.width) // 2
        y = (height - image.height) // 2
        self.image_bounds = (x, y, image.width, image.height)
        self.canvas.delete("screen")
        self.canvas.create_image(x, y, anchor="nw", image=self.photo, tags="screen")

    def _remote_point(self, event: tk.Event) -> tuple[int, int] | None:
        device = self.devices.get(self.selected_session or "")
        x0, y0, w, h = self.image_bounds
        if not device or not w or not h or not (x0 <= event.x <= x0 + w and y0 <= event.y <= y0 + h):
            return None
        x = device.screen_left + round((event.x - x0) * device.source_width / w)
        y = device.screen_top + round((event.y - y0) * device.source_height / h)
        return x, y

    def _mouse_move(self, event: tk.Event) -> None:
        point = self._remote_point(event)
        if point:
            self._send({"action": "mouse_move", "x": point[0], "y": point[1]})

    def _mouse_button(self, event: tk.Event, button: str, pressed: bool) -> None:
        self.canvas.focus_set()
        point = self._remote_point(event)
        if point:
            self._send({"action": "mouse_move", "x": point[0], "y": point[1]})
            self._send({"action": "mouse_button", "button": button, "pressed": pressed})

    def _key(self, event: tk.Event, pressed: bool) -> None:
        if self.selected_session and self.canvas.focus_get() == self.canvas:
            if key_name(event.keysym):
                self._send({"action": "key", "keysym": event.keysym, "pressed": pressed})

    def _poll_clipboard(self) -> None:
        if self.running:
            if self.clipboard_var.get() and self.selected_session:
                try:
                    text = self.root.clipboard_get()
                    if text != self.last_clipboard:
                        self.last_clipboard = text
                        self._send({"action": "clipboard_set", "text": text})
                except tk.TclError:
                    pass
            self.root.after(700, self._poll_clipboard)

    def close(self) -> None:
        self.running = False
        for session_id in list(self.devices):
            if self.network:
                self.network.send({
                    "type": "close_session",
                    "session_id": session_id,
                    "payload": {"action": "close"},
                })
        if self.network:
            self.network.stop()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Remote Control Desk")
    parser.add_argument(
        "--mode",
        choices=["auto", "master", "agent"],
        default="auto",
        help="Use auto to detect the panel from the login key.",
    )
    parser.add_argument("--relay", default=os.environ.get("RELAY_URL", "ws://127.0.0.1:8765"))
    parser.add_argument("--session-key", default=os.environ.get("SESSION_KEY", ""))
    args = parser.parse_args()
    make_dpi_aware()
    root = tk.Tk()
    if args.mode == "auto":
        LoginApp(root, args.relay, args.session_key)
    elif args.mode == "agent":
        AgentApp(root, args.relay, args.session_key)
    else:
        MasterApp(root, args.relay, args.session_key)
    root.mainloop()


if __name__ == "__main__":
    main()