"""Shared plumbing for the routes: the Store handle, the Jinja environment, and the two response shapes
(an HTML sheet for a browser, JSON for `fetch`) that every control has to speak.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..core.config import settings
from ..core.store import Store
from . import views
from .security import LimitExceeded, budget, scan_flight

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["stamp"] = views.fmt_stamp
templates.env.filters["day"] = views.fmt_date
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


def get_store(request: Request) -> Store:
    return request.app.state.store


def base_context(request: Request, *, tab: str = "") -> dict:
    store = get_store(request)
    used, limit = budget.state()
    return {
        "request": request,
        "tab": tab,
        "food_bank": settings.food_bank_name,
        "clock_offset": views.clock_offset_hours(store),
        "budget_used": used,
        "budget_limit": limit,
        "active_job": scan_flight.active or "",
    }


def render(request: Request, template: str, *, tab: str = "", status_code: int = 200, **context: Any) -> HTMLResponse:
    ctx = base_context(request, tab=tab)
    ctx.update(context)
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def wants_json(request: Request) -> bool:
    """`fetch` from app.js asks for JSON; a plain form post does not."""
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


async def form_or_json(request: Request) -> dict:
    """Read a control's payload whether it arrived as a form post or as a JSON body."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items()}


def redirect(request: Request, url: str, *, message: str = "") -> Response:
    """One control, two callers: JSON for the fetch helper, a 303 for a plain form post."""
    if wants_json(request):
        return JSONResponse({"ok": True, "redirect": url, "message": message})
    return RedirectResponse(url, status_code=303)


def limit_response(request: Request, exc: LimitExceeded) -> Response:
    """The polite 429: a sheet for a browser, JSON for the fetch helper."""
    if wants_json(request):
        return JSONResponse(
            {"ok": False, "headline": exc.headline, "message": exc.message},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
        )
    return render(
        request,
        "message.html",
        status_code=429,
        eyebrow="Rate limit",
        headline=exc.headline,
        message=exc.message,
        links=[{"href": "/run", "label": "Back to the run page"}, {"href": "/cases", "label": "Case register"}],
    )


def not_found(request: Request, what: str, *, back: str = "/cases") -> HTMLResponse:
    return render(
        request,
        "message.html",
        status_code=404,
        eyebrow="Not found",
        headline="Nothing here",
        message=what,
        links=[{"href": back, "label": "Go back"}],
    )


def service_module() -> Any:
    """Import the agent façade lazily: the read-only surfaces must render even if the agent layer is
    mid-edit or its dependencies are not installed."""
    from ..agents import service

    return service


async def call_service(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a service function and await it if it turns out to be a coroutine.

    The façade's contract was specified async and is currently implemented sync (approve, dismiss,
    resolve_needs_human, record_response, run_followups, close_case). Both shapes are correct callers'
    business, so the web layer tolerates either and the merge point cannot break on it.
    """
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def maybe_store_audit(store: Store, case_id: str, actor: str, kind: str, detail: str) -> None:
    """Audit and never let the audit be the thing that breaks a control."""
    try:
        store.audit(case_id, actor, kind, detail)
    except Exception:  # pragma: no cover - a broken audit must not swallow the action
        pass


def parse_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


__all__ = [
    "STATIC_DIR",
    "TEMPLATES_DIR",
    "base_context",
    "call_service",
    "form_or_json",
    "get_store",
    "limit_response",
    "maybe_store_audit",
    "not_found",
    "parse_int",
    "redirect",
    "render",
    "service_module",
    "templates",
    "wants_json",
]
