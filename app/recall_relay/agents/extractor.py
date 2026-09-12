"""Extractor agent: the fallback when the deterministic parse leaves holes.

`intake.to_recall_notice()` does the work for a well-formed FDA press page. This agent runs only when that
parse reports `extraction_confidence < 0.8` -- a forwarded alert, a firm letter, a PDF with a text layer,
an FSIS notice. Its whole job is transcription: lots, UPCs, best-by strings and the disposition sentence
come across VERBATIM. An invented lot number is worse than a missing one, because a missing one widens
(rule 4) and an invented one narrows onto the wrong pallet.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from strands import Agent

from ..core.models import Classification, ProductLine, RecallNotice, Source
from .model_factory import get_model

SYSTEM_PROMPT = """You transcribe food recall notices into a fixed shape for a food bank's recall desk.

You are a transcriber, not a summarizer. Rules, in order of importance:

1. COPY VERBATIM. Lot codes, UPCs, best-by / use-by strings and the disposition sentence are copied
   character for character from the notice, including spaces, dashes and capitalization. Do not normalize
   "6040 01-6" into "604001-6". Do not reformat "February 9, 2028" into a date.
2. NEVER INVENT. If the notice does not name a lot, `lots` is empty. If it does not name a state,
   `distribution_states` is empty. An empty field is correct; a guessed field is a wrong pallet pulled or a
   right pallet missed.
3. `disposition_verbatim` is the sentence(s) telling people what to do with the product -- return it,
   discard it, do not consume it. Copy the sentence, do not paraphrase it. If there is none, leave it empty.
4. `distribution_states` are two-letter US postal codes only, and only ones the notice actually names.
   If the notice says "nationwide", leave the list empty and put the wording in `distribution_text`.
5. Split products into one `ProductLine` per distinct item: brand, name, size as printed, then its own
   lots / UPCs / best-by strings. If the notice lists lots in a table against sizes, keep them together.
6. `classification` is only set when the notice literally states a class (Class I / II / III) or calls
   itself a market withdrawal. Otherwise leave it null -- downstream code treats null as Class I.
7. `is_food` is false for drugs, devices, cosmetics, tobacco, and pet food.
"""


class ExtractedNotice(BaseModel):
    """What the model is allowed to fill in. Provenance fields are set by code, not by the model."""

    firm: str = Field(description="the recalling company, as named in the notice")
    products: list[ProductLine] = Field(default_factory=list)
    reason: str = Field("", description="the hazard, e.g. 'E. coli O145' or 'undeclared egg'")
    classification: Optional[Classification] = Field(
        None, description="only when the notice states it; null otherwise"
    )
    distribution_states: list[str] = Field(default_factory=list, description="two-letter codes only")
    distribution_text: str = Field("", description="the distribution sentence, verbatim")
    disposition_verbatim: str = Field("", description="what to do with the product, verbatim (rule 11)")
    product_type: str = Field("", description="e.g. 'Food & Beverages'")
    is_food: bool = True


def build_extractor_agent() -> Agent:
    """The transcription agent. Patched out in tests."""
    return Agent(
        model=get_model("extractor"),
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
        name="recall-extractor",
    )


def _prompt_for(raw: Any) -> str:
    header = [
        f"SOURCE URL: {getattr(raw, 'source_url', '') or '(none)'}",
        f"TITLE: {getattr(raw, 'title', '') or '(none)'}",
        f"COMPANY ANNOUNCEMENT DATE: {getattr(raw, 'company_announcement_date', None) or '(none)'}",
        f"FDA PUBLISH DATE: {getattr(raw, 'fda_publish_date', None) or '(none)'}",
        f"PRODUCT TYPE: {getattr(raw, 'product_type', '') or '(none)'}",
        f"REASON FOR ANNOUNCEMENT: {getattr(raw, 'reason', '') or '(none)'}",
        f"COMPANY NAME: {getattr(raw, 'firm', '') or '(none)'}",
        f"BRAND NAME(S): {', '.join(getattr(raw, 'brand_names', []) or []) or '(none)'}",
        f"PRODUCT DESCRIPTION: {getattr(raw, 'product_description', '') or '(none)'}",
    ]
    body = (getattr(raw, "body_text", "") or "")[:12000]
    return (
        "Transcribe this recall notice.\n\n"
        "=== HEADER (already parsed; trust it over the body when they disagree) ===\n"
        + "\n".join(header)
        + "\n\n=== NOTICE BODY ===\n"
        + body
        + "\n\n=== END ===\nCopy lots, UPCs, best-by strings and the disposition sentence verbatim."
    )


def extract_notice(
    raw: Any,
    source: Source,
    seen_at: Optional[datetime] = None,
    *,
    agent: Optional[Agent] = None,
) -> RecallNotice:
    """Run the extractor over a `RawNotice` and merge the result into a `RecallNotice`.

    Provenance (source, source_url, seen_at, raw_excerpt, channel, dates) never comes from the model; it
    comes from the fetch that produced `raw`.

    Args:
        raw: a `core.intake.RawNotice`.
        source: which door the notice came in through.
        seen_at: when we first saw it; defaults to now (UTC).
        agent: injected agent, for tests.

    Returns:
        A `RecallNotice` with extraction_confidence 0.85 (model-filled, not hand-verified).
    """
    seen_at = seen_at or datetime.now(timezone.utc)
    runner = agent or build_extractor_agent()
    result = runner(_prompt_for(raw), structured_output_model=ExtractedNotice)
    out = result.structured_output
    if out is None:  # pragma: no cover - model refused to fill the shape
        raise RuntimeError("extractor returned no structured output")

    body = getattr(raw, "body_text", "") or ""
    return RecallNotice(
        source=source,
        source_url=getattr(raw, "source_url", "") or "",
        source_seen_at=seen_at,
        channel=getattr(raw, "channel", None) or RecallNotice.model_fields["channel"].default,
        firm=out.firm or (getattr(raw, "firm", "") or ""),
        products=list(out.products),
        reason=out.reason or (getattr(raw, "reason", "") or ""),
        classification=out.classification,
        product_type=out.product_type or (getattr(raw, "product_type", "") or "Food & Beverages"),
        announcement_date=getattr(raw, "company_announcement_date", None),
        publish_date=getattr(raw, "fda_publish_date", None),
        distribution_states=[s.strip().upper()[:2] for s in out.distribution_states if s and s.strip()],
        distribution_text=out.distribution_text or (getattr(raw, "distribution_text", "") or ""),
        disposition_verbatim=out.disposition_verbatim or (getattr(raw, "disposition_text", "") or ""),
        is_food=bool(out.is_food),
        raw_excerpt=body[:1500],
        extraction_confidence=0.85,
        title=getattr(raw, "title", "") or "",
    )


def needs_extraction(notice: RecallNotice, threshold: float = 0.8) -> bool:
    """The service's gate: only pay for a model call when the deterministic parse fell short."""
    return notice.extraction_confidence < threshold


__all__ = [
    "ExtractedNotice",
    "SYSTEM_PROMPT",
    "build_extractor_agent",
    "extract_notice",
    "needs_extraction",
]
