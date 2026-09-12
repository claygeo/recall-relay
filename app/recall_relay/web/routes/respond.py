"""The agency's door: one click from the notice, one number if they have it, and the line is closed.

The link an agency receives carries a store token that is already unguessable. The inbox mirror renders it
wrapped in an itsdangerous signature so a tampered link is refused before it reaches the register; a raw
token still resolves, because that is the link that went out in the mail body and it has to keep working.
Rule 9: the taxonomy is closed. There is no free-text status, only a note attached to one of four answers.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response

from ...core.models import ResponseStatus
from ...core.rules import RESPONSE_OPTIONS
from ...core.store import Store
from .. import views
from ..deps import call_service, get_store, maybe_store_audit, parse_int, render, service_module
from ..security import unsign_token

router = APIRouter()

NEEDS_COUNT = {ResponseStatus.PULLED, ResponseStatus.NEED_PICKUP}

PROMPTS = {
    ResponseStatus.PULLED: "How many cases did you pull off the shelf? An estimate is fine — we need the number for the audit packet, not a perfect count.",
    ResponseStatus.NEED_PICKUP: "How many cases are waiting for pickup? We will schedule a truck and send you a confirmation.",
}


def _resolve(store: Store, token: str) -> tuple[Optional[str], Optional[tuple[str, str]]]:
    """Return (real_token, (case_id, agency_id)) or (None, None) when the link is not valid.

    A signed value is unwrapped first; anything else is tried against the register verbatim so the plain
    link in the mail body still works.
    """
    inner = unsign_token(token)
    if inner is not None:
        resolved = store.resolve_token(inner)
        if resolved is not None:
            return inner, resolved
        return None, None
    resolved = store.resolve_token(token)
    if resolved is not None:
        return token, resolved
    return None, None


def _bad_link(request: Request) -> Response:
    return render(
        request,
        "message.html",
        status_code=400,
        eyebrow="Response link",
        headline="That link is not valid",
        message=(
            "This reply link has been altered, or it belongs to a demo that has since been reset. Open the "
            "notice again from your inbox and use the link in it — or reply to the coordinator directly."
        ),
        links=[{"href": "/inbox", "label": "Open the agency inbox"}],
    )


def _context(store: Store, case_id: str, agency_id: str, token: str) -> dict:
    case = store.get_case(case_id)
    agency = store.get_agency(agency_id)
    mail = next(
        (m for m in reversed(store.outbox(case_id, agency_id)) if m.get("token") == token or m["kind"] == "notice"),
        None,
    )
    return {
        "case": case,
        "agency": agency,
        "mail": mail,
        "headline": views.product_headline(case) if case is not None else "Recall notice",
    }


def _suggested_count(store: Store, case, agency_id: str) -> int:
    if case is None or case.pull_list is None:
        return 0
    return sum(item.cases for item in case.pull_list.items if item.agency_id == agency_id)


async def _record(request: Request, store: Store, token: str, case_id: str, agency_id: str,
                  status: ResponseStatus, count: Optional[int], free_text: str) -> Response:
    before = sum(1 for m in store.outbox(case_id, agency_id) if m["kind"] == "client_sign")
    maybe_store_audit(
        store,
        case_id,
        "agency",
        "ui_response",
        f"{agency_id} answered {status.value} via the response link"
        + (f" ({count} cases)" if count is not None else ""),
    )
    try:
        service = service_module()
        await call_service(service.record_response, store, token, status, count, free_text)
    except Exception as exc:
        return render(
            request,
            "message.html",
            status_code=400,
            eyebrow="Response",
            headline="That answer could not be recorded",
            message=f"{type(exc).__name__}: {exc}",
            links=[{"href": "/inbox", "label": "Back to the inbox"}],
        )

    after = sum(1 for m in store.outbox(case_id, agency_id) if m["kind"] == "client_sign")
    latest = store.latest_response(case_id, agency_id) or {}
    context = _context(store, case_id, agency_id, token)
    context.update(
        {
            "status": status.value,
            "label": views.RESPONSE_LABELS.get(status.value, status.value),
            "count": count,
            "free_text": free_text,
            "recorded_at": views.fmt_stamp(latest.get("at")) or views.fmt_stamp(store.now()),
            "sign_sent": after > before,
        }
    )
    return render(request, "respond_done.html", **context)


@router.get("/r/{token}")
async def respond_get(request: Request, token: str, status: str = "") -> Response:
    store: Store = get_store(request)
    real_token, resolved = _resolve(store, token)
    if real_token is None or resolved is None:
        return _bad_link(request)
    case_id, agency_id = resolved

    if not status:
        context = _context(store, case_id, agency_id, real_token)
        return render(
            request,
            "message.html",
            eyebrow=f"Recall {context['case'].notice.recall_number if context['case'] else ''} · {case_id}",
            headline=context["headline"],
            message="Pick the answer that is true for your pantry. Any of the four closes your line.",
            links=[
                {"href": f"/r/{token}?status={option.status.value}", "label": option.label}
                for option in RESPONSE_OPTIONS
            ],
        )

    try:
        parsed = ResponseStatus(status)
    except ValueError:
        return _bad_link(request)

    if parsed in NEEDS_COUNT:
        context = _context(store, case_id, agency_id, real_token)
        context.update(
            {
                "token": token,
                "status": parsed.value,
                "prompt": PROMPTS[parsed],
                "suggested_count": _suggested_count(store, context["case"], agency_id),
            }
        )
        return render(request, "respond_confirm.html", **context)

    return await _record(request, store, real_token, case_id, agency_id, parsed, None, "")


@router.post("/r/{token}")
async def respond_post(
    request: Request,
    token: str,
    status: str = Form(...),
    count: str = Form(""),
    free_text: str = Form(""),
) -> Response:
    store: Store = get_store(request)
    real_token, resolved = _resolve(store, token)
    if real_token is None or resolved is None:
        return _bad_link(request)
    case_id, agency_id = resolved

    try:
        parsed = ResponseStatus(status)
    except ValueError:
        return _bad_link(request)

    n = parse_int(count, None) if str(count).strip() != "" else None
    if parsed in NEEDS_COUNT and n is None:
        n = 0
    return await _record(request, store, real_token, case_id, agency_id, parsed, n, str(free_text).strip())
