"""Matcher: the one judgement call in the procedure, wrapped as an agent-as-tool.

`rules.score_candidates` does the arithmetic and hands over at most a handful of ledger rows. This agent
decides which of them are actually the recalled product, using the coordinator's own rules -- and it is
allowed to say "I do not know", which is the whole point (rule 5).

Everything around the model call is deterministic:

* zero candidates -> NO_MATCH with no model call at all (you do not pay a model to say "nothing scored");
* the returned receipt ids are intersected with the ids we actually offered, so the model cannot invent a
  row;
* MATCH with an empty id list is downgraded to NEEDS_HUMAN;
* widening (rule 4) is recomputed from the candidates, not taken on the model's word.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

from strands import Agent, tool
from strands.types.tools import ToolContext

from ..core import rules
from ..core.models import Candidate, Decision, MatchVerdict, RecallNotice, Verdict
from ..core.store import Store
from .model_factory import get_model

SYSTEM_PROMPT = """You are the matching step of a food bank's recall procedure. You are given a recall
notice and a short list of receiving-ledger rows that already scored above the floor. You decide which rows
are the recalled product.

The coordinator's rules, which you follow exactly:

RULE 3 -- MATCH HIERARCHY. Brand + product line + size decide the match. The receipt-date window has
already been applied. A lot code only NARROWS a match that brand/product/size already made; a lot never
makes a match on its own, and a lot mismatch on an otherwise strong row is a reason to look harder, not an
automatic rejection. Size is the sharpest discriminator you have: "Organic Triple Berry Blend 10 oz" and
"Mixed Berries 16 oz" are different products even from the same brand.

RULE 4 -- MISSING LOT WIDENS, NEVER DROPS. If the ledger row carries no lot and the row otherwise matches,
the row MATCHES and every case on it is affected. Set widening_applied=true and say so in the evidence.
Never reason "the receipt has no lot, so we cannot tell, so skip it". The food bank cannot tell either,
which is exactly why the whole line comes off the shelf.

RULE 4 IS ABOUT LOTS, NOT ABOUT IDENTITY. A missing lot widens a line you have ALREADY identified. A
missing brand is a different thing entirely: it means you do not know whose product the row is. If the
ledger row carries no brand and the notice does not pin the product some other way (a UPC, a distinctive
product name only that firm sells), then "no brand on either side" is NOT "no conflict" -- it is an
unidentified row. A commodity line like frozen blueberries, rice or shredded cheese is sold by a dozen
suppliers into the same pantry, so an unbranded row that matches only on product name and size is exactly
the ambiguity rule 5 was written for. Return NEEDS_HUMAN and ask which supplier that row came from. Do not
reason "neither side has a brand, therefore they agree".

RULE 5 -- AMBIGUITY IS NEEDS_HUMAN, NEVER NO_MATCH. If you are genuinely unsure -- a plausible brand with
the wrong size, a product name that could be two different items, a firm that also supplies a different
brand -- return NEEDS_HUMAN and name the specific question a human should answer. NO_MATCH is only for
rows you are confident are a different product. Defaulting to NO_MATCH is the expensive failure: it means
recalled food stays on a pantry shelf and nobody is told.

RULE 7 -- SALVAGE ROWS. Rule 7 applies to exactly one thing: a candidate whose `channel` field is
"salvage". Those rows are reclamation / repack and have no lot lineage at all, so when one matches, say in
the evidence that a physical sort is required. Do NOT claim a physical sort for any other channel --
"retail_rescue", "purchase", "tefap" and "food_drive" are ordinary rows. A missing lot on an ordinary row
is rule 4 (widening), not rule 7. The candidate's `note` field already tells you which rule the row
triggers; do not invent a second one.

REMEMBERED DECISIONS. The coordinator's past decisions are given to you verbatim. They OVERRIDE your own
reading of the row. If a decision says a specific ledger row was a different product or a different
supplier, that row is NO_MATCH -- exclude it and cite the decision in the evidence.

