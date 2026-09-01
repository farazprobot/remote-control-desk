# Cleanup manifest

## Included because the runtime uses them

- `main.py`, `Dockerfile`, `requirements.txt`
- `pyproject.toml`, `uv.lock`
- `relay/`
- `control_bot/`
- `desktop/`
- `installer/RemoteControl.iss`
- `build_windows.ps1`
- `build_windows.bat`
- `README.md`, `replit.md`

## Excluded from the clean package

- Empty `venv.zip` and `pythonlibs.zip`
- Replit TypeScript scaffolding: `package.json`, `pnpm-lock.yaml`,
  `pnpm-workspace.yaml`, `tsconfig*.json`, `lib/`, and `scripts/`
- Generated `dist/` archives; they are reproducible from the included source
- Runtime database state; keep an existing `data/session_keys.sqlite3` as a
  separate backup when migrating
- The older standalone Telegram bot and the separate `bot/` Credit Retriever
  application; they are not imported by this runtime
- Nested upload dumps and duplicate screenshots

The original uploads remain untouched in `attached_assets/`. This manifest
describes the clean package only; it is not a destructive deletion of the
source backups.

## State migration note

The uploaded session database contains four rows and uses the older `legacy`
role default. The current relay accepts only role-bound `master` and `agent`
keys, so keep the database as a backup but issue fresh keys with
`/newmaster` and `/newagent` after migration unless the existing rows have been
updated deliberately.