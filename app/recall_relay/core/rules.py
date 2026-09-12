"""The encoded domain rules of Recall Relay, as pure functions and constants.

These are the rules a food-safety coordinator actually runs. Each one is a code path, not a prompt line,
so a judge (or an auditor) can read what the agent is not allowed to do. Rule numbers match the README.

Rule 1  Intake precedence: earliest source creates the case; openFDA only enriches.
Rule 2  Class-driven handling (21 CFR 7.3(m)); unclassified press releases are handled as Class I.
Rule 3  Match hierarchy: brand + product line + size, then receipt-date window, then lot to NARROW only.
Rule 4  Missing lot widens, never drops.
Rule 5  NEEDS_HUMAN is the default for ambiguity.
Rule 6  Regulatory scope is explicit (FDA feeds only; FSIS/USDA Foods by paste, tagged).
Rule 7  Salvage / reclamation / repack rows have no lot lineage: physical sort required.
Rule 8  Distribution-pattern gate: auto-dismiss only when the state is excluded AND the brand was never received.
Rule 9  Closed agency response taxonomy.
Rule 10 Follow-up cadence is encoded and editable; an agency is never auto-confirmed.
Rule 11 Disposition is copied verbatim, never invented; on-hand product is HOLD, not destroyed.
Rule 12 One human decision per case; HOLD tags are automatic because they are reversible.
Rule 13 Audit packet on close.
Rule 14 Never re-ping; remember decisions; drills are labeled and never touch real HOLD tags.
Rule 15 Client-facing notice on Class I or on an "already distributed" response.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Iterable

from rapidfuzz import fuzz

from .models import (
    Candidate,
    Classification,
    ProductLine,
    Receipt,
    ReceiptChannel,
    RecallNotice,
    ResponseOption,
    ResponseStatus,
)

# ---------------------------------------------------------------------------
# Rule 2 / Rule 10: classification-driven handling
# ---------------------------------------------------------------------------
CLASS_DEFINITIONS = {
    Classification.CLASS_I: (
        "a situation in which there is a reasonable probability that the use of, or exposure to, a violative "
        "product will cause serious adverse health consequences or death"
    ),
    Classification.CLASS_II: (
        "a situation in which use of, or exposure to, a violative product may cause temporary or medically "
        "reversible adverse health consequences or where the probability of serious adverse health "
        "consequences is remote"
    ),
    Classification.CLASS_III: (
        "a situation in which use of, or exposure to, a violative product is not likely to cause adverse "
        "health consequences"
    ),
}

# hours: (reminder_after, escalate_after). Class III goes to the weekly digest (168h) and never escalates.
FOLLOWUP_CADENCE_HOURS: dict[Classification, tuple[int, int | None]] = {
    Classification.CLASS_I: (24, 48),
    Classification.CLASS_II: (72, 168),
    Classification.CLASS_III: (168, None),
    Classification.MARKET_WITHDRAWAL: (168, None),
}


def effective_classification(notice: RecallNotice) -> Classification:
    """Rule 2: an unclassified press release is handled as Class I until openFDA says otherwise."""
    return notice.classification or Classification.CLASS_I


def followup_cadence(classification: Classification) -> tuple[int, int | None]:
    """Rule 10."""
    return FOLLOWUP_CADENCE_HOURS[classification]


def client_notice_required(classification: Classification, any_already_distributed: bool) -> bool:
    """Rule 15."""
    return classification == Classification.CLASS_I or any_already_distributed


def agencies_notified_immediately(classification: Classification) -> bool:
    """Rule 2: Class III and market withdrawals go to the weekly digest, not an immediate notice."""
    return classification in (Classification.CLASS_I, Classification.CLASS_II)


# ---------------------------------------------------------------------------
# Rule 9: closed response taxonomy
# ---------------------------------------------------------------------------
RESPONSE_OPTIONS: list[ResponseOption] = [
    ResponseOption(status=ResponseStatus.PULLED, label="We pulled it (enter case count)"),
    ResponseOption(status=ResponseStatus.NEVER_RECEIVED, label="We never received this product"),
    ResponseOption(status=ResponseStatus.ALREADY_DISTRIBUTED, label="Already distributed to clients"),
    ResponseOption(status=ResponseStatus.NEED_PICKUP, label="Pulled, need a pickup"),
]


# ---------------------------------------------------------------------------
# Rule 3 / Rule 4: matching
# ---------------------------------------------------------------------------
# Receipts inside this window before the announcement are candidates. Frozen product lives a long time,
# so the window is generous; the Matcher narrows with lot / best-by when the ledger has them.
CANDIDATE_WINDOW_DAYS_BEFORE = 120
CANDIDATE_WINDOW_DAYS_AFTER = 14  # receipts logged a few days after the announcement still count

# Deterministic thresholds. Above AUTO_CANDIDATE the row goes to the Matcher; below FLOOR it is not shown.
CANDIDATE_FLOOR = 45.0
STRONG_CANDIDATE = 80.0

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(oz|ounce|ounces|lb|lbs|pound|pounds|g|gram|grams|kg|ml|l|liter|liters|fl\.?\s*oz|ct|count|pack|pk)\b", re.I)


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _size_tokens(s: str) -> set[str]:
    out = set()
    for num, unit in _SIZE_RE.findall(s or ""):
        unit = unit.lower().replace(".", "").replace(" ", "")
        unit = {"ounce": "oz", "ounces": "oz", "floz": "oz", "lbs": "lb", "pound": "lb", "pounds": "lb",
                "gram": "g", "grams": "g", "liter": "l", "liters": "l", "count": "ct", "pk": "pack"}.get(unit, unit)
        out.add(f"{float(num):g}{unit}")
    return out


def candidate_window(notice: RecallNotice) -> tuple[date, date]:
    """Rule 3, second tier: the receipt-date window around the announcement."""
    anchor = notice.announcement_date or notice.publish_date or notice.source_seen_at.date()
    return anchor - timedelta(days=CANDIDATE_WINDOW_DAYS_BEFORE), anchor + timedelta(days=CANDIDATE_WINDOW_DAYS_AFTER)


def lot_relation(receipt: Receipt, line: ProductLine) -> str:
    if not line.lots:
        return "notice_has_no_lot"
    if not receipt.lot:
        return "receipt_has_no_lot"
    r = _norm(receipt.lot).replace(" ", "")
    for lot in line.lots:
        n = _norm(lot).replace(" ", "")
        if n and (n in r or r in n):
            return "match"
    return "mismatch"


def score_receipt(receipt: Receipt, line: ProductLine, notice: RecallNotice) -> Candidate:
    """Rule 3: deterministic candidate scoring. The LLM never sees the raw ledger, only these rows.

    brand and product line carry the score; size confirms; the date window gates; lot only narrows
    (a lot mismatch drops the score, a missing lot never does).
    """
    start, end = candidate_window(notice)
    in_window = start <= receipt.received_at <= end

    brand_score = fuzz.token_set_ratio(_norm(receipt.brand), _norm(line.brand)) if (receipt.brand and line.brand) else 0.0
    product_score = fuzz.token_set_ratio(_norm(receipt.product), _norm(f"{line.brand} {line.name}"))
    rs, ls = _size_tokens(receipt.size or receipt.product), _size_tokens(line.size or line.name)
    if rs and ls:
        size_score = 100.0 if rs & ls else 0.0
    else:
        size_score = 50.0  # unknown on one side: neither confirms nor denies

    # when the ledger row has no brand column, the brand often sits inside the product text
    if not receipt.brand and line.brand and _norm(line.brand) in _norm(receipt.product):
        brand_score = 100.0

    rel = lot_relation(receipt, line)
    composite = 0.45 * brand_score + 0.40 * product_score + 0.15 * size_score
    if not in_window:
        composite *= 0.5
    if rel == "mismatch":
        composite *= 0.6  # narrows, never widens (rule 3)

    note = ""
    if receipt.channel == ReceiptChannel.SALVAGE:
        note = "salvage/repack row: no lot lineage, physical sort required (rule 7)"
    elif rel == "receipt_has_no_lot" and composite >= STRONG_CANDIDATE:
        note = "receipt carries no lot; if matched, every case is affected (rule 4)"

    return Candidate(
        receipt_id=receipt.id,
        brand=receipt.brand,
        product=receipt.product,
        size=receipt.size,
        lot=receipt.lot,
        received_at=receipt.received_at,
        cases=receipt.cases,
        channel=receipt.channel,
        brand_score=round(brand_score, 1),
        product_score=round(product_score, 1),
        size_score=round(size_score, 1),
        in_window=in_window,
        lot_relation=rel,  # type: ignore[arg-type]
        deterministic_score=round(composite, 1),
        note=note,
    )


def score_candidates(receipts: Iterable[Receipt], notice: RecallNotice, top_k: int = 8) -> list[Candidate]:
    """Score every receipt against every product line in the notice; return the top-k above the floor."""
    best: dict[int, Candidate] = {}
    for r in receipts:
        for line in notice.products:
            c = score_receipt(r, line, notice)
            if c.deterministic_score < CANDIDATE_FLOOR:
                continue
            if r.id not in best or c.deterministic_score > best[r.id].deterministic_score:
                best[r.id] = c
    return sorted(best.values(), key=lambda c: c.deterministic_score, reverse=True)[:top_k]


def widening_applies(candidate: Candidate) -> bool:
    """Rule 4: a matched receipt with no lot means every case of that line is affected."""
    return candidate.lot_relation in ("receipt_has_no_lot", "notice_has_no_lot")


def physical_sort_required(receipt: Receipt) -> bool:
    """Rule 7."""
    return receipt.channel == ReceiptChannel.SALVAGE


# ---------------------------------------------------------------------------
# Rule 8: distribution-pattern gate
# ---------------------------------------------------------------------------
_NATIONWIDE = re.compile(r"nationwide|all states|throughout the (?:united states|u\.?s\.?)|50 states", re.I)


def distribution_gate(notice: RecallNotice, food_bank_state: str, ledger_brands: set[str]) -> tuple[bool, str]:
    """Return (auto_dismiss, reason).

    Auto-dismiss silently ONLY when the notice names states, ours is not among them, the text is not
    nationwide, AND no product line's brand appears anywhere in the ledger. Distribution patterns are
    routinely incomplete (the demo recall's own press release was corrected from 16 to 27 states), so a
    received brand is always evaluated.
    """
    states = {s.upper() for s in notice.distribution_states}
    text = notice.distribution_text or ""
    if not states or _NATIONWIDE.search(text) or food_bank_state.upper() in states:
        return False, "distribution includes our state or is unspecified/nationwide"
    brands = {_norm(b) for b in ledger_brands if b}
    for line in notice.products:
        if line.brand and _norm(line.brand) in brands:
            return False, f"distribution excludes {food_bank_state} but brand {line.brand!r} exists in the ledger"
    return True, f"distribution limited to {sorted(states)}; brand never received"


# ---------------------------------------------------------------------------
# Rule 6: regulatory scope
# ---------------------------------------------------------------------------
NON_FOOD_HINTS = re.compile(
    r"\b(drug|drugs|injection|tablet|capsule|medical|device|firmware|apap|cpap|supplement for dogs|pet food|dog food|cat food|cosmetic|vape|tobacco)\b",
    re.I,
)


UNAMBIGUOUS_NON_FOOD = re.compile(
    r"\b(injection|injectable|firmware|medical device|pet food|dog food|cat food|supplements? for (?:dogs|cats|pets))\b",
    re.I,
)


def looks_like_food(title: str, product_type: str) -> bool:
    """Rule 6 helper. Product Type decides, except that an unambiguous non-food title always loses:
    FDA has tagged an epinephrine injection as 'Food & Beverages'. Supplements stay food (they are
    FDA-regulated food, and a silent drop is the expensive failure)."""
    if UNAMBIGUOUS_NON_FOOD.search(title or ""):
        return False
    if product_type and "food" in product_type.lower() and "pet" not in product_type.lower():
        return True
    if product_type and "food" not in product_type.lower():
        return False
    return not NON_FOOD_HINTS.search(title or "")
