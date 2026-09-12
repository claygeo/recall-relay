"""Rules tests: the deterministic half of the matcher, scored against the real demo ledger.

The notices here are built inline from the two real recalls the demo uses:

  H-1181-2026  Frutas y Hortalizas del Sur S.A., expanded 2026-09-02/03. Great Value Organic Triple
               Berry Blend 10 oz, lot 6040 01-6, best by Feb 9 2028, E. coli O145. Shipped to Walmart
               stores in 27 states including Florida. The same event also covers Organic Whole
               Blueberries 10 oz, which is why receipt 34 is genuinely ambiguous rather than merely noisy.
  H-0844-2026  Ghirardelli sweet ground white chocolate flavored powder, 50 oz, lot S394260. The ledger
               holds Ghirardelli semi-sweet chocolate chips: same brand, unrelated product.

Two planted-defect tests live at the bottom: corrupt the known-good hero row and the known-good
distribution gate, and assert the corrupted case is rejected. A verifier that has never failed has never
been tested.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from recall_relay.core import rules
from recall_relay.core import seed as seed_module
from recall_relay.core.models import (
    Channel,
    Classification,
    ProductLine,
    Receipt,
    ReceiptChannel,
    RecallNotice,
    ResponseStatus,
    Source,
)
from recall_relay.core.rules import CANDIDATE_FLOOR, STRONG_CANDIDATE
from recall_relay.core.store import Store

HERO = seed_module.HERO_RECEIPT_ID
DECOY_SAME_BRAND = seed_module.DECOY_SAME_BRAND_RECEIPT_ID
DECOY_GHIRARDELLI = seed_module.DECOY_GHIRARDELLI_RECEIPT_ID
DECOY_AMBIGUOUS = seed_module.DECOY_AMBIGUOUS_RECEIPT_ID
SALVAGE = seed_module.SALVAGE_RECEIPT_ID


# --------------------------------------------------------------------------------------------------
# notices under test
# --------------------------------------------------------------------------------------------------
def berry_notice(**overrides) -> RecallNotice:
    """FDA press release of 2026-09-03 (openFDA H-1181-2026). Unclassified on the press-release path."""
    kwargs = dict(
        source=Source.FDA_RSS,
        source_url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/frutas-expands",
        source_seen_at=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc),
        channel=Channel.FDA,
        recall_number="H-1181-2026",
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[
            ProductLine(brand="Great Value", name="Organic Triple Berry Blend", size="10 oz",
                        upcs_as_printed=["0 78742 42846 5"], lots=["6040 01-6"], best_by=["Feb 9 2028"]),
            ProductLine(brand="", name="Organic Whole Blueberries", size="10 oz",
                        lots=["6040 01-6"], best_by=["Feb 9 2028"]),
        ],
        reason="potential contamination with E. coli O145",
        classification=None,
        announcement_date=date(2026, 9, 2),
        distribution_states=["FL", "TX", "CA", "GA", "NY"],
        distribution_text="Shipped to Walmart retail stores in 27 states, including Florida.",
        disposition_verbatim="Consumers should not eat the recalled product and should throw it away "
                             "or return it to the place of purchase for a full refund.",
        product_type="Food & Beverages",
    )
    kwargs.update(overrides)
    return RecallNotice(**kwargs)


def ghirardelli_notice(**overrides) -> RecallNotice:
    kwargs = dict(
        source=Source.OPENFDA,
        source_url="https://api.fda.gov/food/enforcement.json?search=H-0844-2026",
        source_seen_at=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
        channel=Channel.FDA,
        recall_number="H-0844-2026",
        firm="Ghirardelli Chocolate Company",
        products=[ProductLine(brand="Ghirardelli", name="Sweet Ground White Chocolate Flavored Powder",
                              size="50 oz", lots=["S394260"])],
        reason="undeclared milk allergen",
        classification=Classification.CLASS_II,
        announcement_date=date(2026, 7, 15),
        distribution_states=["CA", "FL", "TX"],
        product_type="Food & Beverages",
    )
    kwargs.update(overrides)
    return RecallNotice(**kwargs)


def repack_notice(**overrides) -> RecallNotice:
    """A firm-level frozen fruit recall broad enough to reach the reclamation repack pallet."""
    kwargs = dict(
        source=Source.PASTED_TEXT,
        source_seen_at=datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc),
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[ProductLine(brand="", name="Assorted Frozen Fruit Repack", size="")],
        reason="potential contamination with E. coli O145",
        announcement_date=date(2026, 9, 2),
        product_type="Food & Beverages",
    )
    kwargs.update(overrides)
    return RecallNotice(**kwargs)


def by_id(candidates) -> dict[int, object]:
    return {c.receipt_id: c for c in candidates}


@pytest.fixture
def receipts(seeded_store: Store) -> list[Receipt]:
    return seeded_store.list_receipts()


# --------------------------------------------------------------------------------------------------
# Rule 3 / Rule 4: the hero match
# --------------------------------------------------------------------------------------------------
def test_hero_receipt_is_the_top_candidate(receipts: list[Receipt]):
    candidates = rules.score_candidates(receipts, berry_notice())
    assert candidates, "the ledger should produce candidates for the berry notice"

    top = candidates[0]
    assert top.receipt_id == HERO
    assert top.deterministic_score >= STRONG_CANDIDATE
    assert top.brand == "Great Value" and top.product == "Organic Triple Berry Blend"
    assert top.size == "10 oz" and top.cases == 30
    assert top.channel == ReceiptChannel.RETAIL_RESCUE
    assert top.in_window is True
    assert top.brand_score == 100.0 and top.product_score == 100.0 and top.size_score == 100.0

    # exactly one strong candidate: everything else in the ledger is noise the Matcher has to reject
    strong = [c for c in candidates if c.deterministic_score >= STRONG_CANDIDATE]
    assert [c.receipt_id for c in strong] == [HERO]


def test_hero_receipt_has_no_lot_so_rule_4_widens(receipts: list[Receipt]):
    top = rules.score_candidates(receipts, berry_notice())[0]
    assert top.receipt_id == HERO
    assert top.lot == ""
    assert top.lot_relation == "receipt_has_no_lot"
    assert rules.widening_applies(top) is True
    assert "rule 4" in top.note.lower()
    assert "every case" in top.note.lower()


def test_widening_does_not_apply_when_the_lot_actually_matches(receipts: list[Receipt]):
    """Rule 4 widens only because the ledger row is blank. A real lot match must narrow, not widen."""
    hero = next(r for r in receipts if r.id == HERO)
    with_lot = hero.model_copy(update={"lot": "6040 01-6"})
    notice = berry_notice()
    candidate = rules.score_receipt(with_lot, notice.products[0], notice)
    assert candidate.lot_relation == "match"
    assert rules.widening_applies(candidate) is False
    assert candidate.deterministic_score >= STRONG_CANDIDATE
    assert "rule 4" not in candidate.note.lower()


def test_same_brand_different_line_lands_in_the_ambiguous_band(receipts: list[Receipt]):
    """Great Value Mixed Berries 16 oz: same brand, same freezer, different product and different size."""
    candidates = by_id(rules.score_candidates(receipts, berry_notice(), top_k=20))
    decoy = candidates.get(DECOY_SAME_BRAND)
    assert decoy is not None, "the decoy should be visible to the Matcher, not silently dropped"
    assert decoy.product == "Mixed Berries" and decoy.size == "16 oz"
    assert CANDIDATE_FLOOR <= decoy.deterministic_score < STRONG_CANDIDATE
    assert decoy.brand_score == 100.0        # brand alone must never be enough
    assert decoy.size_score == 0.0
    assert decoy.deterministic_score < candidates[HERO].deterministic_score


def test_unbranded_blueberry_row_is_the_needs_human_beat(receipts: list[Receipt]):
    """Receipt 34 has no brand column and matches a recalled line by name and size exactly.

    It is genuinely ambiguous: the same firm recalled Organic Whole Blueberries 10 oz, and nothing in the
    ledger says whose blueberries these were. Rule 5 says that is a human's call, so it has to surface in
    the ambiguous band rather than auto-match or disappear.
    """
    candidates = by_id(rules.score_candidates(receipts, berry_notice(), top_k=20))
    ambiguous = candidates.get(DECOY_AMBIGUOUS)
    assert ambiguous is not None
    assert ambiguous.brand == ""
    assert CANDIDATE_FLOOR <= ambiguous.deterministic_score < STRONG_CANDIDATE
    assert ambiguous.product_score == 100.0 and ambiguous.size_score == 100.0
    assert ambiguous.brand_score == 0.0      # no brand evidence at all: that is the whole ambiguity


def test_ghirardelli_row_is_not_a_candidate_for_the_berry_notice(receipts: list[Receipt]):
    candidates = by_id(rules.score_candidates(receipts, berry_notice(), top_k=60))
    assert DECOY_GHIRARDELLI not in candidates

    chips = next(r for r in receipts if r.id == DECOY_GHIRARDELLI)
    notice = berry_notice()
    scored = [rules.score_receipt(chips, line, notice) for line in notice.products]
    assert max(c.deterministic_score for c in scored) < CANDIDATE_FLOOR


def test_ghirardelli_notice_never_makes_the_chips_row_strong(receipts: list[Receipt]):
    """Same brand, different product, and a lot that does not match: rule 3 narrows, it never widens."""
    notice = ghirardelli_notice()
    chips = next(r for r in receipts if r.id == DECOY_GHIRARDELLI)
    candidate = rules.score_receipt(chips, notice.products[0], notice)

    assert candidate.brand_score == 100.0
    assert candidate.size_score == 0.0                     # 12 oz bag vs a 50 oz foodservice can
    assert candidate.lot_relation == "mismatch"
    assert candidate.deterministic_score < STRONG_CANDIDATE

    for c in rules.score_candidates(receipts, notice, top_k=60):
        assert c.deterministic_score < STRONG_CANDIDATE, f"receipt {c.receipt_id} should never be strong"


def test_salvage_row_carries_the_rule_7_note_when_it_scores(receipts: list[Receipt]):
    notice = repack_notice()
    salvage = next(r for r in receipts if r.id == SALVAGE)
    assert salvage.channel == ReceiptChannel.SALVAGE

    candidate = rules.score_receipt(salvage, notice.products[0], notice)
    assert candidate.deterministic_score >= CANDIDATE_FLOOR, "the row has to actually score"
    assert "rule 7" in candidate.note.lower()
    assert "physical sort" in candidate.note.lower()
    assert rules.physical_sort_required(salvage) is True

    scored = by_id(rules.score_candidates(receipts, notice, top_k=20))
    assert SALVAGE in scored and "rule 7" in scored[SALVAGE].note.lower()

    # and a normal retail-rescue row does not claim a physical sort
    assert rules.physical_sort_required(next(r for r in receipts if r.id == HERO)) is False


def test_out_of_window_receipts_are_penalised(receipts: list[Receipt]):
    hero = next(r for r in receipts if r.id == HERO)
    notice = berry_notice()
    inside = rules.score_receipt(hero, notice.products[0], notice)
    stale = rules.score_receipt(hero.model_copy(update={"received_at": date(2026, 1, 5)}),
                                notice.products[0], notice)
    assert inside.in_window is True and stale.in_window is False
    assert stale.deterministic_score == pytest.approx(inside.deterministic_score * 0.5, abs=0.2)


# --------------------------------------------------------------------------------------------------
# lot relations
# --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("receipt_lot", "notice_lots", "expected"), [
    ("", ["6040 01-6"], "receipt_has_no_lot"),
    ("6040 01-6", [], "notice_has_no_lot"),
    ("", [], "notice_has_no_lot"),
    ("6040 01-6", ["6040 01-6"], "match"),
    ("604001-6", ["6040 01-6"], "match"),          # whitespace is not a lot difference
    ("6040 01-6 / 2", ["6040 01-6"], "match"),     # the ledger sometimes appends a pallet suffix
    ("9999 99-9", ["6040 01-6"], "mismatch"),
])
def test_lot_relation(receipt_lot: str, notice_lots: list[str], expected: str):
    receipt = Receipt(id=1, received_at=date(2026, 8, 24), donor="d", channel=ReceiptChannel.PURCHASE,
                      product="Organic Triple Berry Blend", lot=receipt_lot, cases=1)
    line = ProductLine(brand="Great Value", name="Organic Triple Berry Blend", size="10 oz", lots=notice_lots)
    assert rules.lot_relation(receipt, line) == expected


# --------------------------------------------------------------------------------------------------
# Rule 8: distribution gate
# --------------------------------------------------------------------------------------------------
def test_distribution_gate_our_state_is_named(seeded_store: Store):
    dismiss, reason = rules.distribution_gate(berry_notice(), "FL", seeded_store.ledger_brands())
    assert dismiss is False
    assert "our state" in reason or "FL" in reason


def test_distribution_gate_excluded_state_and_brand_never_received():
    notice = berry_notice(distribution_states=["WA", "OR"],
                          distribution_text="Distributed to retail stores in Washington and Oregon.")
    dismiss, reason = rules.distribution_gate(notice, "FL", {"Goya", "Del Monte", "USDA Foods"})
    assert dismiss is True
    assert "never received" in reason


def test_distribution_gate_excluded_state_but_brand_is_in_the_ledger(seeded_store: Store):
    """Distribution patterns are routinely incomplete. A brand we actually stock is never auto-dismissed."""
    notice = berry_notice(distribution_states=["WA", "OR"],
                          distribution_text="Distributed to retail stores in Washington and Oregon.")
    dismiss, reason = rules.distribution_gate(notice, "FL", seeded_store.ledger_brands())
    assert dismiss is False
    assert "Great Value" in reason


def test_distribution_gate_nationwide_text_beats_a_short_state_list():
    notice = berry_notice(distribution_states=["WA"],
                          distribution_text="The product was distributed nationwide.")
    dismiss, _ = rules.distribution_gate(notice, "FL", {"Goya"})
    assert dismiss is False


def test_distribution_gate_unspecified_states_never_dismiss():
    notice = berry_notice(distribution_states=[], distribution_text="")
    dismiss, _ = rules.distribution_gate(notice, "FL", set())
    assert dismiss is False


# --------------------------------------------------------------------------------------------------
# Rule 3 second tier: the candidate window
# --------------------------------------------------------------------------------------------------
def test_candidate_window_anchors_on_the_announcement_date():
    notice = berry_notice()
    start, end = rules.candidate_window(notice)
    assert start == date(2026, 9, 2) - timedelta(days=rules.CANDIDATE_WINDOW_DAYS_BEFORE)
    assert end == date(2026, 9, 2) + timedelta(days=rules.CANDIDATE_WINDOW_DAYS_AFTER)
    assert start <= date(2026, 8, 24) <= end          # the hero receipt sits inside it


def test_candidate_window_falls_back_to_publish_date_then_to_when_we_saw_it():
    publish = berry_notice(announcement_date=None, publish_date=date(2026, 8, 30))
    assert rules.candidate_window(publish)[0] == date(2026, 8, 30) - timedelta(days=120)

    seen_only = berry_notice(announcement_date=None, publish_date=None)
    anchor = seen_only.source_seen_at.date()
    assert rules.candidate_window(seen_only) == (anchor - timedelta(days=120), anchor + timedelta(days=14))


# --------------------------------------------------------------------------------------------------
# Rule 6: regulatory scope
# --------------------------------------------------------------------------------------------------
def test_looks_like_food_rejects_a_device_recall():
    assert rules.looks_like_food("Acme Recalls Insulin Pump Over Firmware Defect", "Medical Devices") is False
    assert rules.looks_like_food("Acme Recalls Insulin Pump Over Firmware Defect", "") is False


def test_looks_like_food_accepts_a_food_recall():
    assert rules.looks_like_food(
        "Frutas y Hortalizas del Sur S.A. Expands Recall of Frozen Organic Berries",
        "Food & Beverages") is True
    assert rules.looks_like_food("Firm Recalls Frozen Berries Due to E. coli", "") is True


def test_looks_like_food_rejects_pet_food():
    assert rules.looks_like_food("Brand X Recalls Dry Dog Food for Salmonella",
                                 "Animal & Veterinary") is False
    assert rules.looks_like_food("Brand X Recalls Dry Dog Food for Salmonella", "") is False
    assert rules.looks_like_food("Brand X Recalls Cat Food", "Pet Food") is False


# --------------------------------------------------------------------------------------------------
# Rule 2 / Rule 10 / Rule 15: classification-driven handling
# --------------------------------------------------------------------------------------------------
def test_effective_classification_defaults_to_class_i():
    """An FDA press release carries no class for weeks. Treating it as Class I is the safe default."""
    unclassified = berry_notice(classification=None)
    assert unclassified.classification is None
    assert rules.effective_classification(unclassified) == Classification.CLASS_I

    classified = berry_notice(classification=Classification.CLASS_II)
    assert rules.effective_classification(classified) == Classification.CLASS_II


def test_followup_cadence_per_class():
    assert rules.followup_cadence(Classification.CLASS_I) == (24, 48)
    assert rules.followup_cadence(Classification.CLASS_II) == (72, 168)
    assert rules.followup_cadence(Classification.CLASS_III) == (168, None)
    assert rules.followup_cadence(Classification.MARKET_WITHDRAWAL) == (168, None)
    assert all(c in rules.FOLLOWUP_CADENCE_HOURS for c in Classification)


def test_client_notice_rules():
    assert rules.client_notice_required(Classification.CLASS_I, False) is True
    assert rules.client_notice_required(Classification.CLASS_III, True) is True   # already on plates
    assert rules.client_notice_required(Classification.CLASS_III, False) is False


def test_response_taxonomy_is_closed():
    assert [o.status for o in rules.RESPONSE_OPTIONS] == [
        ResponseStatus.PULLED, ResponseStatus.NEVER_RECEIVED,
        ResponseStatus.ALREADY_DISTRIBUTED, ResponseStatus.NEED_PICKUP,
    ]
    assert all(o.label for o in rules.RESPONSE_OPTIONS)


# --------------------------------------------------------------------------------------------------
# PLANTED DEFECT 1: a wrong lot on the hero row must be rejected, not absorbed
# --------------------------------------------------------------------------------------------------
def test_planted_defect_wrong_lot_on_the_hero_row_is_rejected(receipts: list[Receipt]):
    """Copy the known-good hero row, change only the lot, and the verdict has to change with it.

    If this passes while the real row also passes, the lot comparison is decorative.
    """
    notice = berry_notice()
    line = notice.products[0]
    hero = next(r for r in receipts if r.id == HERO)
    real = rules.score_receipt(hero, line, notice)

    corrupted_row = hero.model_copy(update={"id": 9001, "lot": "9999 99-9"})
    corrupted = rules.score_receipt(corrupted_row, line, notice)

    assert real.lot_relation == "receipt_has_no_lot"
    assert corrupted.lot_relation == "mismatch"
    assert corrupted.deterministic_score < real.deterministic_score
    assert corrupted.deterministic_score < STRONG_CANDIDATE
    assert rules.widening_applies(corrupted) is False
    # brand, product and size are untouched: only the lot moved the score
    assert (corrupted.brand_score, corrupted.product_score, corrupted.size_score) == \
           (real.brand_score, real.product_score, real.size_score)

    # and the corrupted row must not be able to take the top slot away from the real one
    ranked = rules.score_candidates(list(receipts) + [corrupted_row], notice, top_k=20)
    assert ranked[0].receipt_id == HERO


# --------------------------------------------------------------------------------------------------
# PLANTED DEFECT 2: the distribution gate must flip when its inputs are corrupted
# --------------------------------------------------------------------------------------------------
def test_planted_defect_distribution_gate_flips_on_the_brand_ledger(seeded_store: Store):
    """Same notice, two ledgers. If the gate dismisses in both, it is not reading the ledger; if it
    dismisses in neither, rule 8 is dead code and every out-of-state recall becomes a human's problem."""
    notice = berry_notice(distribution_states=["WA"],
                          distribution_text="Distributed to retail stores in Washington.")

    brand_absent = {b for b in seeded_store.ledger_brands() if b != "Great Value"}
    assert "Great Value" not in brand_absent
    dismiss_absent, reason_absent = rules.distribution_gate(notice, "FL", brand_absent)

    brand_present = seeded_store.ledger_brands()
    assert "Great Value" in brand_present
    dismiss_present, reason_present = rules.distribution_gate(notice, "FL", brand_present)

    assert dismiss_absent is True, "an out-of-state recall for a brand we never took must self-dismiss"
    assert dismiss_present is False, "the same recall must stay open once the brand is in the ledger"
    assert reason_absent != reason_present


# --------------------------------------------------------------------------------------------------
# PLANTED DEFECT 3: a corrupted size must not be scored as a confirmation
# --------------------------------------------------------------------------------------------------
def test_planted_defect_wrong_size_is_not_scored_as_a_match(receipts: list[Receipt]):
    """Size confirms; it must never silently confirm the wrong pack. A 10 oz recall against a 32 oz
    ledger row has to score the size at 0, not at the 50 reserved for 'unknown on one side'."""
    notice = berry_notice()
    line = notice.products[0]
    hero = next(r for r in receipts if r.id == HERO)

    real = rules.score_receipt(hero, line, notice)
    wrong_size = rules.score_receipt(hero.model_copy(update={"id": 9002, "size": "32 oz"}), line, notice)
    unknown_row = hero.model_copy(update={"id": 9003, "size": ""})
    unknown_size = rules.score_receipt(unknown_row, line, notice)

    assert real.size_score == 100.0
    assert wrong_size.size_score == 0.0
    assert unknown_size.size_score == 50.0            # absent evidence is not contrary evidence
    assert wrong_size.deterministic_score < unknown_size.deterministic_score < real.deterministic_score
