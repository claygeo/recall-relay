"""Store + seed tests.

The register is the part of Recall Relay that has to be boring and right: if the ledger arithmetic is
wrong, the pull list is wrong, and the pull list is the thing a pantry acts on.

Three tests in here (and in test_rules.py) are deliberately planted-defect tests: they corrupt a known-good
input and assert the corruption is *rejected* or produces a different, correct answer. A verifier that has
never failed has never been tested.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from recall_relay.core import seed as seed_module
from recall_relay.core.models import (
    CaseStatus,
    Channel,
    Classification,
    Distribution,
    ProductLine,
    RecallCase,
    RecallNotice,
    ResponseStatus,
    Source,
)
from recall_relay.core.store import Store

HERO = seed_module.HERO_RECEIPT_ID


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def make_notice(url: str = "https://www.fda.gov/safety/recalls/demo", recall_number: str = "H-1181-2026",
                source: Source = Source.FDA_RSS) -> RecallNotice:
    return RecallNotice(
        source=source,
        source_url=url,
        source_seen_at=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc),
        channel=Channel.FDA,
        recall_number=recall_number,
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[ProductLine(brand="Great Value", name="Organic Triple Berry Blend", size="10 oz",
                              lots=["6040 01-6"], best_by=["Feb 9 2028"])],
        reason="E. coli O145",
        classification=Classification.CLASS_I,
        announcement_date=datetime(2026, 9, 2).date(),
    )


def make_case(store: Store, case_id: str = "case-001", *, is_drill: bool = False,
              status: CaseStatus = CaseStatus.NEW, **notice_kwargs) -> RecallCase:
    case = RecallCase(
        id=case_id,
        status=status,
        is_drill=is_drill,
        created_at=store.now(),
        notice=make_notice(**notice_kwargs),
    )
    return store.create_case(case)


def shipped_cases(store: Store, receipt_id: int) -> int:
    """Sum the distribution log. Tests compute this from data instead of trusting a constant."""
    return sum(d.cases for d in store.list_distributions(receipt_id))


# --------------------------------------------------------------------------------------------------
# seed + ledger
# --------------------------------------------------------------------------------------------------
def test_seed_loads_expected_counts(seeded_store: Store):
    assert len(seeded_store.list_agencies()) == seed_module.N_AGENCIES == 12
    assert len(seeded_store.list_receipts()) == seed_module.N_RECEIPTS == 60
    assert len(seeded_store.list_distributions()) == seed_module.N_DISTRIBUTIONS == 140


def test_seed_is_deterministic():
    """Same generator, same bytes. The demo has to reproduce on camera."""
    a1, a2 = seed_module.agencies(), seed_module.agencies()
    r1, r2 = seed_module.receipts(), seed_module.receipts()
    d1, d2 = seed_module.distributions(), seed_module.distributions()
    assert a1 == a2 and r1 == r2 and d1 == d2


def test_csv_round_trip_matches_the_generator(store: Store, tmp_path: Path):
    """agencies/receipts/distributions survive write_csvs -> import_csv unchanged."""
    out = tmp_path / "ledger"
    counts = seed_module.write_csvs(out)
    assert counts == {"agencies": 12, "receipts": 60, "distributions": 140}

    for kind, filename in seed_module.CSV_FILES.items():
        assert store.import_csv(kind, out / filename) == counts[kind]

    assert store.list_agencies() == sorted(seed_module.agencies(), key=lambda a: a.id)
    assert store.list_receipts() == seed_module.receipts()
    assert store.list_distributions() == seed_module.distributions()


def test_committed_fixture_csvs_are_not_stale(store: Store, ledger_fixture_dir: Path):
    """Drift guard: the CSVs checked into data/fixtures/ledger must equal the generator output."""
    for kind, filename in seed_module.CSV_FILES.items():
        path = ledger_fixture_dir / filename
        assert path.exists(), f"missing committed fixture {path}"
        store.import_csv(kind, path)

    assert store.list_agencies() == sorted(seed_module.agencies(), key=lambda a: a.id)
    assert store.list_receipts() == seed_module.receipts()
    assert store.list_distributions() == seed_module.distributions()


def test_agency_round_trip_preserves_languages_and_same_day_flag(store: Store, ledger_fixture_dir: Path):
    store.import_csv("agencies", ledger_fixture_dir / "agencies.csv")
    kitchen = store.get_agency("redland-kitchen")
    assert kitchen is not None
    assert kitchen.same_day_distribution is True
    assert kitchen.languages == ["en", "es"]          # pipe-separated in CSV, list in the model
    assert kitchen.contact_email == "redland-kitchen@example.org"
    assert kitchen.contact_phone.startswith("305-555-01")

    church = store.get_agency("cutler-bay-baptist")
    assert church is not None and church.same_day_distribution is False
    assert church.languages == ["en"]

    same_day = [a.id for a in store.list_agencies() if a.same_day_distribution]
    assert len(same_day) >= 2
    assert {a.city for a in store.list_agencies()} >= {"Homestead", "Cutler Bay", "Florida City"}
    assert {lang for a in store.list_agencies() for lang in a.languages} == {"en", "es", "ht"}


def test_hero_receipt_round_trips_exactly(seeded_store: Store):
    r = seeded_store.get_receipt(HERO)
    assert r is not None
    assert r.received_at.isoformat() == "2026-08-24"
    assert r.donor == "Walmart Supercenter #1234 retail rescue"
    assert r.channel.value == "retail_rescue"
    assert r.brand == "Great Value"
    assert r.product == "Organic Triple Berry Blend"
    assert r.size == "10 oz"
    assert r.upc == "" and r.lot == ""          # retail rescue: no UPC, no lot. This is the whole point.
    assert r.best_by is None
    assert r.cases == 30
    assert r.storage == "frozen"


def test_ledger_is_messy_in_the_ways_the_matcher_has_to_handle(seeded_store: Store):
    receipts = seeded_store.list_receipts()
    with_lot = [r for r in receipts if r.lot]
    assert 0.20 <= len(with_lot) / len(receipts) <= 0.45, "lots should sit near 30% of rows"
    assert all(r.lot for r in receipts if r.channel.value in ("purchase", "tefap"))
    assert not any(r.lot or r.upc for r in receipts if r.channel.value == "retail_rescue")
    assert len([r for r in receipts if r.channel.value == "salvage"]) == 1
    assert len([r for r in receipts if r.channel.value == "food_drive"]) == 1
    assert {r.storage for r in receipts} == {"frozen", "refrigerated", "dry"}
    assert all(2 <= r.cases <= 40 for r in receipts)
    # the brand column is often empty and the brand hides in the product text
    blank_brand = [r for r in receipts if not r.brand]
    assert len(blank_brand) >= 20
    assert any("Great Value" in r.product for r in blank_brand)
    assert "Great Value" in seeded_store.ledger_brands()


def test_on_hand_for_the_hero_receipt(seeded_store: Store):
    receipt = seeded_store.get_receipt(HERO)
    assert receipt is not None
    dists = seeded_store.list_distributions(HERO)
    assert sorted((d.shipped_at.isoformat(), d.cases) for d in dists) == [
        ("2026-08-26", 10), ("2026-09-01", 6), ("2026-09-04", 6),
    ]
    assert len({d.agency_id for d in dists}) == 3
    # one of the three is a same-day-distribution pantry: that is the one that gets a phone call
    agencies = {a.id: a for a in seeded_store.list_agencies()}
    assert any(agencies[d.agency_id].same_day_distribution for d in dists)

    assert shipped_cases(seeded_store, HERO) == 22
    assert seeded_store.on_hand(HERO) == receipt.cases - 22 == 8


def test_no_receipt_ships_more_than_it_received(seeded_store: Store):
    for receipt in seeded_store.list_receipts():
        shipped = shipped_cases(seeded_store, receipt.id)
        assert shipped <= receipt.cases, f"receipt {receipt.id} over-shipped"
        assert seeded_store.on_hand(receipt.id) == receipt.cases - shipped
    for d in seeded_store.list_distributions():
        receipt = seeded_store.get_receipt(d.receipt_id)
        assert receipt is not None and d.shipped_at >= receipt.received_at


def test_decoy_rows_exist_and_moved(seeded_store: Store):
    same_brand = seeded_store.get_receipt(seed_module.DECOY_SAME_BRAND_RECEIPT_ID)
    assert same_brand is not None
    assert (same_brand.brand, same_brand.product, same_brand.size) == (
        "Great Value", "Mixed Berries", "16 oz")
    assert same_brand.lot == "" and same_brand.storage == "frozen"

    ghirardelli = seeded_store.get_receipt(seed_module.DECOY_GHIRARDELLI_RECEIPT_ID)
    assert ghirardelli is not None and ghirardelli.brand == "Ghirardelli" and ghirardelli.lot

    ambiguous = seeded_store.get_receipt(seed_module.DECOY_AMBIGUOUS_RECEIPT_ID)
    assert ambiguous is not None
    assert ambiguous.brand == "" and ambiguous.lot == ""
    assert ambiguous.product == "Organic Whole Blueberries 10 oz frozen"
    assert ambiguous.donor.startswith("Publix #0455")

    salvage = seeded_store.get_receipt(seed_module.SALVAGE_RECEIPT_ID)
    assert salvage is not None and salvage.channel.value == "salvage" and salvage.lot == ""

    for receipt_id in (seed_module.DECOY_SAME_BRAND_RECEIPT_ID, seed_module.DECOY_GHIRARDELLI_RECEIPT_ID,
                       seed_module.DECOY_AMBIGUOUS_RECEIPT_ID, seed_module.SALVAGE_RECEIPT_ID):
        assert seeded_store.list_distributions(receipt_id), f"decoy {receipt_id} never moved"


# --------------------------------------------------------------------------------------------------
# PLANTED DEFECT 1: corrupt the distribution log, the arithmetic must move with it
# --------------------------------------------------------------------------------------------------
def test_planted_defect_extra_shipped_case_changes_on_hand(seeded_store: Store):
    """A ledger that shipped 23 cases of receipt 17 must report 7 on hand, not 8.

    The assertion is computed from the data, then counter-checked against the pristine value, so a
    hardcoded `8` (or a hardcoded `7`) fails.
    """
    receipt = seeded_store.get_receipt(HERO)
    assert receipt is not None
    pristine_shipped = shipped_cases(seeded_store, HERO)
    pristine_on_hand = seeded_store.on_hand(HERO)
    assert (pristine_shipped, pristine_on_hand) == (22, 8)

    # plant the defect: one extra case leaves the dock
    seeded_store.add_distribution(Distribution(
        id=9001, receipt_id=HERO, agency_id="palmetto-bay-community",
        shipped_at=datetime(2026, 9, 8).date(), cases=1))

    corrupted_shipped = shipped_cases(seeded_store, HERO)
    assert corrupted_shipped == pristine_shipped + 1 == 23
    assert seeded_store.on_hand(HERO) == receipt.cases - corrupted_shipped == 7
    assert seeded_store.on_hand(HERO) != pristine_on_hand


def test_on_hand_never_goes_negative_and_is_zero_for_unknown_receipts(seeded_store: Store):
    seeded_store.add_distribution(Distribution(
        id=9002, receipt_id=HERO, agency_id="goulds-mobile",
        shipped_at=datetime(2026, 9, 9).date(), cases=999))
    assert seeded_store.on_hand(HERO) == 0
    assert seeded_store.on_hand(99999) == 0


# --------------------------------------------------------------------------------------------------
# holds
# --------------------------------------------------------------------------------------------------
def test_holds_set_and_clear(seeded_store: Store):
    assert seeded_store.holds() == {}
    seeded_store.set_hold(HERO, "case-001", True)
    seeded_store.set_hold(48, "case-001", True)
    assert seeded_store.holds() == {HERO: "case-001", 48: "case-001"}

    seeded_store.set_hold(48, "case-001", False)
    assert seeded_store.holds() == {HERO: "case-001"}

    # re-tagging a held row for another case moves it, it does not duplicate
    seeded_store.set_hold(HERO, "case-002", True)
    assert seeded_store.holds() == {HERO: "case-002"}

    seeded_store.set_hold(HERO, "case-002", False)
    assert seeded_store.holds() == {}


# --------------------------------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------------------------------
def test_case_create_save_get_list(store: Store):
    case = make_case(store, "case-001")
    fetched = store.get_case("case-001")
    assert fetched is not None
    assert fetched.status == CaseStatus.NEW
    assert fetched.notice.recall_number == "H-1181-2026"
    assert fetched.notice.products[0].lots == ["6040 01-6"]

    case.status = CaseStatus.AWAITING_APPROVAL
    case.verdict = None
    store.save_case(case)
    assert store.get_case("case-001").status == CaseStatus.AWAITING_APPROVAL

    make_case(store, "case-002", status=CaseStatus.DISMISSED,
              url="https://www.fda.gov/other", recall_number="F-0001-2026")
    make_case(store, "drill-001", is_drill=True,
              url="https://www.fda.gov/drill", recall_number="DRILL-1")

    assert {c.id for c in store.list_cases()} == {"case-001", "case-002", "drill-001"}
    assert {c.id for c in store.list_cases(include_drills=False)} == {"case-001", "case-002"}
    assert [c.id for c in store.list_cases(status=CaseStatus.DISMISSED)] == ["case-002"]
    assert store.get_case("nope") is None


def test_find_case_by_source_by_url_and_recall_number(store: Store):
    """Rule 14: the same notice arriving twice must find the existing case, never open a second one."""
    make_case(store, "case-001")
    found_by_url = store.find_case_by_source("https://www.fda.gov/safety/recalls/demo")
    assert found_by_url is not None and found_by_url.id == "case-001"

    found_by_number = store.find_case_by_source("H-1181-2026")
    assert found_by_number is not None and found_by_number.id == "case-001"

    assert store.find_case_by_source("https://www.fda.gov/unseen") is None
    assert store.find_case_by_source("H-9999-2026") is None
    assert store.find_case_by_source("") is None


# --------------------------------------------------------------------------------------------------
# PLANTED DEFECT 2: a drill must never satisfy a real lookup
# --------------------------------------------------------------------------------------------------
def test_planted_defect_drill_case_is_not_returned_by_find_case_by_source(store: Store):
    """Rule 14: drills are labeled and never touch real state. A drill on the same URL and the same
    recall number must not shadow a real notice, or the real recall would be silently swallowed."""
    make_case(store, "drill-001", is_drill=True)   # same URL and recall number as the real notice
    assert store.list_cases() and store.list_cases()[0].is_drill is True
    assert store.find_case_by_source("https://www.fda.gov/safety/recalls/demo") is None
    assert store.find_case_by_source("H-1181-2026") is None

    make_case(store, "case-001")                   # now the real one arrives
    found = store.find_case_by_source("H-1181-2026")
    assert found is not None and found.id == "case-001" and found.is_drill is False


def test_find_case_by_source_ignores_blank_recall_numbers(store: Store):
    """Two cases with no recall number must not collide on the empty string."""
    make_case(store, "case-001", url="https://example.org/a", recall_number="")
    make_case(store, "case-002", url="https://example.org/b", recall_number="")
    assert store.find_case_by_source("https://example.org/a").id == "case-001"
    assert store.find_case_by_source("https://example.org/b").id == "case-002"
    assert store.find_case_by_source("") is None


# --------------------------------------------------------------------------------------------------
# tokens, responses
# --------------------------------------------------------------------------------------------------
def test_tokens_issue_and_resolve(store: Store):
    make_case(store, "case-001")
    token = store.issue_token("case-001", "nueva-vida")
    assert token and isinstance(token, str)
    assert store.resolve_token(token) == ("case-001", "nueva-vida")
    assert store.resolve_token("not-a-token") is None
    assert store.issue_token("case-001", "nueva-vida") != token   # one-time links, not a shared secret


def test_responses_and_latest_response(store: Store):
    make_case(store, "case-001")
    token = store.issue_token("case-001", "nueva-vida")

    assert store.responses("case-001") == []
    assert store.latest_response("case-001", "nueva-vida") is None

    store.record_response("case-001", "nueva-vida", ResponseStatus.NEVER_RECEIVED, token=token)
    store.advance_clock(1)
    store.record_response("case-001", "nueva-vida", ResponseStatus.PULLED, count=10,
                          free_text="found 10 cases in the walk-in", token=token)
    store.record_response("case-001", "perrine-senior", ResponseStatus.ALREADY_DISTRIBUTED, count=6)

    rows = store.responses("case-001")
    assert len(rows) == 3
    assert [r["agency_id"] for r in rows] == ["nueva-vida", "nueva-vida", "perrine-senior"]

    latest = store.latest_response("case-001", "nueva-vida")
    assert latest is not None
    assert latest["status"] == ResponseStatus.PULLED.value      # corrected answer wins, rule 9
    assert latest["count"] == 10
    assert latest["free_text"] == "found 10 cases in the walk-in"
    assert store.latest_response("case-001", "homestead-bethel") is None


# --------------------------------------------------------------------------------------------------
# follow-ups and the demo clock
# --------------------------------------------------------------------------------------------------
def test_followups_schedule_due_mark_and_cancel(store: Store):
    make_case(store, "case-001")
    now = store.now()
    reminder = store.schedule_followup("case-001", "nueva-vida", now + timedelta(hours=24), "reminder")
    escalation = store.schedule_followup("case-001", "nueva-vida", now + timedelta(hours=48), "escalation")
    other = store.schedule_followup("case-001", "perrine-senior", now + timedelta(hours=24), "reminder")
    assert reminder and escalation and other

    assert store.due_followups() == []                       # Class I cadence is 24h, nothing is due yet
    assert len(store.followups("case-001")) == 3

    store.advance_clock(25)                                  # demo clock jumps a day
    due = store.due_followups()
    assert {row["id"] for row in due} == {reminder, other}
    assert all(row["kind"] == "reminder" for row in due)

    store.mark_followup(reminder)
    assert {row["id"] for row in store.due_followups()} == {other}
    marked = [f for f in store.followups("case-001") if f["id"] == reminder][0]
    assert marked["sent_at"] is not None

    # an agency that answers is never chased again (rule 10: never auto-confirm, but never re-ping either)
    store.cancel_followups("case-001", "perrine-senior")
    assert {f["id"] for f in store.followups("case-001")} == {reminder, escalation}
    assert store.due_followups() == []

    store.advance_clock(25)
    assert {row["id"] for row in store.due_followups()} == {escalation}


def test_cancel_followups_leaves_already_sent_rows_alone(store: Store):
    make_case(store, "case-001")
    sent = store.schedule_followup("case-001", "nueva-vida", store.now() - timedelta(hours=1), "reminder")
    store.mark_followup(sent)
    store.cancel_followups("case-001", "nueva-vida")
    assert [f["id"] for f in store.followups("case-001")] == [sent]     # the audit trail survives


def test_clock_advances_and_resets(store: Store):
    t0 = store.now()
    store.advance_clock(25)
    assert store.now() - t0 >= timedelta(hours=24)
    store.reset_clock()
    assert abs((store.now() - t0).total_seconds()) < 60


# --------------------------------------------------------------------------------------------------
# outbox, audit, decisions
# --------------------------------------------------------------------------------------------------
def test_outbox_records_and_filters(store: Store):
    make_case(store, "case-001")
    first = store.record_mail("case-001", "nueva-vida", "nueva-vida@example.org",
                              "Recall H-1181-2026: action needed", "body", "notice", token="tok-1")
    second = store.record_mail("case-001", "perrine-senior", "perrine-senior@example.org",
                               "Recall H-1181-2026: action needed", "body", "notice")
    third = store.record_mail("case-001", None, "coordinator@example.org", "Weekly digest", "body", "digest")
    assert first < second < third

    assert len(store.outbox()) == 3
    assert len(store.outbox(case_id="case-001")) == 3
    assert [m["id"] for m in store.outbox(agency_id="nueva-vida")] == [first]
    assert store.outbox(case_id="case-002") == []
    mail = store.outbox(agency_id="nueva-vida")[0]
    assert mail["kind"] == "notice" and mail["backend"] == "mirror" and mail["token"] == "tok-1"
    assert mail["sent_at"]


def test_audit_events_are_ordered_and_scoped(store: Store):
    make_case(store, "case-001")
    make_case(store, "case-002", url="https://example.org/b", recall_number="F-0002-2026")
    store.audit("case-001", "agent", "intake", "notice ingested from fda_rss")
    store.audit("case-001", "coordinator", "approval", "approved 3 notices")
    store.audit("case-002", "system", "dismiss", "distribution excluded FL")

    events = store.events("case-001")
    assert [e.kind for e in events] == ["intake", "approval"]
    assert [e.actor for e in events] == ["agent", "coordinator"]
    assert [e.case_id for e in store.events("case-002")] == ["case-002"]
    assert [e.kind for e in store.recent_events()] == ["intake", "approval", "dismiss"]


def test_decisions_remember_and_query(store: Store):
    """Rule 14: the coordinator's resolution of the ambiguous blueberry row has to stick."""
    decision = store.remember(
        "Receipt 34 Organic Whole Blueberries was Driscoll's, not the recalled firm; do not flag again",
        kind="row_correction")
    store.remember("We have never received Ghirardelli baking powder", kind="brand_never_received")

    assert decision.id > 0 and decision.kind == "row_correction"
    assert len(store.decisions()) == 2
    hits = store.decisions("driscoll")
    assert len(hits) == 1 and hits[0].id == decision.id
    assert [d.kind for d in store.decisions("ghirardelli")] == ["brand_never_received"]
    assert store.decisions("zzzzz") == []