Output: verdict, matched_receipt_ids (only ids from the candidate list), one evidence line per candidate
you accepted or rejected saying why, a confidence between 0 and 1, widening_applied, and a one-sentence
reason. Cite ledger rows by receipt id."""


def build_matcher_agent() -> Agent:
    """The adjudicating agent. Patched out in tests."""
    return Agent(
        model=get_model("matcher"),
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
        name="recall-matcher",
    )


def _candidate_block(candidates: Sequence[Candidate]) -> str:
    rows = []
    for c in candidates:
        rows.append(
            json.dumps(
                {
                    "receipt_id": c.receipt_id,
                    "brand": c.brand,
                    "product": c.product,
                    "size": c.size,
                    "lot_on_receipt": c.lot or None,
                    "received_at": c.received_at.isoformat(),
                    "cases": c.cases,
                    "channel": c.channel.value,
                    "scores": {
                        "brand": c.brand_score,
                        "product": c.product_score,
                        "size": c.size_score,
                        "composite": c.deterministic_score,
                    },
                    "in_receipt_window": c.in_window,
                    "lot_relation": c.lot_relation,
                    "note": c.note,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(rows)


def _notice_block(notice: RecallNotice) -> str:
    unclassified = "" if notice.classification else " (unclassified press release, handled as Class I)"
    lines = [
        f"firm: {notice.firm}",
        f"recall number: {notice.recall_number or '(pending)'}",
        f"classification: {rules.effective_classification(notice).value}{unclassified}",
        f"reason: {notice.reason}",
        f"announced: {notice.announcement_date or notice.publish_date or notice.source_seen_at.date()}",
        f"distribution: {notice.distribution_text or ', '.join(notice.distribution_states) or '(unstated)'}",
        "products:",
    ]
    for p in notice.products:
        lines.append(
            "  - "
            + json.dumps(
                {
                    "brand": p.brand,
                    "name": p.name,
                    "size": p.size,
                    "upcs_as_printed": p.upcs_as_printed,
                    "lots": p.lots,
                    "best_by": p.best_by,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _decision_block(decisions: Sequence[Decision]) -> str:
    if not decisions:
        return "(none on file)"
    return "\n".join(f"  [{d.id}] ({d.kind}) {d.text}" for d in decisions)


def build_prompt(notice: RecallNotice, candidates: Sequence[Candidate], decisions: Sequence[Decision]) -> str:
    """The exact text the matcher sees. Exposed so tests can assert what reached the model."""
    return (
        "=== RECALL NOTICE ===\n"
        + _notice_block(notice)
        + "\n\n=== CANDIDATE LEDGER ROWS (one JSON object per row; these are the ONLY rows you may cite) ===\n"
        + _candidate_block(candidates)
        + "\n\n=== COORDINATOR DECISIONS ON FILE (verbatim; these override your reading) ===\n"
        + _decision_block(decisions)
        + "\n\n=== END ===\nWhich of these ledger rows are the recalled product?"
    )


def adjudicate(
    notice: RecallNotice,
    candidates: Sequence[Candidate],
    decisions: Sequence[Decision] = (),
    *,
    agent: Optional[Agent] = None,
) -> MatchVerdict:
    """Decide whether the food bank received the recalled product.

    Deterministic before and after the model: no candidates means no model call, and the model's answer is
    validated back against the rows we actually offered.
    """
    candidates = list(candidates)
    if not candidates:
        return MatchVerdict(
            verdict=Verdict.NO_MATCH,
            matched_receipt_ids=[],
            evidence=[],
            confidence=0.95,
            widening_applied=False,
            reason="no candidate above floor",
        )

    runner = agent or build_matcher_agent()
    result = runner(
        build_prompt(notice, candidates, list(decisions)),
        structured_output_model=MatchVerdict,
    )
    verdict: Optional[MatchVerdict] = result.structured_output
    if verdict is None:  # pragma: no cover - model refused to fill the shape
        return MatchVerdict(
            verdict=Verdict.NEEDS_HUMAN,
            matched_receipt_ids=[],
            evidence=[],
            confidence=0.0,
            widening_applied=False,
            reason="matcher returned no structured output; escalating rather than guessing (rule 5)",
        )

    return validate_verdict(verdict, candidates)


def validate_verdict(verdict: MatchVerdict, candidates: Sequence[Candidate]) -> MatchVerdict:
    """Post-validation. The model proposes; this function decides what the case records."""
    by_id = {c.receipt_id: c for c in candidates}
    kept = [rid for rid in dict.fromkeys(verdict.matched_receipt_ids) if rid in by_id]
    dropped = [rid for rid in verdict.matched_receipt_ids if rid not in by_id]

    evidence = list(verdict.evidence)
    for rid in dropped:
        evidence.append(f"receipt {rid}: dropped, not among the candidate rows offered to the matcher")

    out = verdict.model_copy(update={"matched_receipt_ids": kept, "evidence": evidence})

    if out.verdict == Verdict.MATCH and not kept:
        return out.model_copy(
            update={
                "verdict": Verdict.NEEDS_HUMAN,
                "confidence": min(out.confidence, 0.5),
                "reason": (
                    "matcher said MATCH but named no ledger row; escalating instead of guessing (rule 5). "
                    + out.reason
                ),
            }
        )

    if kept:
        widened = any(rules.widening_applies(by_id[rid]) for rid in kept)
        if widened and not out.widening_applied:
            return out.model_copy(
                update={
                    "widening_applied": True,
                    "evidence": out.evidence
                    + [
                        "widening set by rule 4: a matched receipt carries no lot, so every case on that "
                        "row is treated as affected"
                    ],
                }
            )
    return out


# ---------------------------------------------------------------------------
# Agent-as-tool: the orchestrator calls this, and a whole second agent runs inside it.
# ---------------------------------------------------------------------------
@tool(context=True)
def adjudicate_candidates(tool_context: ToolContext, receipt_ids: Optional[list[int]] = None) -> dict:
    """Decide which scored ledger rows are the recalled product, by running the matcher agent.

    Call this after score_candidates. The matcher applies the match hierarchy (brand + product line + size
    decide, lot only narrows), widens when a receipt carries no lot, honours the coordinator's remembered
    decisions, and escalates to NEEDS_HUMAN rather than guessing. The verdict is saved onto the case.

    Args:
        receipt_ids: optional subset of candidate receipt ids to adjudicate. Omit to adjudicate every row
            that scored above the floor.
    """
    store: Store = tool_context.invocation_state["store"]
    case_id: str = tool_context.invocation_state["case_id"]
    case = store.get_case(case_id)
    if case is None:
        return {"status": "error", "content": [{"text": f"no case {case_id}"}]}

    candidates = rules.score_candidates(store.list_receipts(), case.notice)
    if receipt_ids:
        wanted = set(receipt_ids)
        candidates = [c for c in candidates if c.receipt_id in wanted]

    decisions = store.decisions()
    agent = tool_context.invocation_state.get("matcher_agent")
    verdict = adjudicate(case.notice, candidates, decisions, agent=agent)

    case.verdict = verdict
    store.save_case(case)
    store.audit(
        case_id,
        "agent",
        "verdict",
        f"{verdict.verdict.value} rows={verdict.matched_receipt_ids} widening={verdict.widening_applied} "
        f"conf={verdict.confidence:.2f} :: {verdict.reason}",
    )
    return {"status": "success", "content": [{"json": verdict.model_dump(mode="json")}]}


__all__ = [
    "SYSTEM_PROMPT",
    "adjudicate",
    "adjudicate_candidates",
    "build_matcher_agent",
    "build_prompt",
    "validate_verdict",
]
