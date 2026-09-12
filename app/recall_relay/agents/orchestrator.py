"""The orchestrator: the agent that runs the coordinator's procedure.

It owns no data. Every tool reads and writes the `Store` it is handed through the invocation state, so the
model never sees the receiving ledger -- only the handful of rows `rules.score_candidates` scored above the
floor. The two judgement calls (which rows match, how to word the notice) are delegated to sub-agents
through `adjudicate_candidates` and `draft_notices`; everything else -- HOLD tags, pull-list arithmetic,
follow-up cadence, the approval gate -- is deterministic code that the model can call but not rewrite.

The run ends at `request_approval`, `dismiss_case` or `mark_needs_human`. That is rule 12, and it is
enforced by `TerminalToolGuard`, not by the system prompt alone.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from strands import Agent, tool
from strands.types.tools import ToolContext

from ..core import rules
from ..core.config import settings
from ..core.models import (
    CaseStatus,
    PullList,
    PullListItem,
    RecallCase,
    Verdict,
)
from ..core.store import Store
from . import writer as writer_mod
from .hooks import ApprovalGuard, AuditHook, TerminalToolGuard
from .mailer import send_and_record
from .matcher import adjudicate_candidates
from .model_factory import get_model

SYSTEM_PROMPT = """You run a food bank's recall procedure. A recall notice has arrived and a case is open.
Work the procedure in order and stop as soon as it is somebody else's turn.

THE PROCEDURE

1. load_case -- read the notice and the case status.
2. score_candidates -- deterministic scoring of the receiving ledger against the notice. You see only the
   rows that scored above the floor, never the ledger itself.
3. adjudicate_candidates -- run the matcher. It returns MATCH, NO_MATCH or NEEDS_HUMAN.
   - NO_MATCH  -> call dismiss_case with the matcher's reason. Done. Nobody is pinged.
   - NEEDS_HUMAN -> call mark_needs_human with the specific question a human must answer. Done.
   - MATCH -> continue.
4. build_pull_list -- computes what is still on hand, what was shipped and to whom, and applies HOLD tags.
   HOLD is automatic because it is reversible; product is held, never destroyed.
5. draft_notices -- runs the writer for each affected agency, plus client shelf signs when the class or the
   situation requires them.
6. request_approval -- hand the coordinator ONE decision: send or do not send. Then STOP.

RULES YOU DO NOT BEND

- One human decision per case. After request_approval, dismiss_case or mark_needs_human you are finished:
  do not call another tool, do not summarize at length, do not ask a second question.
- You never send anything. send_notices exists, but it is refused until a human has approved the case; the
  dashboard calls it after approval. Do not attempt it.
- Never invent a lot code, a case count, a date, or an agency. Every number comes from a tool result.
- If a tool returns an error or an empty result you did not expect, call mark_needs_human and say what was
  missing. Guessing is worse than escalating.
- recall_decisions / remember_decision are how you carry the coordinator's past rulings forward. Check them
  when a match looks like one a human already ruled on.

Keep your own text short. The artifacts are the output; your prose is not."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _state(tool_context: ToolContext) -> tuple[Store, str]:
    store: Store = tool_context.invocation_state["store"]
    case_id: str = tool_context.invocation_state["case_id"]
    return store, case_id


def _require_case(tool_context: ToolContext) -> tuple[Store, str, Optional[RecallCase]]:
    store, case_id = _state(tool_context)
    return store, case_id, store.get_case(case_id)


def _err(msg: str) -> dict:
    return {"status": "error", "content": [{"text": msg}]}


