"""The dashboard. FastAPI owns the SQLite register (DECISIONS #5): the agents call in, never the reverse.

    .venv/Scripts/python.exe -m uvicorn recall_relay.web.app:app --port 8000 --app-dir app

Server-rendered Jinja, one small vanilla JS file, no build step and no CSS framework. The design is
DESIGN.md, to the token.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.config import settings
from ..core.store import Store
from .deps import STATIC_DIR, render
from .routes import api, pages, respond


def _seed_if_empty(store: Store) -> None:
    """A fresh disk (Render's is ephemeral) boots with the demo ledger already in place.

    Only the ledger is seeded, and only when there are no receipts at all, so a store handed in by a test
    or a real food bank's imported CSVs are never touched.
    """
    try:
        if store.list_receipts():
            return
        from ..core.seed import load_seed

        load_seed(store)
    except Exception:  # pragma: no cover - a missing seed module must not stop the dashboard from booting
        return


def create_app(store: Optional[Store] = None, *, db_path: Optional[Path | str] = None) -> FastAPI:
    """Build the app. Pass a Store (tests) or a db_path; otherwise settings.db_path is used."""
    application = FastAPI(
        title="Recall Relay",
        description="Every recall, every pantry, with proof.",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.store = store if store is not None else Store(db_path or settings.db_path)
    _seed_if_empty(application.state.store)

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(api.router)
    application.include_router(respond.router)
    application.include_router(pages.router)  # last: its "/" and "/cases/{id}" are the catch-alls

    @application.exception_handler(404)
    async def _not_found(request: Request, exc) -> HTMLResponse | JSONResponse:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return render(
            request,
            "message.html",
            status_code=404,
            eyebrow="404",
            headline="Nothing at that address",
            message="The binder has five tabs: the receiving ledger, the daily scan, the case register, "
                    "the audit packet, and the agency inbox.",
            links=[{"href": "/ledger", "label": "Receiving ledger"}, {"href": "/cases", "label": "Case register"}],
        )

    return application


app = create_app()

__all__ = ["app", "create_app"]
