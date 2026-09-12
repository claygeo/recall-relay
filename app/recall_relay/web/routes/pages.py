"""The read-only surfaces: ledger, run, cases, one case, the inbox mirror, the audit packet.

Nothing in this module writes. Every control that changes state lives in routes/api.py or routes/respond.py,
so a judge clicking around the dashboard cannot move the demo by accident.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from ...core.rules import RESPONSE_OPTIONS
from ...core.store import Store
from .. import views
from ..deps import get_store, not_found, render, service_module
from ..pdf import render_packet_pdf
from ..security import sign_token

router = APIRouter()


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
@router.get("/")
@router.get("/ledger")
async def ledger(request: Request, dist: int = 1) -> Response:
    store: Store = get_store(request)
    return render(request, "ledger.html", tab="ledger", **views.ledger_view(store, dist_page=dist))


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@router.get("/run")
async def run(request: Request) -> Response:
    return render(request, "run.html", tab="run")


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------
@router.get("/cases")
async def cases(request: Request) -> Response:
    store: Store = get_store(request)
    return render(request, "cases.html", tab="cases", **views.cases_view(store))


def _ping_for(store: Store, case) -> str:
    """The verbatim ping, or an honest blank if the agent layer cannot be imported."""
    try:
        service = service_module()
    except Exception:  # pragma: no cover - agent layer unavailable
        return ""
    try:
        return service.ping_text(case, store)
    except Exception:  # pragma: no cover - a half-built case should still render
        return ""


@router.get("/cases/{case_id}")
async def case_detail(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    case = store.get_case(case_id)
    if case is None:
        return not_found(request, f"There is no case {case_id}.")

    context = views.case_view(store, case, ping=_ping_for(store, case))
    context["notices_sent_count"] = sum(1 for m in context["outbox"] if m["kind"] == "notice")
    context["responded_count"] = sum(1 for row in context["agency_rows"] if row["response"])
    return render(request, "case.html", tab="cases", **context)


# ---------------------------------------------------------------------------
# packet
# ---------------------------------------------------------------------------
@router.get("/packet")
async def packet_tab(request: Request) -> Response:
    """The Packet tab has no case of its own: send the coordinator to the most recently filed one."""
    store: Store = get_store(request)
    all_cases = store.list_cases()
    closed = [c for c in all_cases if c.closed_at is not None]
    target = (closed or all_cases)
    if not target:
        return render(
            request,
            "message.html",
            tab="packet",
            eyebrow="Audit packet",
            headline="No packet has been filed yet",
            message=(
                "An audit packet is produced when a case is closed: every source, every matched ledger row, "
                "every send and reply, and the elapsed time from the notice to the full trace. Run the daily "
                "scan first."
            ),
            links=[{"href": "/run", "label": "Run the daily scan"}, {"href": "/cases", "label": "Case register"}],
        )
    return RedirectResponse(f"/cases/{target[0].id}/packet", status_code=307)


@router.get("/cases/{case_id}/packet")
async def packet(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    case = store.get_case(case_id)
    if case is None:
        return not_found(request, f"There is no case {case_id}.")
    return render(request, "packet.html", tab="packet", **views.packet_view(store, case))


@router.get("/cases/{case_id}/packet.pdf")
async def packet_pdf(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    case = store.get_case(case_id)
    if case is None:
        return not_found(request, f"There is no case {case_id}.")
    data = render_packet_pdf(views.packet_view(store, case))
    filename = f"recall-relay-{case.id}.pdf"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# inbox mirror
# ---------------------------------------------------------------------------
def _inbox(request: Request, agency_id: str = "") -> Response:
    store: Store = get_store(request)
    context = views.inbox_view(store, agency_id=agency_id)
    context["response_options"] = RESPONSE_OPTIONS
    context["signed_tokens"] = {
        row["mail"]["token"]: sign_token(row["mail"]["token"]) for row in context["rows"] if row["mail"].get("token")
    }
    return render(request, "inbox.html", tab="inbox", **context)


@router.get("/inbox")
async def inbox(request: Request) -> Response:
    return _inbox(request)


@router.get("/inbox/{agency_id}")
async def inbox_agency(request: Request, agency_id: str) -> Response:
    store: Store = get_store(request)
    if store.get_agency(agency_id) is None:
        return not_found(request, f"There is no agency {agency_id}.", back="/inbox")
    return _inbox(request, agency_id)