def _ok(payload: Any) -> dict:
    if isinstance(payload, str):
        return {"status": "success", "content": [{"text": payload}]}
    return {"status": "success", "content": [{"json": payload}]}


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------
@tool(context=True, name="load_case")
def load_case(tool_context: ToolContext) -> dict:
    """Read the open recall case: the notice, its classification, and the current status.

    Always the first call. Returns the firm, products, lots, distribution and disposition text, plus how
    many partner agencies the food bank serves.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    classification = rules.effective_classification(case.notice)
    return _ok(
        {
            "case_id": case.id,
            "status": case.status.value,
            "is_drill": case.is_drill,
            "source": case.notice.source.value,
            "source_url": case.notice.source_url,
            "firm": case.notice.firm,
            "recall_number": case.notice.recall_number or "pending",
            "classification": classification.value,
            "classification_assumed": case.notice.classification is None,
            "reason": case.notice.reason,
            "announcement_date": str(case.notice.announcement_date or ""),
            "publish_date": str(case.notice.publish_date or ""),
            "distribution_states": case.notice.distribution_states,
            "distribution_text": case.notice.distribution_text,
            "disposition_verbatim": case.notice.disposition_verbatim,
            "products": [p.model_dump(mode="json") for p in case.notice.products],
            "partner_agencies": len(store.list_agencies()),
            "food_bank_state": settings.food_bank_state,
        }
    )


@tool(context=True, name="score_candidates")
def score_candidates_tool(tool_context: ToolContext, top_k: int = 8) -> dict:
    """Score the receiving ledger against the notice and return only the rows above the floor.

    Deterministic: brand and product line carry the score, size confirms, the receipt-date window gates,
    and a lot code only narrows. You never see the full ledger -- just these candidate rows.

    Args:
        top_k: how many scored rows to return, highest first (default 8).
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    candidates = rules.score_candidates(store.list_receipts(), case.notice, top_k=top_k)
    start, end = rules.candidate_window(case.notice)
    return _ok(
        {
            "receipt_window": [start.isoformat(), end.isoformat()],
            "floor": rules.CANDIDATE_FLOOR,
            "ledger_rows_scanned": len(store.list_receipts()),
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }
    )


def compute_pull_list(store: Store, case: RecallCase) -> PullList:
    """Deterministic pull list from a MATCH verdict, with HOLD tags applied as a side effect.

    Separated from the tool so the service can re-run it after a coordinator resolves a NEEDS_HUMAN case
    without paying for another orchestrator turn.
    """
    items: list[PullListItem] = []
    agencies: list[str] = []
    on_hand_total = 0
    widened = case.verdict.widening_applied if case.verdict else False
    physical_sort_rows: list[int] = []
    matched = case.verdict.matched_receipt_ids if case.verdict else []

    for receipt_id in matched:
        receipt = store.get_receipt(receipt_id)
        if receipt is None:
            continue
        lot_known = bool(receipt.lot)
        if not lot_known:
            widened = True
        sort_needed = rules.physical_sort_required(receipt)
        if sort_needed:
            physical_sort_rows.append(receipt_id)

        on_hand = store.on_hand(receipt_id)
        if on_hand > 0:
            on_hand_total += on_hand
            note = "still at the food bank; HOLD tag applied"
            if not case.is_drill:
                store.set_hold(receipt_id, case.id, True)
            else:
                note = "drill: HOLD tag NOT applied to real inventory (rule 14)"
            items.append(
                PullListItem(
                    receipt_id=receipt_id,
                    agency_id=None,
                    cases=on_hand,
                    shipped_at=None,
                    lot=receipt.lot,
                    lot_known=lot_known,
                    physical_sort_required=sort_needed,
                    note=note,
                )
            )

        for dist in store.list_distributions(receipt_id):
            if dist.shipped_at < receipt.received_at:
                continue
            agency = store.get_agency(dist.agency_id)
            note = ""
            if agency is not None and agency.same_day_distribution:
                note = "same-day distribution pantry: assume it reached clients; client sign required"
            if not lot_known:
                note = (note + " " if note else "") + "receipt carried no lot, so every case is affected (rule 4)"
            if sort_needed:
                note = (note + " " if note else "") + "salvage/repack row: physical sort required (rule 7)"
            items.append(
                PullListItem(
                    receipt_id=receipt_id,
                    agency_id=dist.agency_id,
                    cases=dist.cases,
                    shipped_at=dist.shipped_at,
                    lot=receipt.lot,
                    lot_known=lot_known,
                    physical_sort_required=sort_needed,
                    note=note.strip(),
                )
            )
            if dist.agency_id not in agencies:
                agencies.append(dist.agency_id)

    shipped_total = sum(i.cases for i in items if i.agency_id)
    summary_bits = [
        f"{on_hand_total} cases on hand (HOLD tag applied)" if on_hand_total else "nothing on hand",
        (f"{shipped_total} cases shipped to {len(agencies)} " + ("agency" if len(agencies) == 1 else "agencies"))
        if shipped_total else "nothing shipped",
    ]
    if widened:
        summary_bits.append("no lot on the receipt, so the whole line is treated as affected (rule 4)")
    if physical_sort_rows:
        summary_bits.append(f"physical sort required on receipts {physical_sort_rows} (rule 7)")

    pull = PullList(
        case_id=case.id,
        on_hand_cases=on_hand_total,
        items=items,
        agencies_affected=agencies,
        widening_applied=widened,
        summary="; ".join(summary_bits),
    )
    case.pull_list = pull
    store.save_case(case)
    store.audit(case.id, "agent", "pull_list", pull.summary)
    return pull