# --------------------------------------------------------------------------------------------------
# demo reset
# --------------------------------------------------------------------------------------------------
def _populate_everything(store: Store) -> None:
    make_case(store, "case-001")
    store.set_hold(HERO, "case-001", True)
    token = store.issue_token("case-001", "nueva-vida")
    store.record_response("case-001", "nueva-vida", ResponseStatus.PULLED, count=10, token=token)
    store.schedule_followup("case-001", "nueva-vida", store.now() + timedelta(hours=24), "reminder")
    store.audit("case-001", "agent", "intake", "ingested")
    store.record_mail("case-001", "nueva-vida", "nueva-vida@example.org", "subject", "body", "notice")
    store.advance_clock(5)
    store.remember("keep me", kind="other")


def test_reset_demo_wipes_case_state_but_keeps_the_ledger(seeded_store: Store):
    _populate_everything(seeded_store)
    assert seeded_store.list_cases() and seeded_store.holds() and seeded_store.outbox()

    before = seeded_store.now()
    seeded_store.reset_demo()

    assert seeded_store.list_cases() == []
    assert seeded_store.holds() == {}
    assert seeded_store.responses("case-001") == []
    assert seeded_store.followups("case-001") == []
    assert seeded_store.outbox() == []
    assert seeded_store.events("case-001") == []
    assert seeded_store.now() < before                       # clock offset rolled back

    # the ledger is the food bank's own data: a demo reset must not touch it
    assert len(seeded_store.list_agencies()) == 12
    assert len(seeded_store.list_receipts()) == 60
    assert len(seeded_store.list_distributions()) == 140
    assert seeded_store.on_hand(HERO) == 8
    assert len(seeded_store.decisions()) == 1                # remembered decisions outlive a demo reset


def test_wipe_all_clears_the_ledger_too(seeded_store: Store):
    _populate_everything(seeded_store)
    seeded_store.wipe_all()
    assert seeded_store.list_agencies() == []
    assert seeded_store.list_receipts() == []
    assert seeded_store.list_distributions() == []
    assert seeded_store.list_cases() == []
    assert seeded_store.holds() == {}
    assert seeded_store.outbox() == []
    assert seeded_store.on_hand(HERO) == 0


def test_seeding_is_idempotent(store: Store):
    seed_module.load_seed(store)
    seed_module.load_seed(store)
    assert len(store.list_agencies()) == 12
    assert len(store.list_receipts()) == 60
    assert len(store.list_distributions()) == 140
    assert store.on_hand(HERO) == 8


def test_store_creates_its_parent_directory(tmp_path: Path):
    db = tmp_path / "nested" / "deeper" / "recall.db"
    Store(db)
    assert db.exists()


@pytest.mark.parametrize("kind", ["agencies", "receipts", "distributions"])
def test_import_csv_rejects_unknown_kinds(store: Store, ledger_fixture_dir: Path, kind: str):
    with pytest.raises(ValueError):
        store.import_csv("not_a_kind", ledger_fixture_dir / seed_module.CSV_FILES[kind])
