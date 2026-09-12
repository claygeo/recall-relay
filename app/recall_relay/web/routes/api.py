"""The controls: the scan and its SSE stream, manual intake, the one approval, follow-ups, the demo clock,
the reset, health, and the allow-listed data RPC the deployed AgentCore Runtime calls back into.

Every route here changes state or spends money, so every one of them is rate limited, audited, or both.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ...core.models import CaseStatus
from ...core.store import Store
from ..deps import (
    call_service,
    form_or_json,
    get_store,
    limit_response,
    maybe_store_audit,
    not_found,
    parse_int,
    redirect,
    render,
    service_module,
    wants_json,
)
from ..security import LimitExceeded, budget, limiter, scan_flight

router = APIRouter()


# ---------------------------------------------------------------------------
# the scan job registry (one process, one scan at a time)
# ---------------------------------------------------------------------------
class ScanJob:
    """An in-flight scan. Events are kept so a reloaded page can reattach and replay the log."""

    def __init__(self, job_id: str):
        self.id = job_id
        self.events: list[dict] = []
        self.finished = False
        self._bell = asyncio.Event()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        self._bell.set()

    def finish(self) -> None:
        self.finished = True
        self._bell.set()

    async def wait(self, timeout: float = 15.0) -> None:
        try:
            await asyncio.wait_for(self._bell.wait(), timeout=timeout)
        except TimeoutError:
            return
        self._bell.clear()


JOBS: dict[str, ScanJob] = {}
MAX_JOBS_KEPT = 8


async def _run_scan(store: Store, job: ScanJob) -> None:
    try:
        service = service_module()
        async for event in service.scan(store):
            job.emit(event)
    except Exception as exc:  # pragma: no cover - the log is the place an agent failure belongs
        job.emit({"type": "item", "index": 0, "total": 0, "title": "scan failed",
                  "decision": "error", "error": f"{type(exc).__name__}: {exc}"})
        job.emit({"type": "done", "tally": {"errors": 1}})
    finally:
        job.finish()
        scan_flight.release(job.id)


@router.post("/api/scan")
async def start_scan(request: Request) -> Response:
    store: Store = get_store(request)
    try:
        limiter.check(
            "scan",
            message="This demo allows six scans an hour so the feed (and the model bill) stays sane. "
                    "The case register, the inbox and the audit packet are all still readable.",
            headline="Too many scans this hour",
        )
        job_id = secrets.token_hex(4)
        scan_flight.acquire(job_id)
        try:
            budget.spend(1)
        except LimitExceeded:
            scan_flight.release(job_id)
            raise
    except LimitExceeded as exc:
        return limit_response(request, exc)

    job = ScanJob(job_id)
    JOBS[job_id] = job
    for stale in list(JOBS)[:-MAX_JOBS_KEPT]:
        JOBS.pop(stale, None)

    maybe_store_audit(store, "", "coordinator", "ui_scan_started", f"job {job_id}")
    asyncio.create_task(_run_scan(store, job))
    return JSONResponse({"ok": True, "job": job_id})


@router.get("/api/scan/stream")
async def scan_stream(request: Request, job: str) -> Response:
    scan_job = JOBS.get(job)
    if scan_job is None:
        return JSONResponse({"ok": False, "message": f"unknown scan job {job!r}"}, status_code=404)

    async def events():
        index = 0
        while True:
            while index < len(scan_job.events):
                yield f"data: {json.dumps(scan_job.events[index])}\n\n"
                index += 1
            if scan_job.finished:
                break
            if await request.is_disconnected():
                return
            await scan_job.wait()
            yield ": keepalive\n\n"
        yield f"data: {json.dumps({'type': 'closed', 'job': scan_job.id})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# manual intake
# ---------------------------------------------------------------------------
@router.post("/api/intake")
async def intake(
    request: Request,
    url: str = Form(""),
    text: str = Form(""),
    pdf: Optional[UploadFile] = File(None),
) -> Response:
    store: Store = get_store(request)
    try:
        limiter.check(
            "intake",
            message="This demo allows twelve manual intakes an hour. Everything already filed is still readable.",
            headline="Too many intakes this hour",
        )
        budget.spend(1)
    except LimitExceeded as exc:
        return limit_response(request, exc)

    service = service_module()
    payload = (pdf.filename if pdf is not None else "") or ""

    try:
        if pdf is not None and payload:
            data = await pdf.read()
            if not data:
                raise RuntimeError("the uploaded PDF was empty")
            maybe_store_audit(store, "", "coordinator", "ui_intake", f"pdf upload :: {payload} ({len(data)} bytes)")
            case = await service.intake_pdf(store, data)
        elif url.strip():
            maybe_store_audit(store, "", "coordinator", "ui_intake", f"pasted url :: {url.strip()}")
            case = await service.intake_url(store, url.strip())
        elif text.strip():
            maybe_store_audit(store, "", "coordinator", "ui_intake", f"pasted text :: {len(text)} chars")
            case = await service.intake_text(store, text.strip())
        else:
            budget.refund(1)
            return render(
                request,
                "message.html",
                status_code=400,
                tab="run",
                eyebrow="Intake",
                headline="Nothing to read",
                message="Paste a URL, paste the notice text, or upload a PDF. One source per submission.",
                links=[{"href": "/run", "label": "Back to the run page"}],
            )
    except Exception as exc:
        return render(
            request,
            "message.html",
            status_code=400,
            tab="run",
            eyebrow="Intake failed",
            headline="That notice could not be read",
            message=f"{type(exc).__name__}: {exc}",
            links=[{"href": "/run", "label": "Back to the run page"}],
        )

    maybe_store_audit(store, case.id, "coordinator", "ui_intake_case", f"case opened from manual intake: {case.status.value}")
    return redirect(request, f"/cases/{case.id}")


# ---------------------------------------------------------------------------
# the one decision, and what follows it
# ---------------------------------------------------------------------------
def _case_or_404(request: Request, case_id: str):
    store: Store = get_store(request)
    case = store.get_case(case_id)
    if case is None:
        return None, not_found(request, f"There is no case {case_id}.")
    return case, None


def _control_error(request: Request, exc: Exception, *, case_id: str) -> Response:
    message = f"{type(exc).__name__}: {exc}"
    if wants_json(request):
        return JSONResponse({"ok": False, "headline": "Not done", "message": message}, status_code=400)
    return render(
        request,
        "message.html",
        status_code=400,
        tab="cases",
        eyebrow="Not done",
        headline="That control could not run",
        message=message,
        links=[{"href": f"/cases/{case_id}", "label": "Back to the case"}],
    )


@router.post("/api/cases/{case_id}/approve")
async def approve(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    _case, missing = _case_or_404(request, case_id)
    if missing is not None:
        return missing
    maybe_store_audit(store, case_id, "coordinator", "ui_approve", "approve pressed in the dashboard")
    try:
        service = service_module()
        await call_service(service.approve, store, case_id)
    except Exception as exc:
        return _control_error(request, exc, case_id=case_id)
    return redirect(request, f"/cases/{case_id}")


@router.post("/api/cases/{case_id}/dismiss")
async def dismiss(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    _case, missing = _case_or_404(request, case_id)
    if missing is not None:
        return missing
    payload = await form_or_json(request)
    reason = str(payload.get("reason", "")).strip() or "dismissed by the coordinator without a stated reason"
    maybe_store_audit(store, case_id, "coordinator", "ui_dismiss", reason)
    try:
        service = service_module()
        await call_service(service.dismiss, store, case_id, reason)
    except Exception as exc:
        return _control_error(request, exc, case_id=case_id)
    return redirect(request, f"/cases/{case_id}")


@router.post("/api/cases/{case_id}/resolve")
async def resolve(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    _case, missing = _case_or_404(request, case_id)
    if missing is not None:
        return missing
    payload = await form_or_json(request)
    decision_text = str(payload.get("decision_text", "")).strip()
    treat_as = str(payload.get("treat_as", "MATCH")).strip().upper()
    if not decision_text or treat_as not in ("MATCH", "NO_MATCH"):
        return _control_error(
            request,
            ValueError("a decision in words and a MATCH / NO_MATCH ruling are both required"),
            case_id=case_id,
        )
    maybe_store_audit(store, case_id, "coordinator", "ui_resolve", f"{treat_as} :: {decision_text}")
    try:
        service = service_module()
        await call_service(service.resolve_needs_human, store, case_id, decision_text, treat_as)
    except Exception as exc:
        return _control_error(request, exc, case_id=case_id)
    return redirect(request, f"/cases/{case_id}")


@router.post("/api/cases/{case_id}/close")
async def close(request: Request, case_id: str) -> Response:
    store: Store = get_store(request)
    _case, missing = _case_or_404(request, case_id)
    if missing is not None:
        return missing
    maybe_store_audit(store, case_id, "coordinator", "ui_close", "close pressed in the dashboard")
    try:
        service = service_module()
        await call_service(service.close_case, store, case_id)
    except Exception as exc:
        return _control_error(request, exc, case_id=case_id)
    return redirect(request, f"/cases/{case_id}/packet")


# ---------------------------------------------------------------------------
# demo clock, follow-ups, reset, health
# ---------------------------------------------------------------------------
@router.post("/api/clock/advance")
async def clock_advance(request: Request) -> Response:
    store: Store = get_store(request)
    payload = await form_or_json(request)
    hours = parse_int(payload.get("hours", 24), 24) or 24
    hours = max(min(hours, 24 * 30), -24 * 30)
    store.advance_clock(float(hours))
    maybe_store_audit(store, "", "coordinator", "ui_clock_advance", f"demo clock advanced {hours}h")
    referer = request.headers.get("referer") or "/cases"
    return redirect(request, referer, message=f"demo clock advanced {hours}h")


@router.post("/api/followups/run")
async def followups_run(request: Request) -> Response:
    store: Store = get_store(request)
    maybe_store_audit(store, "", "coordinator", "ui_followups", "run follow-ups pressed in the dashboard")
    try:
        service = service_module()
        done = await call_service(service.run_followups, store)
    except Exception as exc:
        return _control_error(request, exc, case_id="")
    count = len(done) if isinstance(done, list) else (done or {}).get("sent", 0)
    referer = request.headers.get("referer") or "/cases"
    return redirect(request, referer, message=f"{count} follow-up(s) sent")


@router.post("/api/demo/reset")
async def demo_reset(request: Request) -> Response:
    store: Store = get_store(request)
    try:
        limiter.check(
            "reset",
            message="The demo reset is throttled to once a minute. Give the last one a moment to settle.",
            headline="Reset just ran",
        )
    except LimitExceeded as exc:
        return limit_response(request, exc)

    store.reset_demo()
    seeded: dict[str, int] = {}
    try:
        from ...core import seed as seed_module

        loader = getattr(seed_module, "load_seed", None)
        if callable(loader):
            seeded = loader(store) or {}
    except Exception:  # pragma: no cover - a missing seeder must not break the reset
        seeded = {}

    budget.refund(0)
    maybe_store_audit(store, "", "coordinator", "ui_demo_reset", f"demo reset; ledger reseeded: {seeded}")
    return redirect(request, "/ledger", message="demo reset")


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    store: Store = get_store(request)
    used, limit = budget.state()
    try:
        counts = {
            "receipts": len(store.list_receipts()),
            "agencies": len(store.list_agencies()),
            "distributions": len(store.list_distributions()),
            "cases": len(store.list_cases()),
            "holds": len(store.holds()),
            "outbox": len(store.outbox()),
        }
    except Exception as exc:  # pragma: no cover - a broken db should still answer honestly
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    open_cases = sum(
        1
        for c in store.list_cases()
        if c.status in (CaseStatus.NEEDS_HUMAN, CaseStatus.AWAITING_APPROVAL, CaseStatus.RELAYING, CaseStatus.CHASING)
    )
    return JSONResponse(
        {
            "ok": True,
            "counts": counts,
            "open_cases": open_cases,
            "scan_running": scan_flight.active is not None,
            "agent_runs_today": {"used": used, "limit": limit},
            "clock": store.now().isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# the data RPC the deployed Runtime calls (DECISIONS #5)
# ---------------------------------------------------------------------------
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "list_agencies",
        "get_agency",
        "list_receipts",
        "get_receipt",
        "list_distributions",
        "on_hand",
        "holds",
        "set_hold",
        "create_case",
        "save_case",
        "get_case",
        "find_case_by_source",
        "list_cases",
        "issue_token",
        "resolve_token",
        "record_response",
        "responses",
        "latest_response",
        "schedule_followup",
        "due_followups",
        "followups",
        "mark_followup",
        "cancel_followups",
        "audit",
        "events",
        "remember",
        "decisions",
        "now",
        "record_mail",
        "outbox",
    }
)


def jsonable(value: Any) -> Any:
    """Pydantic models, datetimes, sets and int-keyed dicts, in a shape httpx can carry."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    return value


@router.post("/api/data/rpc")
async def data_rpc(request: Request) -> JSONResponse:
    from ..security import data_secret

    secret = data_secret()
    presented = request.headers.get("x-relay-secret", "")
    if not secret or presented != secret:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "body must be a JSON object"}, status_code=400)

    method = str(body.get("method", ""))
    args = body.get("args") or []
    kwargs = body.get("kwargs") or {}
    if method not in ALLOWED_METHODS:
        return JSONResponse(
            {"ok": False, "error": f"method {method!r} is not allow-listed", "allowed": sorted(ALLOWED_METHODS)},
            status_code=400,
        )
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        return JSONResponse({"ok": False, "error": "args must be a list and kwargs an object"}, status_code=400)

    store: Store = get_store(request)
    fn = getattr(store, method, None)
    if not callable(fn):
        return JSONResponse({"ok": False, "error": f"store has no method {method!r}"}, status_code=400)

    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=400)

    return JSONResponse({"ok": True, "method": method, "result": jsonable(result)})
