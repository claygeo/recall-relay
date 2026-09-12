"""Offline tests for the Strands agent layer.

No network, no model calls. Two kinds of stand-in:

* `ScriptedModel` is a real `strands.models.Model` that emits a scripted sequence of tool-use turns. The
  actual Strands event loop, tool registry, hooks and tool executor all run -- which is the point: the
  ApprovalGuard test is only worth anything if the cancel goes through the real `BeforeToolCallEvent`.
* `FakeStructuredAgent` stands in for the matcher / writer sub-agents and returns fixed structured output,
  recording the prompt it was given so the tests can assert what actually reached the model.

The ledger is the hero scenario, inline: receipt 17 is 30 cases of Great Value Organic Triple Berry Blend
10 oz received 2026-08-24 with NO lot on the receipt, 8 still on hand and 22 shipped to three agencies.
Receipt 18 is the decoy: same brand, 16 oz Mixed Berries.
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from strands.models.model import Model

from recall_relay.agents import matcher as matcher_mod
from recall_relay.agents import orchestrator as orch
from recall_relay.agents import service, writer as writer_mod
from recall_relay.agents.hooks import ApprovalGuard, AuditHook, TerminalToolGuard
from recall_relay.core import rules
from recall_relay.core.models import (
    Agency,
    AgencyNotice,
    CaseStatus,
    ClientSign,
    Distribution,
    MatchVerdict,
    ProductLine,
    Receipt,
    ReceiptChannel,
    RecallNotice,
    ResponseStatus,
    Source,
    Verdict,
)
from recall_relay.core.store import Store

HERO_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "frutas-y-hortalizas-del-sur-sa-expands-recall-include-one-lot-great-value-frozen-organic-triple"
)
HERO_DISPOSITION = (
    "Consumers who have purchased the affected Great Value Organic Triple Berry Blend 10 oz are urged "
    "not to consume the product and to return it to the place of purchase for a full refund."
)


# ---------------------------------------------------------------------------
# stand-ins
# ---------------------------------------------------------------------------
class ScriptedModel(Model):
    """A Model that plays a fixed script of tool-use turns, then falls back to plain text.

    Each script step is either a list of (tool_name, input_dict) pairs or a string (a final text turn).
    """

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.turns = 0
        self.seen_system_prompts: list[str] = []

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover - unused
        pass

    def get_config(self) -> dict:
        return {}

    async def structured_output(  # type: ignore[override]
        self, output_model, prompt, system_prompt=None, **kwargs
    ) -> AsyncGenerator[dict, None]:  # pragma: no cover - unused
        raise NotImplementedError("ScriptedModel does not implement provider-side structured output")
        yield {}

    async def stream(  # type: ignore[override]
        self,
        messages,
        tool_specs=None,
        system_prompt=None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        if system_prompt:
            self.seen_system_prompts.append(system_prompt)
        step = self.script[self.turns] if self.turns < len(self.script) else "Nothing left to do."
        self.turns += 1

        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, str):
            yield {"contentBlockDelta": {"delta": {"text": step}, "contentBlockIndex": 0}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            for i, (name, args) in enumerate(step):
                yield {
                    "contentBlockStart": {
                        "start": {"toolUse": {"toolUseId": f"tu-{self.turns}-{i}", "name": name}},
                        "contentBlockIndex": i,
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps(args)}},
                        "contentBlockIndex": i,
                    }
                }
                yield {"contentBlockStop": {"contentBlockIndex": i}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 0},
            }
        }


class FakeStructuredAgent:
    """Stands in for a sub-agent invoked as `agent(prompt, structured_output_model=Model)`."""

    def __init__(self, produce):
        self.produce = produce
        self.prompts: list[str] = []
        self.models: list[Any] = []

    def __call__(self, prompt: str, structured_output_model=None, **kwargs):
        self.prompts.append(prompt)
        self.models.append(structured_output_model)
        value = self.produce(prompt) if callable(self.produce) else self.produce
        return SimpleNamespace(structured_output=value)


class ExplodingAgent:
    """Any call means we paid for a model we should not have."""

    def __call__(self, *args, **kwargs):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("the matcher agent was invoked when it should have short-circuited")


def size_aware_matcher(store: Store, notice: RecallNotice) -> FakeStructuredAgent:
    """A stand-in that applies the real discriminator (brand + size) rather than hard-coding row 17."""

    def produce(prompt: str) -> MatchVerdict:
        candidates = rules.score_candidates(store.list_receipts(), notice)
        matched, evidence = [], []
        for c in candidates:
            if c.brand_score >= 90 and c.size_score >= 100:
                matched.append(c.receipt_id)
                evidence.append(f"receipt {c.receipt_id}: brand, product line and size all agree")
            else:
                evidence.append(
                    f"receipt {c.receipt_id}: rejected, size {c.size!r} is not the recalled size"
                )
        return MatchVerdict(
            verdict=Verdict.MATCH if matched else Verdict.NO_MATCH,
            matched_receipt_ids=matched,
            evidence=evidence,
            confidence=0.92,
            widening_applied=False,
            reason="brand and product line match; size confirms",
        )

    return FakeStructuredAgent(produce)


def fake_writer_agent(*, mangle_disposition: bool = True) -> FakeStructuredAgent:
    """Writes a plausible notice AND rewrites the disposition, which rule 11 must undo."""

    def produce(prompt: str) -> AgencyNotice:
        payload = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
        agency_id = payload["agency"]["id"]
        return AgencyNotice(
            agency_id="WRONG-AGENCY" if False else agency_id,
            recall_number="",
            classification="unknown",
            product=payload["recall"]["product"],
            lots=[],
            best_by=[],
            reason=payload["recall"]["reason"],
            disposition_verbatim=(
                "Customers should throw the product away."
                if mangle_disposition
                else payload["recall"]["disposition_verbatim"]
            ),
            cases_shipped=0,
            shipped_dates=[],
            subject=f"RECALL Class I: {payload['recall']['product']}",
            body="Please pull this product from your shelves today and reply with what you found.",
            actions=[],
        )

    return FakeStructuredAgent(produce)


def fake_sign_agent() -> FakeStructuredAgent:
    def produce(prompt: str) -> ClientSign:
        payload = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
        return ClientSign(
            agency_id="unset",
            language="en",
            title="Food Recall Notice",
            product_line=payload["product"],
            what_to_do="Do not eat this food. Bring it back or throw it away.",
            symptoms="Stomach cramps, diarrhea, vomiting. Call a doctor if it does not stop.",
            agency_contact=payload["agency"]["contact_name"],
            source_url="",
        )

    return FakeStructuredAgent(produce)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "recall_relay_test.db")
    s.upsert_agency(
        Agency(
            id="AG-A",
            name="Redland Church Pantry",
            kind="church pantry",
            open_schedule="Tue/Thu 9-12",
            same_day_distribution=False,
            languages=["en"],
            contact_name="Maria Ruiz",
            contact_email="pantry-a@example.org",
            contact_phone="305-555-0101",
        )
    )
    s.upsert_agency(
        Agency(
            id="AG-B",
            name="Homestead Soup Kitchen",
            kind="soup kitchen",
            open_schedule="daily 11-1",
            same_day_distribution=True,
            languages=["en"],
            contact_name="Andre Pierre",
            contact_email="pantry-b@example.org",
            contact_phone="305-555-0102",
        )
    )
    s.upsert_agency(
        Agency(
            id="AG-C",
            name="Perrine School Pantry",
            kind="school pantry",
            open_schedule="first Saturday monthly",
            same_day_distribution=False,
            languages=["en"],
            contact_name="Dana Webb",
            contact_email="pantry-c@example.org",
            contact_phone="305-555-0103",
        )
    )
    s.upsert_receipt(
        Receipt(
            id=17,
            received_at=date(2026, 8, 24),
            donor="Walmart #1234 retail rescue",
            channel=ReceiptChannel.RETAIL_RESCUE,
            brand="Great Value",
            product="Organic Triple Berry Blend",
            size="10 oz",
            upc="",
            lot="",
            cases=30,
            storage="frozen",
            notes="no lot captured at pickup",
        )
    )
    s.upsert_receipt(
        Receipt(
            id=18,
            received_at=date(2026, 8, 24),
            donor="Walmart #1234 retail rescue",
            channel=ReceiptChannel.RETAIL_RESCUE,
            brand="Great Value",
            product="Mixed Berries",
            size="16 oz",
            upc="",
            lot="",
            cases=12,
            storage="frozen",
            notes="decoy: same brand, different product and size",
        )
    )
    s.add_distribution(Distribution(id=1, receipt_id=17, agency_id="AG-A", shipped_at=date(2026, 8, 26), cases=10))
    s.add_distribution(Distribution(id=2, receipt_id=17, agency_id="AG-B", shipped_at=date(2026, 9, 1), cases=6))
    s.add_distribution(Distribution(id=3, receipt_id=17, agency_id="AG-C", shipped_at=date(2026, 9, 4), cases=6))
    return s


@pytest.fixture
def hero_notice() -> RecallNotice:
    return RecallNotice(
        source=Source.FDA_RSS,
        source_url=HERO_URL,
        source_seen_at=datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc),
        recall_number="",
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[
            ProductLine(
                brand="Great Value",
                name="Organic Triple Berry Blend",
                size="10 oz",
                upcs_as_printed=["7874211226"],
                lots=["6040 01-6"],
                best_by=["February 9, 2028"],
            )
        ],
        reason="E. coli O145",
        classification=None,
        announcement_date=date(2026, 9, 2),
        publish_date=date(2026, 9, 3),
        distribution_states=["FL", "TX", "GA"],
        distribution_text="shipped to select Walmart stores in 27 states including Florida",
        disposition_verbatim=HERO_DISPOSITION,
        is_food=True,
        title="Frutas y Hortalizas Del Sur S.A. Expands Recall to Include One Lot of Great Value Frozen "
        "Organic Triple Berry Blend",
    )


HERO_SCRIPT = [
    [("load_case", {})],
    [("score_candidates", {})],
    [("adjudicate_candidates", {})],
    [("build_pull_list", {})],
    [("draft_notices", {})],
    [("request_approval", {})],
]


async def run_hero(store: Store, notice: RecallNotice, *, script=None, sink=None):
    matcher_agent = size_aware_matcher(store, notice)
    writer_agent = fake_writer_agent()
    sign_agent = fake_sign_agent()
    model = ScriptedModel(script if script is not None else HERO_SCRIPT)
    agent = orch.build_orchestrator(store, model=model)
    case = await service.process_notice(
        store,
        notice,
        event_sink=sink,
        orchestrator=agent,
        invocation_extras={
            "matcher_agent": matcher_agent,
            "writer_agent": writer_agent,
            "sign_agent": sign_agent,
        },
    )
    return case, SimpleNamespace(model=model, matcher=matcher_agent, writer=writer_agent, sign=sign_agent)


# ---------------------------------------------------------------------------
# the hero run
# ---------------------------------------------------------------------------
async def test_hero_case_ends_awaiting_approval_with_the_right_pull_list(store, hero_notice):
    events: list[dict] = []
    case, fakes = await run_hero(store, hero_notice, sink=events.append)

    assert case.status == CaseStatus.AWAITING_APPROVAL
    assert case.verdict is not None and case.verdict.verdict == Verdict.MATCH
    assert case.verdict.matched_receipt_ids == [17]
    assert case.verdict.widening_applied is True, "rule 4: a receipt with no lot widens"

    pull = case.pull_list
    assert pull is not None
    assert pull.on_hand_cases == 8
    assert sum(i.cases for i in pull.items if i.agency_id) == 22
    assert pull.agencies_affected == ["AG-A", "AG-B", "AG-C"]
    assert pull.widening_applied is True

    # rule 12: HOLD is automatic because it is reversible
    assert store.holds() == {17: case.id}
    assert 18 not in store.holds()

    assert len(case.notices) == 3
    assert len(case.signs) == 3  # Class I -> client notice required (rule 15)

    # the orchestrator worked the procedure in order
    tools = [e["name"] for e in events if e["type"] == "tool"]
    assert tools == [
        "load_case",
        "score_candidates",
        "adjudicate_candidates",
        "build_pull_list",
        "draft_notices",
        "request_approval",
    ]
    verdicts = [e for e in events if e["type"] == "verdict"]
    assert verdicts and verdicts[-1]["verdict"] == "MATCH"

    # nothing was sent: the agent does not send (rule 12)
    assert store.outbox(case.id) == []


async def test_decoy_16oz_is_a_candidate_but_never_matches(store, hero_notice):
    candidates = rules.score_candidates(store.list_receipts(), hero_notice)
    offered = {c.receipt_id for c in candidates}
    assert 18 in offered, "the decoy must reach the matcher, otherwise the test proves nothing"

    verdict = matcher_mod.adjudicate(
        hero_notice, candidates, store.decisions(), agent=size_aware_matcher(store, hero_notice)
    )
    assert verdict.verdict == Verdict.MATCH
    assert verdict.matched_receipt_ids == [17]
    assert any("16 oz" in line for line in verdict.evidence)


async def test_zero_candidates_short_circuits_without_a_model_call(hero_notice):
    verdict = matcher_mod.adjudicate(hero_notice, [], [], agent=ExplodingAgent())
    assert verdict.verdict == Verdict.NO_MATCH
    assert verdict.reason == "no candidate above floor"


def test_match_with_no_rows_is_downgraded_and_invented_rows_are_dropped(store, hero_notice):
    candidates = rules.score_candidates(store.list_receipts(), hero_notice)
    empty = MatchVerdict(
        verdict=Verdict.MATCH, matched_receipt_ids=[], evidence=[], confidence=0.9,
        widening_applied=False, reason="looks right",
    )
    assert matcher_mod.validate_verdict(empty, candidates).verdict == Verdict.NEEDS_HUMAN

    invented = MatchVerdict(
        verdict=Verdict.MATCH, matched_receipt_ids=[17, 999], evidence=[], confidence=0.9,
        widening_applied=True, reason="ok",
    )
    fixed = matcher_mod.validate_verdict(invented, candidates)
    assert fixed.matched_receipt_ids == [17]
    assert any("999" in line for line in fixed.evidence)


# ---------------------------------------------------------------------------
# rule 11: the disposition is not the writer's to improve
# ---------------------------------------------------------------------------
async def test_rule11_altered_disposition_is_overwritten_and_audited(store, hero_notice):
    case, fakes = await run_hero(store, hero_notice)

    assert fakes.writer.prompts, "the writer agent should have run once per affected agency"
    for notice in case.notices:
        assert notice.disposition_verbatim == HERO_DISPOSITION
        assert notice.actions == rules.RESPONSE_OPTIONS
        assert "pending" in notice.body.lower()  # recall number is pending on a press release
        assert HERO_URL in notice.body
        assert HERO_DISPOSITION in notice.body

    corrections = [e for e in store.events(case.id) if e.kind == "writer_correction"]
    assert any("disposition rewritten" in e.detail for e in corrections)


def test_sign_languages_cover_en_es_ht(store, hero_notice):
    agency = store.get_agency("AG-B")
    assert agency is not None
    agency.languages = ["en", "es", "ht"]
    store.upsert_agency(agency)
    assert writer_mod.sign_languages_for(agency) == ["en", "es", "ht"]

    case = service._open_case(store, hero_notice)
    signs = [
        writer_mod.draft_client_sign(case, agency, lang, agent=fake_sign_agent(), store=store)
        for lang in ("en", "es", "ht")
    ]
    assert [s.language for s in signs] == ["en", "es", "ht"]
    assert {s.agency_id for s in signs} == {"AG-B"}
    assert all(s.source_url == HERO_URL for s in signs)


# ---------------------------------------------------------------------------
# the approval gate
# ---------------------------------------------------------------------------
async def test_approval_guard_blocks_send_before_approval_and_allows_after(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    assert case.approved_at is None

    # planted defect: an agent that tries to relay before a human said yes
    rogue = orch.build_orchestrator(store, model=ScriptedModel([[("send_notices", {})], "done"]))
    async for _ in rogue.stream_async(
        "send it", invocation_state={"store": store, "case_id": case.id}
    ):
        pass

    assert store.outbox(case.id) == [], "no notice may leave the building before approval"
    blocked = [e for e in store.events(case.id) if e.kind == "send_blocked"]
    assert blocked and "approved_at" in blocked[-1].detail

    # now a human approves, and the same tool call goes through
    case.approved_at = store.now()
    store.save_case(case)
    allowed = orch.build_orchestrator(store, model=ScriptedModel([[("send_notices", {})], "done"]))
    async for _ in allowed.stream_async(
        "send it", invocation_state={"store": store, "case_id": case.id}
    ):
        pass

    assert len([m for m in store.outbox(case.id) if m["kind"] == "notice"]) == 3


async def test_approval_guard_blocks_every_send_on_a_drill(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    case.is_drill = True
    case.approved_at = store.now()
    store.save_case(case)

    agent = orch.build_orchestrator(store, model=ScriptedModel([[("send_notices", {})], "done"]))
    async for _ in agent.stream_async("send it", invocation_state={"store": store, "case_id": case.id}):
        pass

    assert store.outbox(case.id) == []
    assert any("drill" in e.detail for e in store.events(case.id) if e.kind == "send_blocked")


async def test_terminal_tool_ends_the_run(store, hero_notice):
    """Rule 12: the agent gets no second turn after request_approval, whatever its script says."""
    script = HERO_SCRIPT + [[("send_notices", {})], [("send_notices", {})]]
    case, fakes = await run_hero(store, hero_notice, script=script)
    assert case.status == CaseStatus.AWAITING_APPROVAL
    assert fakes.model.turns == len(HERO_SCRIPT), "the loop must stop at request_approval"
    assert store.outbox(case.id) == []


# ---------------------------------------------------------------------------
# approve -> relay -> responses -> follow-ups
# ---------------------------------------------------------------------------
async def test_approve_relays_notices_issues_tokens_and_schedules_class_i_followups(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    approved = service.approve(store, case.id)

    assert approved.status == CaseStatus.RELAYING
    assert approved.approved_at is not None

    mails = [m for m in store.outbox(case.id) if m["kind"] == "notice"]
    assert len(mails) == 3
    assert {m["agency_id"] for m in mails} == {"AG-A", "AG-B", "AG-C"}
    assert all(m["backend"] == "mirror" for m in mails)
    for m in mails:
        assert m["token"], "every agency gets its own response token"
        assert store.resolve_token(m["token"]) == (case.id, m["agency_id"])
        assert m["token"] in m["body"], "the one-click reply link carries the token"
        for option in rules.RESPONSE_OPTIONS:
            assert option.label in m["body"]

    fups = store.followups(case.id)
    assert len(fups) == 6
    assert sum(1 for f in fups if f["kind"] == "reminder") == 3
    assert sum(1 for f in fups if f["kind"] == "escalation") == 3
    reminder_h, escalate_h = rules.followup_cadence(rules.effective_classification(hero_notice))
    assert (reminder_h, escalate_h) == (24, 48)
    base = approved.approved_at
    for f in fups:
        due = datetime.fromisoformat(f["due_at"])
        offset = round((due - base).total_seconds() / 3600)
        assert offset == (24 if f["kind"] == "reminder" else 48)


async def test_already_distributed_sends_the_client_sign_and_cancels_that_agencys_followups(
    store, hero_notice
):
    case, _ = await run_hero(store, hero_notice)
    service.approve(store, case.id)
    token_b = next(m["token"] for m in store.outbox(case.id) if m["agency_id"] == "AG-B")

    service.record_response(
        store, token_b, ResponseStatus.ALREADY_DISTRIBUTED, None, "it went out Tuesday"
    )

    signs = [m for m in store.outbox(case.id) if m["kind"] == "client_sign"]
    assert len(signs) == 1 and signs[0]["agency_id"] == "AG-B"
    assert "Do not eat this food" in signs[0]["body"]

    pending_b = [f for f in store.followups(case.id) if f["agency_id"] == "AG-B" and f["sent_at"] is None]
    assert pending_b == []
    pending_others = {
        f["agency_id"] for f in store.followups(case.id) if f["sent_at"] is None
    }
    assert pending_others == {"AG-A", "AG-C"}


async def test_followups_remind_then_escalate_and_never_auto_confirm(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    service.approve(store, case.id)
    token_b = next(m["token"] for m in store.outbox(case.id) if m["agency_id"] == "AG-B")
    service.record_response(store, token_b, ResponseStatus.ALREADY_DISTRIBUTED)

    store.advance_clock(25)
    sent = service.run_followups(store)
    assert {(s["agency_id"], s["kind"]) for s in sent} == {("AG-A", "reminder"), ("AG-C", "reminder")}
    reminders = [m for m in store.outbox(case.id) if m["kind"] == "reminder"]
    assert {m["agency_id"] for m in reminders} == {"AG-A", "AG-C"}
    assert store.get_case(case.id).status == CaseStatus.CHASING

    store.advance_clock(24)  # now 49h after approval
    escalated = service.run_followups(store)
    assert {(s["agency_id"], s["kind"]) for s in escalated} == {
        ("AG-A", "escalation"),
        ("AG-C", "escalation"),
    }
    calls = [m for m in store.outbox(case.id) if m["kind"] == "escalation"]
    assert len(calls) == 2
    for m in calls:
        assert m["to_addr"] == service.settings.coordinator_email
        assert "IF THEY PULLED IT:" in m["body"]
        assert HERO_DISPOSITION in m["body"]

    # nobody was marked confirmed by silence (rule 10)
    responded = {r["agency_id"] for r in store.responses(case.id)}
    assert responded == {"AG-B"}


async def test_close_case_files_the_audit_packet(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    service.approve(store, case.id)
    for m in [m for m in store.outbox(case.id) if m["kind"] == "notice"]:
        service.record_response(store, m["token"], ResponseStatus.PULLED, 6)

    packet = service.close_case(store, case.id)
    assert packet.case_id == case.id
    assert packet.notices_sent == 3
    assert packet.matched_receipts == [17]
    assert packet.disposition_verbatim == HERO_DISPOSITION
    assert len(packet.responses) == 3
    assert packet.approvals and "approved relay" in packet.approvals[0]
    assert packet.elapsed_notice_to_full_trace
    assert store.get_case(case.id).status == CaseStatus.CLOSED
    assert any(e.kind == "ready_to_close" for e in packet.events)


# ---------------------------------------------------------------------------
# rule 14: remembered decisions
# ---------------------------------------------------------------------------
async def test_resolve_needs_human_remembers_the_decision_and_the_next_matcher_sees_it(store, hero_notice):
    script = [[("load_case", {})], [("mark_needs_human", {"reason": "is row 17 the recalled firm?"})]]
    case, _ = await run_hero(store, hero_notice, script=script)
    assert case.status == CaseStatus.NEEDS_HUMAN

    decision_text = "row 17 was Driscoll's, not the recalled firm"
    recorder = FakeStructuredAgent(
        MatchVerdict(
            verdict=Verdict.NO_MATCH, matched_receipt_ids=[], evidence=["excluded per coordinator"],
            confidence=0.9, widening_applied=False, reason="coordinator says row 17 is a different supplier",
        )
    )
    resolved = service.resolve_needs_human(
        store, case.id, decision_text, "NO_MATCH", matcher_agent=recorder
    )

    assert resolved.status == CaseStatus.DISMISSED
    assert resolved.verdict is not None and resolved.verdict.verdict == Verdict.NO_MATCH
    assert resolved.verdict.matched_receipt_ids == []
    assert [d.text for d in store.decisions()] == [decision_text]

    # the re-run already saw it...
    assert decision_text in recorder.prompts[-1]

    # ...and so does the next, unrelated adjudication (rule 14)
    later = FakeStructuredAgent(
        MatchVerdict(
            verdict=Verdict.NO_MATCH, matched_receipt_ids=[], evidence=[], confidence=0.9,
            widening_applied=False, reason="different supplier per decision on file",
        )
    )
    candidates = rules.score_candidates(store.list_receipts(), hero_notice)
    matcher_mod.adjudicate(hero_notice, candidates, store.decisions(), agent=later)
    assert decision_text in later.prompts[-1]
    assert "override" in later.prompts[-1].lower() or "overrides" in matcher_mod.SYSTEM_PROMPT.lower()


async def test_resolve_needs_human_match_rebuilds_the_pull_list(store, hero_notice):
    script = [[("load_case", {})], [("mark_needs_human", {"reason": "unsure about the 10 oz row"})]]
    case, _ = await run_hero(store, hero_notice, script=script)
    assert case.status == CaseStatus.NEEDS_HUMAN
    assert store.holds() == {}

    resolved = service.resolve_needs_human(
        store,
        case.id,
        "yes, receipt 17 is the recalled Great Value 10 oz blend",
        "MATCH",
        matcher_agent=size_aware_matcher(store, hero_notice),
        writer_agent=fake_writer_agent(),
        sign_agent=fake_sign_agent(),
    )
    assert resolved.status == CaseStatus.AWAITING_APPROVAL
    assert resolved.pull_list is not None and resolved.pull_list.on_hand_cases == 8
    assert len(resolved.notices) == 3
    assert store.holds() == {17: case.id}


# ---------------------------------------------------------------------------
# the ping
# ---------------------------------------------------------------------------
async def test_ping_text_says_everything_needed_to_answer_send_or_not(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    ping = service.ping_text(case, store)

    assert "FDA press release 9/3" in ping
    assert "Great Value Organic Triple Berry Blend 10 oz" in ping
    assert "lot 6040 01-6" in ping
    assert "E. coli O145" in ping
    assert "receipt 17, 30 cases received 8/24" in ping
    assert "all 30 are treated as affected" in ping
    assert "8 on hand (HOLD tag applied)" in ping
    assert "22 shipped to 3 agencies 8/26-9/4" in ping
    assert "one distributes same-day" in ping
    assert "3 agency notices" in ping and "3 shelf signs" in ping
    assert ping.rstrip().endswith("Send?")


# ---------------------------------------------------------------------------
# the audit hook
# ---------------------------------------------------------------------------
async def test_every_tool_call_is_on_the_record(store, hero_notice):
    case, _ = await run_hero(store, hero_notice)
    events = store.events(case.id)
    calls = [e for e in events if e.kind == "tool_call"]
    results = [e for e in events if e.kind == "tool_result"]
    assert len(calls) == 6 and len(results) == 6
    assert [e.detail.split(" ", 1)[0] for e in calls] == [
        "load_case",
        "score_candidates",
        "adjudicate_candidates",
        "build_pull_list",
        "draft_notices",
        "request_approval",
    ]
    assert all(e.actor == "agent" for e in calls + results)


def test_hooks_expose_the_invariants_a_judge_reads(store):
    audit = AuditHook(store)
    guard = ApprovalGuard(store)
    terminal = TerminalToolGuard(store)
    assert {"send_notices"} == set(__import__(
        "recall_relay.agents.hooks", fromlist=["SEND_TOOLS"]
    ).SEND_TOOLS)
    assert callable(audit.register_hooks) and callable(guard.register_hooks)
    assert callable(terminal.register_hooks)


# ---------------------------------------------------------------------------
# dismissal path (rule 8 is deterministic; NO_MATCH is the agent's call)
# ---------------------------------------------------------------------------
async def test_no_match_dismisses_silently(store, hero_notice):
    script = [
        [("load_case", {})],
        [("score_candidates", {})],
        [("adjudicate_candidates", {"receipt_ids": [18]})],
        [("dismiss_case", {"reason": "only a 16 oz Mixed Berries row scored; different product"})],
    ]

    def produce(prompt: str) -> MatchVerdict:
        return MatchVerdict(
            verdict=Verdict.NO_MATCH, matched_receipt_ids=[], evidence=["receipt 18: wrong size"],
            confidence=0.9, widening_applied=False, reason="16 oz Mixed Berries is a different product",
        )

    model = ScriptedModel(script)
    agent = orch.build_orchestrator(store, model=model)
    case = await service.process_notice(
        store,
        hero_notice,
        orchestrator=agent,
        invocation_extras={"matcher_agent": FakeStructuredAgent(produce)},
    )
    assert case.status == CaseStatus.DISMISSED
    assert "16 oz" in case.dismissed_reason
    assert store.outbox(case.id) == []
    assert store.holds() == {}
