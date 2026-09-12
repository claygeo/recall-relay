"""View models: everything the templates and the PDF read, assembled here so the two renderings of the
audit packet (HTML and PDF) cannot drift apart.

Read-only. Nothing in this module writes to the Store -- the packet page must be viewable before a case is
closed without closing it as a side effect.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..core import rules
from ..core.config import settings
from ..core.models import (
    Agency,
    CaseStatus,
    RecallCase,
)
from ..core.store import Store

# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
STATUS_LABELS: dict[str, str] = {
    "new": "New",
    "dismissed": "Dismissed",
    "needs_human": "Needs human",
    "awaiting_approval": "Awaiting approval",
    "relaying": "Relaying",
    "chasing": "Chasing",
    "closed": "Closed",
}

# which semantic dot a status carries. vermilion is reserved for needs_human / awaiting_approval.
STATUS_TONE: dict[str, str] = {
    "new": "info",
    "dismissed": "muted",
    "needs_human": "accent",
    "awaiting_approval": "accent",
    "relaying": "info",
    "chasing": "warn",
    "closed": "ok",
}

RESPONSE_LABELS: dict[str, str] = {
    "pulled": "Pulled",
    "never_received": "Never received",
    "already_distributed": "Already distributed",
    "need_pickup": "Needs pickup",
}

RESPONSE_TONE: dict[str, str] = {
    "pulled": "ok",
    "never_received": "info",
    "already_distributed": "err",
    "need_pickup": "warn",
}


def fmt_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d")


def fmt_stamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%Y-%m-%d %H:%M")


def humanize(delta: timedelta) -> str:
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


def product_headline(case: RecallCase) -> str:
    """The one 38px line on the case page: what was recalled, in a coordinator's words."""
    lines = [
        " ".join(bit for bit in (p.brand, p.name, p.size) if bit).strip()
        for p in case.notice.products
    ]
    lines = [line for line in lines if line]
    if not lines:
        return case.notice.title or case.notice.firm or case.id
    head = lines[0]
    if len(lines) > 1:
        head += f" (+{len(lines) - 1} more)"
    return head


def clock_offset_hours(store: Store) -> float:
    """How far the demo clock has been pushed past real time."""
    delta = store.now() - datetime.now(timezone.utc)
    return round(delta.total_seconds() / 3600.0, 1)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------
