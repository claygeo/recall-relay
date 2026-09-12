"""Writer: drafts the partner-agency notice and the client-facing shelf sign.

Two agents-as-tools. The model writes the prose; code owns the facts. Specifically:

* `disposition_verbatim` is compared against the notice after generation and OVERWRITTEN if the model
  touched it (rule 11). "Return to the place of purchase for a full refund" is a legal instruction from the
  recalling firm, not a sentence for an LLM to improve.
* `actions` is always exactly `rules.RESPONSE_OPTIONS`. The response taxonomy is closed (rule 9), so what
  the agency can reply is never up to the writer.
* the body must cite the recall number (or "pending") and the source URL; if the draft omits either, a
  provenance line is appended rather than the draft being silently shipped.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Literal, Optional, Sequence

from strands import Agent

from ..core import rules
from ..core.config import settings
from ..core.models import (
    Agency,
    AgencyNotice,
    ClientSign,
    PullListItem,
    RecallCase,
)
from ..core.store import Store
from .model_factory import get_model

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "ht": "Haitian Creole"}

NOTICE_SYSTEM_PROMPT = """You write recall notices from a food bank to its partner pantries. Your reader is
a volunteer coordinator at a church pantry or a soup kitchen who has ten minutes between distributions.

How to write:

- Plain language, short sentences, no jargon, no marketing. Never "we regret to inform you".
- Lead with what they must do and which product. The first two lines must be actionable standing up.
- State exactly what we shipped them: how many cases, on which dates.
- Copy the disposition instruction from the recall notice WORD FOR WORD. Do not soften it, do not
  translate it into your own words, do not add or remove a step. If it says return for a full refund, it
  says that.
- Cite the recall number (or say the recall number is pending) and include the source URL so they can read
  the original.
- Do not promise anything the food bank has not agreed to: no pickup times, no replacement product, no
  reimbursement, unless the input says so.
- Do not invent lot codes, dates, case counts, or symptoms.
- Say plainly that they should reply even if they never received it, because a closed loop is the point.
- Subject line: product, the word RECALL, and the class. Under 80 characters.

Tone: a colleague telling a colleague something urgent and boring. Calm, specific, finishable."""

SIGN_SYSTEM_PROMPT = """You write the paper sign a pantry tapes to the shelf and hands to clients. Your
reader may be reading in a second language, may be in a hurry, may be embarrassed to ask a question.

How to write:

- Sixth-grade reading level. Short words. No agency jargon, no "voluntary recall of the following lots".
- Say what the product is in the words a person would use, including the size on the package.
- Say what to do with it in one sentence, matching the recall instruction.
- List the symptoms plainly, and say when to call a doctor.
- Never blame the client, never imply they did something wrong, never suggest they will lose benefits or
  access. They came here for food; this sign must not make that harder.
- Do not invent symptoms, deadlines, or a hotline that was not given to you.
- Do not promise anything. No replacement, no swap, no refund from the pantry, no voucher, no delivery. The
  recall notice says what happens to the product and that is the only offer on the sign. A sign that
  promises a swap sends a family back on the bus for nothing.
