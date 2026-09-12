#!/usr/bin/env python
"""Run the dashboard with reload, from anywhere.

    .venv/Scripts/python.exe scripts/dev.py
    .venv/Scripts/python.exe scripts/dev.py --port 8001 --no-reload

Equivalent to:

    .venv/Scripts/python.exe -m uvicorn recall_relay.web.app:app --port 8000 --app-dir app
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Recall Relay dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-reload", action="store_true", help="disable the reloader")
    args = parser.parse_args(argv)

    import uvicorn  # imported here so --help works without the server installed

    from recall_relay.core.config import settings

    print(f"Recall Relay · db {settings.db_path}")
    print(f"http://{args.host}:{args.port}/  (ledger · run · cases · packet · inbox)")
    uvicorn.run(
        "recall_relay.web.app:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=[str(APP_DIR / "recall_relay")],
        app_dir=str(APP_DIR),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