def ledger_view(store: Store, *, dist_page: int = 1, dist_per_page: int = 40) -> dict:
    receipts = store.list_receipts()
    agencies = {a.id: a for a in store.list_agencies()}
    holds = store.holds()
    shipped: dict[int, int] = {}
    for d in store.list_distributions():
        shipped[d.receipt_id] = shipped.get(d.receipt_id, 0) + d.cases

    rows = []
    for r in receipts:
        rows.append(
            {
                "receipt": r,
                "on_hand": max(r.cases - shipped.get(r.id, 0), 0),
                "hold_case_id": holds.get(r.id, ""),
            }
        )

    dists = store.list_distributions()
    total_dist = len(dists)
    pages = max((total_dist + dist_per_page - 1) // dist_per_page, 1)
    page = min(max(dist_page, 1), pages)
    start = (page - 1) * dist_per_page
    window = dists[start : start + dist_per_page]
    dist_rows = []
    for d in window:
        receipt = store.get_receipt(d.receipt_id)
        agency = agencies.get(d.agency_id)
        dist_rows.append(
            {
                "dist": d,
                "product": receipt.product if receipt else f"receipt {d.receipt_id}",
                "brand": receipt.brand if receipt else "",
                "agency_name": agency.name if agency else d.agency_id,
            }
        )

    return {
        "rows": rows,
        "counts": {
            "receipts": len(receipts),
            "agencies": len(agencies),
            "distributions": total_dist,
            "holds": len(holds),
            "on_hand": sum(row["on_hand"] for row in rows),
        },
        "dist_rows": dist_rows,
        "dist_page": page,
        "dist_pages": pages,
        "dist_from": start + 1 if window else 0,
        "dist_to": start + len(window),
        "agencies": list(agencies.values()),
    }


# ---------------------------------------------------------------------------
# one case
# ---------------------------------------------------------------------------
def _agency_rows(store: Store, case: RecallCase) -> list[dict]:
    """agency · cases · shipped · status · last response · next follow-up due."""
    ids: list[str] = []
    if case.pull_list is not None:
        ids = list(case.pull_list.agencies_affected)
    for n in case.notices:
        if n.agency_id not in ids:
            ids.append(n.agency_id)

    pending: dict[str, list[dict]] = {}
    for f in store.followups(case.id):
        if f.get("sent_at") is None:
            pending.setdefault(f["agency_id"], []).append(f)

    out: list[dict] = []
    for agency_id in ids:
        agency: Optional[Agency] = store.get_agency(agency_id)
        items = [i for i in (case.pull_list.items if case.pull_list else []) if i.agency_id == agency_id]
        cases_n = sum(i.cases for i in items)
        dates = sorted({i.shipped_at for i in items if i.shipped_at})
        response = store.latest_response(case.id, agency_id)
        nxt = sorted(pending.get(agency_id, []), key=lambda f: f["due_at"])
        out.append(
            {
                "agency": agency,
                "agency_id": agency_id,
                "name": agency.name if agency else agency_id,
                "cases": cases_n,
                "shipped": " · ".join(fmt_date(d) for d in dates),
                "same_day": bool(agency and agency.same_day_distribution),
                "response": response,
                "response_label": RESPONSE_LABELS.get((response or {}).get("status", ""), ""),
                "response_tone": RESPONSE_TONE.get((response or {}).get("status", ""), "muted"),
                "responded_at": fmt_stamp((response or {}).get("at")),
                "next_followup": nxt[0] if nxt else None,
                "next_followup_due": fmt_stamp(nxt[0]["due_at"]) if nxt else "",
                "next_followup_kind": nxt[0]["kind"] if nxt else "",
            }
        )
    return out


def _pull_rows(store: Store, case: RecallCase) -> list[dict]:
    if case.pull_list is None:
        return []
    out = []
    for item in case.pull_list.items:
        agency = store.get_agency(item.agency_id) if item.agency_id else None
        out.append(
            {
                "item": item,
                "where": agency.name if agency else ("on hand" if not item.agency_id else item.agency_id),
                "on_hand": item.agency_id is None,
                "widening": (not item.lot_known),
            }
        )
    return out


def case_view(store: Store, case: RecallCase, *, ping: str = "") -> dict:
    classification = rules.effective_classification(case.notice)
    lots = [lot for p in case.notice.products for lot in p.lots]
    best_by = [b for p in case.notice.products for b in p.best_by]
    upcs = [u for p in case.notice.products for u in p.upcs_as_printed]

    signs_by_lang: dict[str, list] = {}
    for sign in case.signs:
        signs_by_lang.setdefault(sign.language, []).append(sign)

    return {
        "case": case,
        "headline": product_headline(case),
        "ping": ping,
        "classification": classification.value,
        "class_is_assumed": case.notice.classification is None,
        "lots": lots,
        "best_by": best_by,
        "upcs": upcs,
        "pull_rows": _pull_rows(store, case),
        "agency_rows": _agency_rows(store, case),
        "status_label": STATUS_LABELS.get(case.status.value, case.status.value),
        "status_tone": STATUS_TONE.get(case.status.value, "muted"),
        "outbox": store.outbox(case.id),
        "signs_by_lang": signs_by_lang,
        "events": store.events(case.id),
        "clock_offset": clock_offset_hours(store),
        "can_approve": case.status == CaseStatus.AWAITING_APPROVAL and not case.is_drill,
        "can_resolve": case.status == CaseStatus.NEEDS_HUMAN,
        "can_close": case.status in (CaseStatus.RELAYING, CaseStatus.CHASING),
        "is_closed": case.status == CaseStatus.CLOSED,
        "response_options": rules.RESPONSE_OPTIONS,
    }


def cases_view(store: Store) -> dict:
    cases = store.list_cases()
    rows = []
    for c in cases:
        rows.append(
            {
                "case": c,
                "headline": product_headline(c),
                "status_label": STATUS_LABELS.get(c.status.value, c.status.value),
                "status_tone": STATUS_TONE.get(c.status.value, "muted"),
                "created": fmt_stamp(c.created_at),
                "recall_number": c.notice.recall_number,
                "firm": c.notice.firm,
                "matched": len(c.verdict.matched_receipt_ids) if c.verdict else 0,
                "agencies": len(c.notices),
            }
        )
    by_status: dict[str, int] = {}
    for c in cases:
        by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
    return {"rows": rows, "total": len(cases), "by_status": by_status}


# ---------------------------------------------------------------------------
# the inbox mirror
# ---------------------------------------------------------------------------
def inbox_view(store: Store, *, agency_id: str = "") -> dict:
    agencies = {a.id: a for a in store.list_agencies()}
    mail = store.outbox(agency_id=agency_id or None)
    mail = list(reversed(mail))  # newest first, the way an inbox reads

    counts: dict[str, int] = {}
    for m in store.outbox():
        key = m.get("agency_id") or ""
        counts[key] = counts.get(key, 0) + 1

    rows = []
    for m in mail:
        agency = agencies.get(m.get("agency_id") or "")
        rows.append(
            {
                "mail": m,
                "agency": agency,
                "agency_name": agency.name if agency else (m.get("to_addr") or "coordinator"),
                "sent": fmt_stamp(m.get("sent_at")),
                "kind": m.get("kind", ""),
                "body_lines": (m.get("body") or "").split("\n"),
            }
        )
    return {
        "rows": rows,
        "agencies": list(agencies.values()),
        "counts": counts,
        "selected": agencies.get(agency_id) if agency_id else None,
        "selected_id": agency_id,
        "total": len(store.outbox()),
    }


# ---------------------------------------------------------------------------
# the audit packet (same numbers for the HTML page and the PDF)
# ---------------------------------------------------------------------------
def packet_view(store: Store, case: RecallCase) -> dict:
    responses = store.responses(case.id)
    followups = store.followups(case.id)
    events = store.events(case.id)
    outbox = store.outbox(case.id)
    notices_sent = sum(1 for m in outbox if m["kind"] == "notice")

    sources = [s for s in [case.notice.source_url] if s]
    if case.notice.recall_number:
        sources.append(f"openFDA recall {case.notice.recall_number}")

    approvals: list[str] = []
    if case.approved_at is not None:
        approvals.append(f"coordinator approved relay at {fmt_stamp(case.approved_at)}")
    for event in events:
        if event.actor == "coordinator" and event.kind in ("dismissed", "decision_remembered"):
            approvals.append(f"{event.kind} at {fmt_stamp(event.at)}: {event.detail}")
    if not approvals:
        approvals.append("no coordinator decision recorded yet")

    elapsed = ""
    last = max((datetime.fromisoformat(r["at"]) for r in responses), default=None)
    if last is not None:
        elapsed = humanize(last - case.notice.source_seen_at)

    matched_rows = []
    for rid in (case.verdict.matched_receipt_ids if case.verdict else []):
        receipt = store.get_receipt(rid)
        if receipt is not None:
            matched_rows.append(
                {
                    "receipt": receipt,
                    "on_hand": store.on_hand(rid),
                    "shipped": sum(d.cases for d in store.list_distributions(rid)),
                }
            )

    timeline = []
    for event in events:
        timeline.append(
            {
                "at": fmt_stamp(event.at),
                "actor": event.actor,
                "kind": event.kind,
                "detail": event.detail,
            }
        )

    response_rows = []
    for r in responses:
        agency = store.get_agency(r["agency_id"])
        response_rows.append(
            {
                "at": fmt_stamp(r["at"]),
                "agency_id": r["agency_id"],
                "agency_name": agency.name if agency else r["agency_id"],
                "status": r["status"],
                "label": RESPONSE_LABELS.get(r["status"], r["status"]),
                "count": r["count"],
                "free_text": r["free_text"] or "",
            }
        )

    followup_rows = []
    for f in followups:
        agency = store.get_agency(f["agency_id"])
        followup_rows.append(
            {
                "agency_id": f["agency_id"],
                "agency_name": agency.name if agency else f["agency_id"],
                "kind": f["kind"],
                "due": fmt_stamp(f["due_at"]),
                "sent": fmt_stamp(f["sent_at"]) if f["sent_at"] else "",
            }
        )

    outstanding = []
    responded = {r["agency_id"] for r in responses}
    for row in _agency_rows(store, case):
        if row["agency_id"] not in responded:
            outstanding.append(row["name"])

    return {
        "case": case,
        "headline": product_headline(case),
        "recall_number": case.notice.recall_number or "pending",
        "classification": rules.effective_classification(case.notice).value,
        "generated_at": fmt_stamp(store.now()),
        "food_bank": settings.food_bank_name,
        "sources": sources,
        "matched_rows": matched_rows,
        "pull_rows": _pull_rows(store, case),
        "pull_list": case.pull_list,
        "notices_sent": notices_sent,
        "responses": response_rows,
        "followups": followup_rows,
        "outstanding": outstanding,
        "disposition_verbatim": case.notice.disposition_verbatim,
        "approvals": approvals,
        "elapsed": elapsed,
        "timeline": timeline,
        "status_label": STATUS_LABELS.get(case.status.value, case.status.value),
        "is_closed": case.status == CaseStatus.CLOSED,
        "closed_at": fmt_stamp(case.closed_at) if case.closed_at else "",
    }


__all__ = [
    "RESPONSE_LABELS",
    "RESPONSE_TONE",
    "STATUS_LABELS",
    "STATUS_TONE",
    "case_view",
    "cases_view",
    "clock_offset_hours",
    "fmt_date",
    "fmt_stamp",
    "humanize",
    "inbox_view",
    "ledger_view",
    "packet_view",
    "product_headline",
]