- Write the whole sign in the requested language. Do not mix languages and do not leave English headings
  in a Spanish or Haitian Creole sign."""


def build_writer_agent() -> Agent:
    """The drafting agent. Patched out in tests."""
    return Agent(
        model=get_model("writer"),
        system_prompt=NOTICE_SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
        name="recall-writer",
    )


def build_sign_agent() -> Agent:
    """The client-sign agent: a different reader, so a different system prompt."""
    return Agent(
        model=get_model("writer"),
        system_prompt=SIGN_SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
        name="recall-sign-writer",
    )


# ---------------------------------------------------------------------------
# prompt construction
# ---------------------------------------------------------------------------
def _product_summary(case: RecallCase) -> str:
    parts = []
    for p in case.notice.products:
        bits = [b for b in (p.brand, p.name, p.size) if b]
        parts.append(" ".join(bits))
    return "; ".join(parts) or case.notice.title or "the recalled product"


def _lots(case: RecallCase) -> list[str]:
    out: list[str] = []
    for p in case.notice.products:
        out.extend(p.lots)
    return list(dict.fromkeys(out))


def _best_by(case: RecallCase) -> list[str]:
    out: list[str] = []
    for p in case.notice.products:
        out.extend(p.best_by)
    return list(dict.fromkeys(out))


def notice_prompt(case: RecallCase, agency: Agency, items: Sequence[PullListItem]) -> str:
    shipped = [i for i in items if i.agency_id == agency.id]
    cases_shipped = sum(i.cases for i in shipped)
    dates = sorted({i.shipped_at.isoformat() for i in shipped if i.shipped_at})
    widened = any(not i.lot_known for i in shipped)
    physical_sort = any(i.physical_sort_required for i in shipped)
    classification = rules.effective_classification(case.notice)
    payload = {
        "food_bank": settings.food_bank_name,
        "agency": {
            "id": agency.id,
            "name": agency.name,
            "kind": agency.kind,
            "contact_name": agency.contact_name,
            "open_schedule": agency.open_schedule,
            "same_day_distribution": agency.same_day_distribution,
        },
        "recall": {
            "firm": case.notice.firm,
            "recall_number": case.notice.recall_number or "pending",
            "classification": classification.value
            + ("" if case.notice.classification else " (unclassified press release, handled as Class I)"),
            "class_definition": rules.CLASS_DEFINITIONS.get(classification, ""),
            "reason": case.notice.reason,
            "product": _product_summary(case),
            "lots": _lots(case),
            "best_by": _best_by(case),
            "source_url": case.notice.source_url,
            "announced": str(case.notice.announcement_date or case.notice.publish_date or ""),
            "disposition_verbatim": case.notice.disposition_verbatim,
        },
        "what_we_shipped_you": {
            "cases": cases_shipped,
            "dates": dates,
            "receipt_ids": sorted({i.receipt_id for i in shipped}),
        },
        "flags": {
            "lot_unknown_so_whole_line_affected": widened,
            "physical_sort_required_no_lot_lineage": physical_sort,
        },
        "reply_options": [o.label for o in rules.RESPONSE_OPTIONS],
    }
    return (
        "Draft the recall notice email to this partner agency.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nReturn the structured notice. Copy disposition_verbatim exactly as given above. "
        "Set cases_shipped and shipped_dates from what_we_shipped_you. "
        "The body must name the recall number and include the source URL."
    )


def sign_prompt(case: RecallCase, agency: Agency, language: str) -> str:
    payload = {
        "language": language,
        "language_name": LANGUAGE_NAMES.get(language, language),
        "product": _product_summary(case),
        "lots": _lots(case),
        "best_by": _best_by(case),
        "hazard": case.notice.reason,
        "disposition_verbatim": case.notice.disposition_verbatim,
        "source_url": case.notice.source_url,
        "agency": {
            "name": agency.name,
            "contact_name": agency.contact_name,
            "contact_phone": agency.contact_phone,
            "open_schedule": agency.open_schedule,
        },
    }
    return (
        "Write the shelf sign clients will read.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + f"\n\nWrite every field in {LANGUAGE_NAMES.get(language, language)}."
    )


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------
def draft_agency_notice(
    case: RecallCase,
    agency: Agency,
    items: Sequence[PullListItem],
    *,
    agent: Optional[Agent] = None,
    store: Optional[Store] = None,
) -> AgencyNotice:
    """Draft one partner-agency notice, then repair anything the model was not allowed to change.

    Args:
        case: the recall case being relayed.
        agency: the partner agency receiving the notice.
        items: pull-list items (the full list; rows for other agencies are filtered out here).
        agent: injected writer agent, for tests.
        store: when given, corrections are written to the audit trail.
    """
    runner = agent or build_writer_agent()
    result = runner(notice_prompt(case, agency, items), structured_output_model=AgencyNotice)
    draft: Optional[AgencyNotice] = result.structured_output
    if draft is None:  # pragma: no cover - model refused to fill the shape
        raise RuntimeError("writer returned no structured output for the agency notice")
    return _repair_notice(draft, case, agency, items, store=store)


def _repair_notice(
    draft: AgencyNotice,
    case: RecallCase,
    agency: Agency,
    items: Sequence[PullListItem],
    *,
    store: Optional[Store] = None,
) -> AgencyNotice:
    shipped = [i for i in items if i.agency_id == agency.id]
    verbatim = case.notice.disposition_verbatim
    recall_number = case.notice.recall_number or "pending"
    url = case.notice.source_url

    corrections: list[str] = []

    if draft.disposition_verbatim != verbatim:
        corrections.append(
            "disposition rewritten by the model; restored verbatim from the notice (rule 11). "
            f"model wrote: {draft.disposition_verbatim[:200]!r}"
        )

    body = draft.body or ""
    if recall_number.lower() not in body.lower():
        body = body.rstrip() + f"\n\nRecall number: {recall_number}."
        corrections.append("body did not cite the recall number; provenance line appended")
    if url and url not in body:
        body = body.rstrip() + f"\nSource notice: {url}"
        corrections.append("body did not cite the source URL; provenance line appended")
    if verbatim and verbatim not in body:
        body = body.rstrip() + f"\n\nFrom the recall notice, word for word: {verbatim}"
        corrections.append("body did not carry the verbatim disposition; appended (rule 11)")

    fixed = draft.model_copy(
        update={
            "agency_id": agency.id,
            "recall_number": case.notice.recall_number,
            "classification": rules.effective_classification(case.notice).value,
            "disposition_verbatim": verbatim,
            "actions": list(rules.RESPONSE_OPTIONS),
            "cases_shipped": sum(i.cases for i in shipped),
            "shipped_dates": sorted({i.shipped_at.isoformat() for i in shipped if i.shipped_at}),
            "lots": _lots(case) or list(draft.lots),
            "best_by": _best_by(case) or list(draft.best_by),
            "body": body,
        }
    )

    if store is not None:
        for c in corrections:
            store.audit(case.id, "system", "writer_correction", f"{agency.id}: {c}")
    return fixed


def draft_client_sign(
    case: RecallCase,
    agency: Agency,
    language: Literal["en", "es", "ht"] = "en",
    *,
    agent: Optional[Agent] = None,
    store: Optional[Store] = None,
) -> ClientSign:
    """Draft the shelf sign a pantry hands to clients, in en / es / ht."""
    runner = agent or build_sign_agent()
    result = runner(sign_prompt(case, agency, language), structured_output_model=ClientSign)
    draft: Optional[ClientSign] = result.structured_output
    if draft is None:  # pragma: no cover - model refused to fill the shape
        raise RuntimeError("writer returned no structured output for the client sign")

    fixed = draft.model_copy(
        update={
            "agency_id": agency.id,
            "language": language,
            "source_url": case.notice.source_url,
        }
    )
    if store is not None and (draft.agency_id != agency.id or draft.language != language):
        store.audit(
            case.id,
            "system",
            "writer_correction",
            f"{agency.id}: sign agency/language corrected to {agency.id}/{language}",
        )
    return fixed


def sign_languages_for(agency: Agency) -> list[str]:
    """Which sign languages this agency needs. Unknown languages fall back to English."""
    wanted = [lang for lang in (agency.languages or ["en"]) if lang in LANGUAGE_NAMES]
    return wanted or ["en"]


def render_notice_email(notice: AgencyNotice, *, response_url: str = "") -> tuple[str, str]:
    """Turn a drafted notice into the (subject, body) actually mailed.

    The response block is appended here, deterministically, so the coordinator's approval preview and the
    sent mail differ only by the agency's unique response link.
    """
    lines = [notice.body.rstrip(), "", "How to reply (pick one):"]
    for option in notice.actions:
        lines.append(f"  - {option.label}")
    if response_url:
        lines += ["", f"One-click reply: {response_url}"]
    return notice.subject, "\n".join(lines)


def render_sign_email(sign: ClientSign, case: RecallCase) -> tuple[str, str]:
    """The client sign, as a mailable/printable block."""
    subject = f"[{sign.language.upper()}] Shelf sign for clients - {sign.product_line}"
    body = "\n".join(
        [
            sign.title,
            "",
            sign.product_line,
            "",
            sign.what_to_do,
            "",
            f"Symptoms: {sign.symptoms}",
            "",
            f"Questions: {sign.agency_contact}",
            "",
            f"Source: {sign.source_url or case.notice.source_url}",
        ]
    )
    return subject, body


def _today(case: RecallCase) -> date:
    return (case.notice.announcement_date or case.notice.publish_date or case.created_at.date())


__all__ = [
    "NOTICE_SYSTEM_PROMPT",
    "SIGN_SYSTEM_PROMPT",
    "build_sign_agent",
    "build_writer_agent",
    "draft_agency_notice",
    "draft_client_sign",
    "notice_prompt",
    "render_notice_email",
    "render_sign_email",
    "sign_languages_for",
    "sign_prompt",
]
