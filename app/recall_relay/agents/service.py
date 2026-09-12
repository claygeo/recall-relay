"""The façade the web layer calls. Everything above this line is agents; everything below is a Store.

The dashboard never constructs an Agent, never touches the mailer, and never decides a status. It calls
`scan`, `process_notice`, `approve`, `record_response`, `run_followups`, `close_case` -- and the rules stay
in one place.

Event vocabulary yielded by `scan` (all JSON-able):

    {"type": "feed",    "source": "snapshot"|"live", "url": ..., "items": n, "error": "..."}
    {"type": "fetch",   "link": ..., "origin": "fixture"|"network", "blocked": bool}
    {"type": "tool",    "name": ..., "case_id": ..., "link": ...}
    {"type": "verdict", "case_id": ..., "verdict": ..., "receipt_ids": [...], "reason": ...}
    {"type": "ping",    "case_id": ..., "text": ...}
    {"type": "item",    "index": i, "total": n, "title": ..., "link": ...,
                        "decision": "already_seen"|"skipped_non_food"|"dismissed"|"processed"|"error",
                        "case_id": ..., "status": ...}
    {"type": "done",    "tally": {...}}

An "item" event always carries a decision and is the last event for that item.
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Optional

from ..core import intake, rules
from ..core.config import settings
from ..core.models import (
    AuditPacket,
    CallScript,
    CaseStatus,
    RecallCase,
    RecallNotice,
    ResponseStatus,
    Source,
    Verdict,
)
from ..core.store import Store
from . import writer as writer_mod
from .extractor import extract_notice, needs_extraction
from .matcher import adjudicate
from .orchestrator import (
    build_orchestrator,
    compute_pull_list,
    draft_all,
    ping_text as _ping_text,
    relay_notices,
)
from .mailer import get_mailer, send_and_record

SNAPSHOT_RSS = "rss/recalls-2026-09-11.xml"  # relative to settings.fixtures_dir

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
EventSink = Optional[Callable[[dict], Any]]


async def _emit(sink: EventSink, event: dict) -> None:
    if sink is None:
        return
    out = sink(event)
    if inspect.isawaitable(out):
        await out


def _norm_link(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)
    return u.rstrip("/").lower()


@lru_cache(maxsize=1)
def _press_fixture_index() -> dict[str, str]:
    """Map canonical URL -> fixture path for the cached FDA press pages.

    The pinned snapshot exists so the demo reproduces on camera (DECISIONS #6); serving its press pages
    from disk means a fetch failure cannot sink the run.
    """
    index: dict[str, str] = {}
    press_dir = Path(settings.fixtures_dir) / "press"
    if not press_dir.exists():
        return index
    for path in sorted(press_dir.glob("*.html")):
        try:
            html = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable fixture
            continue
        m = re.search(r'rel="canonical"\s+href="([^"]+)"', html)
        if m:
            index[_norm_link(m.group(1))] = str(path)
    return index


def parse_rss_file(path: Path | str) -> list[intake.RssItem]:
    """Parse a pinned RSS snapshot with the same parser the live feed uses."""
    return intake.parse_rss(Path(path).read_text(encoding="utf-8", errors="ignore"))


# FDA press pages are hand-tagged and the tag is sometimes wrong: the 2026-09-03 American Regent
# epinephrine-injection recall is filed under "Product Type: Food & Beverages". `rules.looks_like_food`
# trusts Product Type when it names food, so a mis-tagged drug recall would open a case and cost a model
# call. This is the narrow override: only words that cannot appear in a real food recall title. Ambiguous
# hints ("capsule", "tablet", "device", "cosmetic") are deliberately NOT here -- dietary supplements are
# FDA-regulated food and silently dropping a real food recall is the expensive failure (rule 5).
UNAMBIGUOUS_NON_FOOD = re.compile(
    r"\b(drug|drugs|injection|injectable|medical device|firmware|vape|tobacco|"
    r"pet food|dog food|cat food|supplements? for dogs|feline|canine)\b",
    re.I,
)


def next_case_id(store: Store, *, drill: bool = False) -> str:
    """RC-YYYY-MMDD-NNN, sequential within the day."""
    today = store.now().date()
    prefix = f"{'DRILL' if drill else 'RC'}-{today.year:04d}-{today.month:02d}{today.day:02d}-"
    existing = [c.id for c in store.list_cases() if c.id.startswith(prefix)]
    n = 0
    for cid in existing:
        try:
            n = max(n, int(cid.rsplit("-", 1)[1]))
        except (ValueError, IndexError):  # pragma: no cover - hand-edited id
            continue
    return f"{prefix}{n + 1:03d}"


# ---------------------------------------------------------------------------
# intake -> notice
# ---------------------------------------------------------------------------
def notice_from_raw(raw: Any, source: Source, seen_at: Optional[datetime] = None) -> RecallNotice:
    """Deterministic parse first; the extractor agent only when the parse fell short (< 0.8)."""
    notice = intake.to_recall_notice(raw, source, seen_at)
    if needs_extraction(notice):
        notice = extract_notice(raw, source, seen_at or notice.source_seen_at)
    return notice


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------
async def scan(store: Store, *, live: bool = True, snapshot: bool = True) -> AsyncIterator[dict]:
    """Walk the FDA recall feed and run the procedure on anything that touches this food bank.

    The pinned snapshot is the base so the demo reproduces; live items are additive and de-duplicated by
    link. Non-food is skipped, a recall whose distribution excludes our state and whose brand we never
    received is dismissed silently (rule 8), and everything else becomes a case.
    """
    items: list[intake.RssItem] = []
    seen_links: set[str] = set()

    if snapshot:
        path = Path(settings.fixtures_dir) / SNAPSHOT_RSS
        try:
            snap = parse_rss_file(path)
        except Exception as exc:  # pragma: no cover - missing/bad fixture
            snap = []
            yield {"type": "feed", "source": "snapshot", "url": str(path), "items": 0, "error": str(exc)}
        else:
            yield {"type": "feed", "source": "snapshot", "url": str(path), "items": len(snap)}
        for it in snap:
            key = _norm_link(it.link)
            if key and key not in seen_links:
                seen_links.add(key)
                items.append(it)

    if live:
        try:
            fresh = intake.poll_fda_rss(cache_dir=Path(settings.fixtures_dir) / "rss")
        except Exception as exc:
            fresh = []
            yield {
                "type": "feed",
                "source": "live",
                "url": intake.FDA_RSS_URL,
                "items": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        else:
            added = 0
            for it in fresh:
                key = _norm_link(it.link)
                if key and key not in seen_links:
                    seen_links.add(key)
                    items.append(it)
                    added += 1
            yield {"type": "feed", "source": "live", "url": intake.FDA_RSS_URL, "items": added}

    tally = {
        "seen": len(items),
        "already_seen": 0,
        "skipped_non_food": 0,
        "dismissed": 0,
        "processed": 0,
        "needs_human": 0,
        "awaiting_approval": 0,
        "errors": 0,
    }

    total = len(items)
    for index, item in enumerate(items, start=1):
        base = {"type": "item", "index": index, "total": total, "title": item.title, "link": item.link}

        existing = store.find_case_by_source(item.link)
        if existing is not None:
            tally["already_seen"] += 1
            yield {**base, "decision": "already_seen", "case_id": existing.id, "status": existing.status.value}
            continue

        try:
            html, origin, blocked = _fetch_press(item.link)
            yield {"type": "fetch", "link": item.link, "origin": origin, "blocked": blocked}
            if blocked or not html:
                tally["errors"] += 1
                yield {**base, "decision": "error", "error": "fetch blocked or empty"}
                continue

            raw = intake.parse_fda_press_page(html, item.link)
            product_type = getattr(raw, "product_type", "")
            mis_tagged = bool(UNAMBIGUOUS_NON_FOOD.search(item.title or ""))
            if (
                not rules.looks_like_food(item.title, product_type)
                or not getattr(raw, "is_food", True)
                or mis_tagged
            ):
                tally["skipped_non_food"] += 1
                reason = (
                    f"FDA tagged this {product_type!r} but the title is a drug/device recall"
                    if mis_tagged and rules.looks_like_food(item.title, product_type)
                    else f"product type {product_type!r}"
                )
                yield {**base, "decision": "skipped_non_food", "reason": reason}
                continue

            notice = notice_from_raw(raw, Source.FDA_RSS, item.published)

            auto_dismiss, reason = rules.distribution_gate(
                notice, settings.food_bank_state, store.ledger_brands()
            )
            if auto_dismiss:
                case = _open_case(store, notice)
                case.status = CaseStatus.DISMISSED
                case.dismissed_reason = reason
                case.closed_at = store.now()
                store.save_case(case)
                store.audit(case.id, "system", "dismissed", f"distribution gate (rule 8): {reason}")
                tally["dismissed"] += 1
                yield {**base, "decision": "dismissed", "case_id": case.id, "reason": reason}
                continue
        except NotImplementedError as exc:
            tally["errors"] += 1
            yield {**base, "decision": "error", "error": f"intake not implemented: {exc}"}
            continue
        except Exception as exc:  # pragma: no cover - network / parse surprises
            tally["errors"] += 1
            yield {**base, "decision": "error", "error": f"{type(exc).__name__}: {exc}"}
            continue

        events: list[dict] = []
        case = await process_notice(store, notice, event_sink=events.append)
        for event in events:
            yield {**event, "link": item.link}

        tally["processed"] += 1
        if case.status == CaseStatus.DISMISSED:
            tally["dismissed"] += 1
        elif case.status == CaseStatus.NEEDS_HUMAN:
            tally["needs_human"] += 1
        elif case.status == CaseStatus.AWAITING_APPROVAL:
            tally["awaiting_approval"] += 1
            yield {"type": "ping", "case_id": case.id, "text": ping_text(case, store)}
        yield {**base, "decision": "processed", "case_id": case.id, "status": case.status.value}

    yield {"type": "done", "tally": tally}


def _fetch_press(url: str) -> tuple[str, str, bool]:
    """Fixture first, network second. Returns (html, origin, blocked)."""
    fixture = _press_fixture_index().get(_norm_link(url))
    if fixture:
        return Path(fixture).read_text(encoding="utf-8", errors="ignore"), "fixture", False
    result = intake.fetch_url(url, cache_dir=Path(settings.fixtures_dir) / "press")
    return result.text, ("cache" if result.from_cache else "network"), bool(result.blocked)


# ---------------------------------------------------------------------------
# running one notice through the orchestrator
# ---------------------------------------------------------------------------
def _open_case(store: Store, notice: RecallNotice, *, is_drill: bool = False) -> RecallCase:
    case = RecallCase(
        id=next_case_id(store, drill=is_drill),
        status=CaseStatus.NEW,
        is_drill=is_drill,
        created_at=store.now(),
        notice=notice,
    )
    store.create_case(case)
    store.audit(
        case.id,
        "system",
        "case_opened",
        f"{notice.source.value} :: {notice.firm} :: {notice.title or notice.reason} :: {notice.source_url}",
    )
    return case


async def process_notice(
    store: Store,
    notice: RecallNotice,
    *,
    event_sink: EventSink = None,
    is_drill: bool = False,
    orchestrator: Any = None,
    invocation_extras: Optional[dict] = None,
) -> RecallCase:
    """Open a case for this notice and let the orchestrator work the procedure.

    Returns the saved case. Its status is DISMISSED, NEEDS_HUMAN or AWAITING_APPROVAL -- never RELAYING,
    because the agent does not send (rule 12).
    """
    case = _open_case(store, notice, is_drill=is_drill)
    agent = orchestrator if orchestrator is not None else build_orchestrator(store, case_id=case.id)

    state: dict[str, Any] = {"store": store, "case_id": case.id}
    if invocation_extras:
        state.update(invocation_extras)

    prompt = (
        f"Case {case.id} is open for a recall from {notice.firm}. Work the procedure and stop at the "
        f"first terminal step."
    )

    emitted: set[str] = set()
    async for event in agent.stream_async(prompt, invocation_state=state):
        current = event.get("current_tool_use") or {}
        tool_use_id = current.get("toolUseId")
        name = current.get("name")
        if tool_use_id and name and tool_use_id not in emitted:
            emitted.add(tool_use_id)
            await _emit(event_sink, {"type": "tool", "name": name, "case_id": case.id})

    saved = store.get_case(case.id) or case
    if saved.verdict is not None:
        await _emit(
            event_sink,
            {
                "type": "verdict",
                "case_id": saved.id,
                "verdict": saved.verdict.verdict.value,
                "receipt_ids": saved.verdict.matched_receipt_ids,
                "widening_applied": saved.verdict.widening_applied,
                "reason": saved.verdict.reason,
            },
        )
    return saved


# ---------------------------------------------------------------------------
# manual intake doors
# ---------------------------------------------------------------------------
async def intake_url(store: Store, url: str, *, event_sink: EventSink = None) -> RecallCase:
    """A coordinator pasted a link."""
    html, origin, blocked = _fetch_press(url)
    if blocked or not html:
        raise RuntimeError(f"could not fetch {url} (origin={origin}, blocked={blocked})")
    raw = intake.parse_fda_press_page(html, url)
    notice = notice_from_raw(raw, Source.PASTED_URL, store.now())
    return await process_notice(store, notice, event_sink=event_sink)


async def intake_text(store: Store, text: str, *, source_url: str = "", event_sink: EventSink = None) -> RecallCase:
    """A coordinator forwarded an alert or pasted a firm letter (this is the FSIS / USDA Foods door)."""
    raw = intake.parse_text(text, source_url)
    notice = notice_from_raw(raw, Source.PASTED_TEXT, store.now())
    return await process_notice(store, notice, event_sink=event_sink)


async def intake_pdf(store: Store, data: bytes, *, source_url: str = "", event_sink: EventSink = None) -> RecallCase:
    """A coordinator uploaded a PDF. Text layer only -- no OCR, and an empty text layer is an honest error."""
    text = intake.pdf_to_text(data)
    if not (text or "").strip():
        raise RuntimeError("PDF has no text layer; re-send the notice as text or a link")
    raw = intake.parse_text(text, source_url)
    notice = notice_from_raw(raw, Source.UPLOADED_PDF, store.now())
    return await process_notice(store, notice, event_sink=event_sink)


# ---------------------------------------------------------------------------
# the one human decision, and what follows it
# ---------------------------------------------------------------------------
def approve(store: Store, case_id: str) -> RecallCase:
    """The coordinator said send. This is the only place notices leave the building."""
    case = store.get_case(case_id)
    if case is None:
        raise KeyError(case_id)
    if case.is_drill:
        store.audit(case.id, "system", "send_blocked", "drill case: approval does not mail anyone (rule 14)")
        raise PermissionError(f"{case.id} is a drill; drills never mail an agency")
    if not case.notices:
        raise ValueError(f"{case.id} has no drafted notices to send")

    case.approved_at = store.now()
    case.status = CaseStatus.RELAYING
    store.save_case(case)
    store.audit(case.id, "coordinator", "approved", f"{len(case.notices)} notices approved for relay")

    sent = relay_notices(store, case)
    store.audit(case.id, "system", "relayed", f"{len(sent)} agencies notified: {[a for a, _ in sent]}")
    return store.get_case(case.id) or case


def dismiss(store: Store, case_id: str, reason: str) -> RecallCase:
    """The coordinator said no. Silent, logged forever."""
    case = store.get_case(case_id)
    if case is None:
        raise KeyError(case_id)
    case.status = CaseStatus.DISMISSED
    case.dismissed_reason = reason
    case.closed_at = store.now()
    store.save_case(case)
    store.audit(case.id, "coordinator", "dismissed", reason)
    return case


def resolve_needs_human(
    store: Store,
    case_id: str,
    decision_text: str,
    treat_as: Literal["MATCH", "NO_MATCH"],
    *,
    kind: str = "row_correction",
    matcher_agent: Any = None,
    writer_agent: Any = None,
    sign_agent: Any = None,
) -> RecallCase:
    """A coordinator answered the ambiguity. Remember the answer, then re-run matching under it.

    The remembered decision is permanent (rule 14): the next recall that surfaces the same row sees it
    verbatim in the matcher's prompt and does not ask again.
    """
    case = store.get_case(case_id)
    if case is None:
        raise KeyError(case_id)

    decision = store.remember(decision_text, kind)
    store.audit(case.id, "coordinator", "decision_remembered", f"[{decision.id}] ({kind}) {decision_text}")

    candidates = rules.score_candidates(store.list_receipts(), case.notice)
    rerun = adjudicate(case.notice, candidates, store.decisions(), agent=matcher_agent)

    forced = Verdict.MATCH if treat_as == "MATCH" else Verdict.NO_MATCH
    matched = rerun.matched_receipt_ids if forced == Verdict.MATCH else []
    verdict = rerun.model_copy(
        update={
            "verdict": forced,
            "matched_receipt_ids": matched,
            "evidence": list(rerun.evidence)
            + [f"coordinator resolution: {decision_text} (treated as {treat_as})"],
            "reason": f"resolved by coordinator as {treat_as}: {decision_text}",
        }
    )
    case.verdict = verdict
    store.save_case(case)
    store.audit(
        case.id,
        "agent",
        "verdict",
        f"{verdict.verdict.value} rows={verdict.matched_receipt_ids} (re-run after coordinator resolution)",
    )

    if forced == Verdict.NO_MATCH:
        return dismiss(store, case.id, f"coordinator resolution: {decision_text}")

    if not matched:
        case.status = CaseStatus.NEEDS_HUMAN
        store.save_case(case)
        store.audit(
            case.id,
            "system",
            "needs_human",
            "coordinator said MATCH but the re-run named no ledger row; still needs a human",
        )
        return case

    compute_pull_list(store, case)
    case = store.get_case(case.id) or case
    draft_all(store, case, writer_agent=writer_agent, sign_agent=sign_agent)
    case = store.get_case(case.id) or case
    case.status = CaseStatus.AWAITING_APPROVAL
    store.save_case(case)
    store.audit(case.id, "agent", "approval_requested", ping_text(case, store))
    return case


# ---------------------------------------------------------------------------
# responses, follow-ups, close
# ---------------------------------------------------------------------------
def _affected_agencies(case: RecallCase) -> list[str]:
    if case.pull_list is not None and case.pull_list.agencies_affected:
        return list(case.pull_list.agencies_affected)
    return [n.agency_id for n in case.notices]


def record_response(
    store: Store,
    token: str,
    status: ResponseStatus | str,
    count: Optional[int] = None,
    free_text: str = "",
    *,
    sign_agent: Any = None,
) -> RecallCase:
    """An agency replied (rule 9: the taxonomy is closed, so this is the only way a reply is recorded).

    ALREADY_DISTRIBUTED mails that agency the client shelf sign immediately (rule 15). Any reply cancels
    that agency's pending follow-ups. Nobody is ever marked confirmed by silence.
    """
    resolved = store.resolve_token(token)
    if resolved is None:
        raise KeyError(f"unknown response token {token!r}")
    case_id, agency_id = resolved
    case = store.get_case(case_id)
    if case is None:
        raise KeyError(case_id)
    status = ResponseStatus(status) if not isinstance(status, ResponseStatus) else status

    store.record_response(case_id, agency_id, status, count, free_text, token)
    store.cancel_followups(case_id, agency_id)
    store.audit(
        case_id,
        "agency",
        "response",
        f"{agency_id}: {status.value}" + (f" ({count} cases)" if count is not None else "")
        + (f" :: {free_text}" if free_text else ""),
    )

    if status == ResponseStatus.ALREADY_DISTRIBUTED:
        _mail_client_signs(store, case, agency_id, sign_agent=sign_agent)

    responded = {r["agency_id"] for r in store.responses(case_id)}
    affected = set(_affected_agencies(case))
    if affected and affected <= responded:
        for f in store.followups(case_id):
            if f["sent_at"] is None:
                store.cancel_followups(case_id, f["agency_id"])
        store.audit(
            case_id,
            "system",
            "ready_to_close",
            f"all {len(affected)} agencies responded; the loop is closed and the packet can be filed",
        )
    return store.get_case(case_id) or case


def _mail_client_signs(store: Store, case: RecallCase, agency_id: str, *, sign_agent: Any = None) -> int:
    """Send the shelf sign(s) to one agency. Drafts on the spot if the case has none for them."""
    agency = store.get_agency(agency_id)
    if agency is None:
        store.audit(case.id, "system", "sign_skipped", f"unknown agency {agency_id}")
        return 0

    signs = [s for s in case.signs if s.agency_id == agency_id]
    if not signs:
        for language in writer_mod.sign_languages_for(agency):
            try:
                signs.append(
                    writer_mod.draft_client_sign(
                        case, agency, language, agent=sign_agent, store=store  # type: ignore[arg-type]
                    )
                )
            except Exception as exc:  # pragma: no cover - writer unavailable
                store.audit(case.id, "system", "sign_failed", f"{agency_id}/{language}: {exc}")
        if signs:
            case.signs = list(case.signs) + signs
            store.save_case(case)

    sent = 0
    for sign in signs:
        subject, body = writer_mod.render_sign_email(sign, case)
        send_and_record(
            store,
            case_id=case.id,
            agency_id=agency_id,
            to_addr=agency.contact_email,
            subject=subject,
            body=body,
            kind="client_sign",
        )
        sent += 1
    store.audit(
        case.id,
        "system",
        "client_sign_sent",
        f"{agency_id}: {sent} sign(s) sent because they reported the product already went to clients (rule 15)",
    )
    return sent


def build_call_script(store: Store, case: RecallCase, agency_id: str) -> CallScript:
    """The escalation artifact: what to say on the phone. Deterministic, because a call script is a form."""
    agency = store.get_agency(agency_id)
    name = agency.name if agency else agency_id
    contact = agency.contact_name if agency else "whoever answers"
    phone = agency.contact_phone if agency else ""
    product = "; ".join(
        " ".join(b for b in (p.brand, p.name, p.size) if b) for p in case.notice.products
    ) or case.notice.title
    shipped = 0
    if case.pull_list is not None:
        shipped = sum(i.cases for i in case.pull_list.items if i.agency_id == agency_id)
    recall_number = case.notice.recall_number or "pending"
    classification = rules.effective_classification(case.notice).value

    return CallScript(
        agency_id=agency_id,
        contact_name=contact,
        contact_phone=phone,
        opening=(
            f"Hi {contact}, this is {settings.food_bank_name} calling about a {classification} recall. "
            f"We emailed {name} about it and we have not heard back, so I am calling to close the loop."
        ),
        ask=(
            f"We shipped you {shipped} cases of {product}. Recall number {recall_number}. "
            f"Do you still have any of it, and has any of it gone out to clients?"
        ),
        if_pulled=(
            "Thank you. How many cases did you pull? I will log that and you are done -- nothing else is "
            "needed from you."
        ),
        if_distributed=(
            "That is okay and it is why we call. I am sending you a shelf sign in your languages right now; "
            "post it where clients pick up and hand it to anyone who asks. I will log this as distributed."
        ),
        close=(
            f"That is everything. From the recall notice, word for word: {case.notice.disposition_verbatim} "
            f"Call me back at any time if you find more."
        ),
    )


def run_followups(store: Store, *, now: Optional[datetime] = None) -> list[dict]:
    """Send everything that is due (rule 10). An agency is never auto-confirmed by silence.

    A reminder goes to the agency; an escalation goes to the coordinator with a call script, because the
    next step after a silent pantry is a human picking up the phone, not another email.
    """
    now = now or store.now()
    done: list[dict] = []
    for row in store.due_followups(now):
        case = store.get_case(row["case_id"])
        if case is None:
            store.mark_followup(row["id"], now)
            continue
        if case.status in (CaseStatus.DISMISSED, CaseStatus.CLOSED):
            store.mark_followup(row["id"], now)
            continue
        if store.latest_response(case.id, row["agency_id"]) is not None:
            store.mark_followup(row["id"], now)
            store.audit(case.id, "system", "followup_skipped", f"{row['agency_id']} already responded")
            continue

        agency = store.get_agency(row["agency_id"])
        if agency is None:
            store.mark_followup(row["id"], now)
            continue

        notice = next((n for n in case.notices if n.agency_id == agency.id), None)
        if row["kind"] == "reminder":
            subject = f"Reminder: {notice.subject if notice else 'recall notice'}"
            body = "\n".join(
                [
                    f"{agency.contact_name}, we have not heard back on this recall and we need a yes or no "
                    f"from {agency.name} to close it out.",
                    "",
                    (notice.body if notice else ""),
                    "",
                    "If you never received this product, say so -- that is a complete answer and it closes "
                    "your line.",
                ]
            )
            send_and_record(
                store,
                case_id=case.id,
                agency_id=agency.id,
                to_addr=agency.contact_email,
                subject=subject,
                body=body,
                kind="reminder",
            )
            store.audit(case.id, "system", "reminder_sent", f"{agency.id} <{agency.contact_email}>")
            done.append({"case_id": case.id, "agency_id": agency.id, "kind": "reminder"})
        else:
            script = build_call_script(store, case, agency.id)
            subject = f"Call {agency.name} about {case.notice.recall_number or 'the open recall'} ({case.id})"
            body = "\n".join(
                [
                    f"{agency.name} has not responded. Call {script.contact_name} at {script.contact_phone}.",
                    "",
                    f"OPEN: {script.opening}",
                    "",
                    f"ASK: {script.ask}",
                    "",
                    f"IF THEY PULLED IT: {script.if_pulled}",
                    "",
                    f"IF IT WENT OUT: {script.if_distributed}",
                    "",
                    f"CLOSE: {script.close}",
                    "",
                    "Nobody is marked confirmed until they actually answer.",
                ]
            )
            send_and_record(
                store,
                case_id=case.id,
                agency_id=agency.id,
                to_addr=settings.coordinator_email,
                subject=subject,
                body=body,
                kind="escalation",
            )
            store.audit(case.id, "system", "escalated", f"{agency.id}: call script sent to the coordinator")
            done.append({"case_id": case.id, "agency_id": agency.id, "kind": "escalation"})

        store.mark_followup(row["id"], now)
        if case.status == CaseStatus.RELAYING:
            case.status = CaseStatus.CHASING
            store.save_case(case)
    return done


def close_case(store: Store, case_id: str) -> AuditPacket:
    """Rule 13: file the packet. Every source, every match, every send, every reply, in one object."""
    case = store.get_case(case_id)
    if case is None:
        raise KeyError(case_id)

    responses = store.responses(case_id)
    followups = store.followups(case_id)
    events = store.events(case_id)
    outbox = store.outbox(case_id)
    notices_sent = sum(1 for m in outbox if m["kind"] == "notice")

    sources = [s for s in [case.notice.source_url] if s]
    if case.notice.recall_number:
        sources.append(f"openFDA recall {case.notice.recall_number}")

    approvals = []
    if case.approved_at is not None:
        approvals.append(f"coordinator approved relay at {case.approved_at.isoformat()}")
    for event in events:
        if event.actor == "coordinator" and event.kind in ("dismissed", "decision_remembered"):
            approvals.append(f"{event.kind} at {event.at.isoformat()}: {event.detail}")

    elapsed = None
    last = max((datetime.fromisoformat(r["at"]) for r in responses), default=None)
    if last is not None:
        delta = last - case.notice.source_seen_at
        elapsed = _humanize(delta)

    now = store.now()
    case.status = CaseStatus.CLOSED
    case.closed_at = now
    store.save_case(case)
    store.audit(case.id, "system", "closed", f"audit packet filed; {notices_sent} notices, {len(responses)} responses")

    return AuditPacket(
        case_id=case.id,
        is_drill=case.is_drill,
        recall_number=case.notice.recall_number or "pending",
        generated_at=now,
        sources=sources,
        matched_receipts=list(case.verdict.matched_receipt_ids) if case.verdict else [],
        pull_list=case.pull_list,
        notices_sent=notices_sent,
        responses=responses,
        followups=followups,
        disposition_verbatim=case.notice.disposition_verbatim,
        approvals=approvals,
        elapsed_notice_to_full_trace=elapsed,
        events=store.events(case.id),
    )


def _humanize(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{sign}{days}d {hours}h"
    if hours:
        return f"{sign}{hours}h {minutes}m"
    return f"{sign}{minutes}m"


def ping_text(case: RecallCase, store: Optional[Store] = None) -> str:
    """The verbatim one-decision ping the coordinator sees."""
    return _ping_text(case, store)


def outstanding_agencies(store: Store, case: RecallCase) -> list[str]:
    """Who still owes an answer. Used by the dashboard grid and by close-readiness."""
    responded = {r["agency_id"] for r in store.responses(case.id)}
    return [a for a in _affected_agencies(case) if a not in responded]


__all__ = [
    "approve",
    "build_call_script",
    "close_case",
    "dismiss",
    "get_mailer",
    "intake_pdf",
    "intake_text",
    "intake_url",
    "next_case_id",
    "notice_from_raw",
    "outstanding_agencies",
    "parse_rss_file",
    "ping_text",
    "process_notice",
    "record_response",
    "resolve_needs_human",
    "run_followups",
    "scan",
]