def draft_all(
    store: Store,
    case: RecallCase,
    *,
    writer_agent: Any = None,
    sign_agent: Any = None,
    any_already_distributed: bool = False,
) -> tuple[list, list]:
    """Run the writer for every affected agency; return (agency notices, client signs)."""
    if case.pull_list is None:
        return [], []
    classification = rules.effective_classification(case.notice)
    signs_required = rules.client_notice_required(classification, any_already_distributed)

    notices = []
    signs = []
    for agency_id in case.pull_list.agencies_affected:
        agency = store.get_agency(agency_id)
        if agency is None:
            store.audit(case.id, "system", "draft_skipped", f"unknown agency {agency_id}")
            continue
        notices.append(
            writer_mod.draft_agency_notice(
                case, agency, case.pull_list.items, agent=writer_agent, store=store
            )
        )
        if signs_required:
            for language in writer_mod.sign_languages_for(agency):
                signs.append(
                    writer_mod.draft_client_sign(
                        case, agency, language, agent=sign_agent, store=store  # type: ignore[arg-type]
                    )
                )

    case.notices = notices
    case.signs = signs
    store.save_case(case)
    store.audit(
        case.id,
        "agent",
        "drafts",
        f"{len(notices)} agency notices, {len(signs)} client signs "
        f"(class {classification.value}; signs required={signs_required})",
    )
    return notices, signs


@tool(context=True, name="build_pull_list")
def build_pull_list(tool_context: ToolContext) -> dict:
    """Turn a MATCH verdict into the physical pull list, and apply HOLD tags to what is still on hand.

    For every matched receipt: what is still in the warehouse (held), and every case that already shipped,
    to which agency and on which date. A receipt with no lot means the whole line is affected (rule 4); a
    salvage or repack row is flagged for a physical sort because it has no lot lineage (rule 7).
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    if case.verdict is None or case.verdict.verdict != Verdict.MATCH:
        return _err("no MATCH verdict on this case; adjudicate_candidates first")
    return _ok(compute_pull_list(store, case).model_dump(mode="json"))


@tool(context=True, name="draft_notices")
def draft_notices(tool_context: ToolContext) -> dict:
    """Draft one notice per affected agency, plus client shelf signs where the rules require them.

    Runs the writer agent per agency. The disposition instruction is copied verbatim from the recall notice
    and the reply options are the fixed taxonomy -- both are enforced after generation, not requested in the
    prompt. Nothing is sent here.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    if case.pull_list is None:
        return _err("no pull list on this case; build_pull_list first")

    writer_agent = tool_context.invocation_state.get("writer_agent")
    sign_agent = tool_context.invocation_state.get("sign_agent", writer_agent)
    notices, signs = draft_all(store, case, writer_agent=writer_agent, sign_agent=sign_agent)
    return _ok(
        {
            "agency_notices": [
                {"agency_id": n.agency_id, "subject": n.subject, "cases_shipped": n.cases_shipped}
                for n in notices
            ],
            "client_signs": [{"agency_id": s.agency_id, "language": s.language} for s in signs],
        }
    )


@tool(context=True, name="request_approval")
def request_approval(tool_context: ToolContext) -> dict:
    """Hand the coordinator the one decision on this case: send the drafted notices, or do not.

    Call this when the pull list and the drafts are ready. The run ENDS here -- nothing is sent until a
    human approves.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    if not case.notices:
        return _err("nothing drafted yet; draft_notices first")

    case.status = CaseStatus.AWAITING_APPROVAL
    store.save_case(case)
    ping = ping_text(case)
    store.audit(case.id, "agent", "approval_requested", ping)
    return _ok({"status": case.status.value, "ping": ping, "stop": True})


@tool(context=True, name="dismiss_case")
def dismiss_case(tool_context: ToolContext, reason: str) -> dict:
    """Close the case silently: the food bank never received this product.

    Nobody is pinged and nobody is mailed, but the reason is on the record forever. The run ENDS here.

    Args:
        reason: why this recall does not touch this food bank, in one sentence.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    case.status = CaseStatus.DISMISSED
    case.dismissed_reason = reason
    case.closed_at = store.now()
    store.save_case(case)
    store.audit(case.id, "agent", "dismissed", reason)
    return _ok({"status": case.status.value, "reason": reason, "stop": True})


@tool(context=True, name="mark_needs_human")
def mark_needs_human(tool_context: ToolContext, reason: str) -> dict:
    """Escalate: the match is ambiguous and a human has to look. The run ENDS here.

    Args:
        reason: the specific question a coordinator must answer, not a general statement of doubt.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    case.status = CaseStatus.NEEDS_HUMAN
    store.save_case(case)
    store.audit(case.id, "agent", "needs_human", reason)
    return _ok({"status": case.status.value, "reason": reason, "stop": True})


@tool(context=True, name="remember_decision")
def remember_decision(tool_context: ToolContext, text: str, kind: str = "other") -> dict:
    """Record a coordinator ruling so future cases do not ask the same question twice.

    Args:
        text: the ruling, in the coordinator's own words.
        kind: brand_never_received | agency_closed | row_correction | dismissal | other.
    """
    store, case_id = _state(tool_context)
    decision = store.remember(text, kind)
    store.audit(case_id, "coordinator", "decision_remembered", f"[{decision.id}] ({kind}) {text}")
    return _ok({"id": decision.id, "kind": kind, "text": text})


@tool(context=True, name="recall_decisions")
def recall_decisions(tool_context: ToolContext, query: str = "") -> dict:
    """Look up rulings the coordinator has already made.

    Args:
        query: optional words to filter on; omit to list every decision on file.
    """
    store, _case_id = _state(tool_context)
    found = store.decisions(query)
    return _ok({"decisions": [d.model_dump(mode="json") for d in found]})


@tool(context=True, name="send_notices")
def send_notices(tool_context: ToolContext) -> dict:
    """Relay the drafted notices to the partner agencies.

    Guarded: this is refused unless a human has approved the case, and always refused on a drill. The
    dashboard calls it after approval; you should not.
    """
    store, case_id, case = _require_case(tool_context)
    if case is None:
        return _err(f"no case {case_id}")
    sent = relay_notices(store, case)
    return _ok({"sent": len(sent), "agencies": [a for a, _ in sent]})


ORCHESTRATOR_TOOLS = [
    load_case,
    score_candidates_tool,
    adjudicate_candidates,
    build_pull_list,
    draft_notices,
    request_approval,
    dismiss_case,
    mark_needs_human,
    remember_decision,
    recall_decisions,
    send_notices,
]


def build_orchestrator(
    store: Store,
    *,
    case_id: str = "",
    model: Any = None,
) -> Agent:
    """The orchestrator agent, with the audit trail and the approval gate wired in as hooks."""
    return Agent(
        model=model if model is not None else get_model("orchestrator"),
        system_prompt=SYSTEM_PROMPT,
        tools=list(ORCHESTRATOR_TOOLS),
        hooks=[AuditHook(store, case_id), ApprovalGuard(store, case_id), TerminalToolGuard(store, case_id)],
        callback_handler=None,
        name="recall-orchestrator",
    )


# ---------------------------------------------------------------------------
# The one decision, in words
# ---------------------------------------------------------------------------
def _md(d) -> str:
    """Dates the way a coordinator says them out loud: 9/3, not 2026-09-03."""
    return f"{d.month}/{d.day}"


def ping_text(case: RecallCase, store: Optional[Store] = None) -> str:
    """The single message the coordinator gets. Everything needed to answer send / do not send.

    Args:
        case: the case awaiting approval.
        store: optional; when given, the ledger line names the receipt date and full case count.
    """
    notice = case.notice
    product = "; ".join(
        " ".join(b for b in (p.brand, p.name, p.size) if b) for p in notice.products
    ) or (notice.title or "the recalled product")
    lots = [lot for p in notice.products for lot in p.lots]
    when = notice.publish_date or notice.announcement_date or notice.source_seen_at.date()
    source_label = {
        "fda_rss": "FDA press release",
        "pasted_url": "Pasted URL",
        "pasted_text": "Pasted notice",
        "uploaded_pdf": "Uploaded PDF",
        "forwarded_alert": "Forwarded alert",
        "openfda": "openFDA enforcement report",
        "drill": "Drill",
    }.get(notice.source.value, notice.source.value)

    head = f"{source_label} {_md(when)}: {product}"
    if lots:
        head += f", lot {lots[0]}" + (f" (+{len(lots) - 1} more)" if len(lots) > 1 else "")
    if notice.reason:
        head += f", {notice.reason}"
    lines = [head + "."]

    pull = case.pull_list
    if pull is not None:
        ledger_bits = []
        for rid in sorted({i.receipt_id for i in pull.items}):
            rows = [i for i in pull.items if i.receipt_id == rid]
            total = sum(i.cases for i in rows)
            received = ""
            if store is not None:
                receipt = store.get_receipt(rid)
                if receipt is not None:
                    total = receipt.cases
                    received = f" received {_md(receipt.received_at)}"
            ledger_bits.append(f"receipt {rid}, {total} cases{received}")
        if ledger_bits:
            lines.append("Your ledger: " + "; ".join(ledger_bits) + ".")
        if pull.widening_applied:
            affected = sum(i.cases for i in pull.items)
            lines.append(f"No lot on the receipt, so all {affected} are treated as affected.")
        if pull.on_hand_cases:
            lines.append(f"{pull.on_hand_cases} on hand (HOLD tag applied).")
        shipped_rows = [i for i in pull.items if i.agency_id]
        if shipped_rows:
            shipped = sum(i.cases for i in shipped_rows)
            dates = sorted({i.shipped_at for i in shipped_rows if i.shipped_at})
            span = ""
            if len(dates) == 1:
                span = f" {_md(dates[0])}"
            elif dates:
                span = f" {_md(dates[0])}-{_md(dates[-1])}"
            n_ag = len(pull.agencies_affected)
            line = f"{shipped} shipped to {n_ag} {'agency' if n_ag == 1 else 'agencies'}{span}"
            if any("same-day" in (i.note or "") for i in shipped_rows):
                line += "; one distributes same-day"
            lines.append(line + ".")
        if any(i.physical_sort_required for i in pull.items):
            lines.append("One row is salvage/repack with no lot lineage: physical sort required.")

    status = getattr(case, "status", None)
    status_value = getattr(status, "value", status)
    if status_value == CaseStatus.NEEDS_HUMAN.value:
        lines.append("One ledger row is ambiguous. Answer the question below and the run continues; nothing is sent until you do.")
    elif status_value == CaseStatus.DISMISSED.value:
        lines.append(f"Dismissed without a ping: {case.dismissed_reason or 'no ledger match'}.")
    else:
        lines.append(
            f"Pull list, {len(case.notices)} agency notice{'s' if len(case.notices) != 1 else ''}, "
            f"and {len(case.signs)} shelf sign{'s' if len(case.signs) != 1 else ''} drafted. Send?"
        )
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Deterministic relay: shared by the send_notices tool and the service facade.
# ---------------------------------------------------------------------------
def relay_notices(store: Store, case: RecallCase) -> list[tuple[str, str]]:
    """Mail every drafted agency notice, issue a response token, and schedule the follow-ups.

    Returns a list of (agency_id, token). Callers reaching this through the `send_notices` tool have
    already passed `ApprovalGuard`; `service.approve` calls it directly after setting `approved_at`.
    """
    classification = rules.effective_classification(case.notice)
    reminder_h, escalate_h = rules.followup_cadence(classification)
    now = store.now()
    sent: list[tuple[str, str]] = []

    for notice in case.notices:
        agency = store.get_agency(notice.agency_id)
        if agency is None:
            store.audit(case.id, "system", "send_skipped", f"unknown agency {notice.agency_id}")
            continue
        token = store.issue_token(case.id, agency.id)
        response_url = f"{settings.public_base_url.rstrip('/')}/r/{token}"
        subject, body = writer_mod.render_notice_email(notice, response_url=response_url)
        send_and_record(
            store,
            case_id=case.id,
            agency_id=agency.id,
            to_addr=agency.contact_email,
            subject=subject,
            body=body,
            kind="notice",
            token=token,
        )
        store.schedule_followup(case.id, agency.id, now + timedelta(hours=reminder_h), "reminder")
        if escalate_h is not None:
            store.schedule_followup(case.id, agency.id, now + timedelta(hours=escalate_h), "escalation")
        store.audit(
            case.id,
            "agent",
            "notice_sent",
            f"{agency.id} <{agency.contact_email}> :: {subject} "
            f"(reminder +{reminder_h}h, escalation "
            f"{'+' + str(escalate_h) + 'h' if escalate_h is not None else 'none'})",
        )
        sent.append((agency.id, token))
    return sent


__all__ = [
    "ORCHESTRATOR_TOOLS",
    "SYSTEM_PROMPT",
    "build_orchestrator",
    "build_pull_list",
    "compute_pull_list",
    "dismiss_case",
    "draft_all",
    "draft_notices",
    "load_case",
    "mark_needs_human",
    "ping_text",
    "recall_decisions",
    "relay_notices",
    "remember_decision",
    "request_approval",
    "score_candidates_tool",
    "send_notices",
]
