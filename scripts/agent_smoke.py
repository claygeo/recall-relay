"""ONE live run of the Strands agent layer against the configured provider.

Runs the two beats the demo is built on, against the real seeded ledger and the real intake parser:

  BEAT 1  the FDA press release (pinned fixture -> intake -> orchestrator -> matcher -> writer)
          ends AWAITING_APPROVAL; approving it relays the notices through the mirror mailer.
  BEAT 2  the openFDA enforcement record that never got a press release. The ledger row is unbranded,
          so the matcher is expected to escalate rather than guess (rule 5).

This costs real money. It is deliberately one run.

    .venv/Scripts/python.exe scripts/agent_smoke.py            # live
    .venv/Scripts/python.exe scripts/agent_smoke.py --resume   # re-print the last run, no model calls
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

# The Windows console defaults to cp1252 and the model writes em dashes and accented names.
# Without this the run dies on a print AFTER every paid model call has already happened.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

from recall_relay.agents import orchestrator as orch  # noqa: E402
from recall_relay.agents import service  # noqa: E402
from recall_relay.agents.matcher import build_matcher_agent  # noqa: E402
from recall_relay.agents.model_factory import model_id_for  # noqa: E402
from recall_relay.agents.writer import build_sign_agent, build_writer_agent  # noqa: E402
from recall_relay.core import intake  # noqa: E402
from recall_relay.core.config import settings  # noqa: E402
from recall_relay.core.models import CaseStatus, RecallCase, Source  # noqa: E402
from recall_relay.core.seed import load_seed  # noqa: E402
from recall_relay.core.store import Store  # noqa: E402

DB_PATH = ROOT / "data" / "runtime" / "agent_smoke.db"
HERO_FIXTURE = ROOT / "data" / "fixtures" / "press" / "hero-great-value-triple-berry.html"
HERO_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "frutas-y-hortalizas-del-sur-sa-expands-recall-include-one-lot-great-value-frozen-organic-triple"
)
BLUEBERRIES = "H-1181-2026"

RULE = "-" * 78


def build_store(*, fresh: bool = True) -> Store:
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
    store = Store(DB_PATH)
    if fresh:
        counts = load_seed(store)
        print(f"seeded ledger: {counts['agencies']} agencies, {counts['receipts']} receipts, "
              f"{counts['distributions']} distributions")
    return store


def hero_notice(store: Store):
    """The real parse path: the pinned FDA press page through intake, no shortcuts."""
    raw = intake.parse_fda_press_page(HERO_FIXTURE.read_text(encoding="utf-8"), HERO_URL)
    return service.notice_from_raw(raw, Source.FDA_RSS, datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc))


def openfda_notice(store: Store):
    records = {r["recall_number"]: r for r in service.openfda_snapshot_records()}
    return service.notice_from_openfda(records[BLUEBERRIES], store.now())


def _usage(agent, label: str) -> str:
    try:
        u = agent.event_loop_metrics.accumulated_usage
        return f"  {label:<14} in={u['inputTokens']:>7}  out={u['outputTokens']:>6}  total={u['totalTokens']:>7}"
    except Exception:
        return f"  {label:<14} (no usage recorded)"


def show_case(store: Store, case: RecallCase, *, with_drafts: bool) -> None:
    print(f"\nstatus: {case.status.value}")

    print(f"\n  verdict {RULE[:60]}")
    if case.verdict is None:
        print("    (none)")
    else:
        v = case.verdict
        print(f"    {v.verdict.value}  rows={v.matched_receipt_ids}  confidence={v.confidence:.2f}  "
              f"widening_applied={v.widening_applied}")
        print(f"    reason: {v.reason}")
        for line in v.evidence:
            print(f"      - {line}")

    print(f"\n  pull list {RULE[:57]}")
    if case.pull_list is None:
        print("    (none built: the case did not reach a MATCH)")
    else:
        p = case.pull_list
        print(f"    {p.summary}")
        for item in p.items:
            where = item.agency_id or "ON HAND (food bank)"
            when = item.shipped_at.isoformat() if item.shipped_at else "-"
            print(f"      receipt {item.receipt_id}: {item.cases:>3} cases -> {where:<22} {when}  "
                  f"lot_known={item.lot_known}")
    print(f"    holds now set: {store.holds()}")

    print(f"\n  the one ping {RULE[:54]}")
    print(f"    {service.ping_text(case, store)}")

    if with_drafts:
        print(f"\n  drafts {RULE[:60]}")
        for n in case.notices:
            print(f"    [{n.agency_id}] {n.subject}")
            print(f"        disposition verbatim intact: "
                  f"{n.disposition_verbatim == case.notice.disposition_verbatim}")
        for s in case.signs:
            print(f"    [{s.agency_id}/{s.language}] {s.title[:58]}")


async def main(resume: bool = False) -> int:
    print("=" * 78)
    print(f"provider={settings.model_provider}  orchestrator={model_id_for('orchestrator')}")
    print(f"matcher={model_id_for('matcher')}  writer={model_id_for('writer')}")
    print("=" * 78)

    if resume and DB_PATH.exists():
        store = build_store(fresh=False)
        for case in store.list_cases():
            print(f"\n### {case.id} ({case.notice.source.value}) {RULE[:40]}")
            show_case(store, case, with_drafts=bool(case.notices))
        print("\n(resumed run: no model calls were made, so there is no usage to report)")
        return 0

    store = build_store()
    matcher_agent = build_matcher_agent()
    writer_agent = build_writer_agent()
    sign_agent = build_sign_agent()
    extras = {"matcher_agent": matcher_agent, "writer_agent": writer_agent, "sign_agent": sign_agent}
    agents: list = []

    def factory():
        agent = orch.build_orchestrator(store)
        agents.append(agent)
        return agent

    # ------------------------------------------------------------------ BEAT 1
    print(f"\n### BEAT 1: FDA press release {RULE[:44]}")
    notice = hero_notice(store)
    print(f"  parsed: {notice.firm} | {[p.name for p in notice.products]} | "
          f"lots={[l for p in notice.products for l in p.lots]} | "
          f"confidence={notice.extraction_confidence}")
    case, decision, reason, _ = await service.work_notice(
        store, notice, agent_factory=factory, invocation_extras=extras
    )
    for event in store.events(case.id):
        if event.kind == "tool_call":
            print(f"  -> tool: {event.detail.split(' ', 1)[0]}")
    show_case(store, case, with_drafts=True)

    if case.status == CaseStatus.AWAITING_APPROVAL:
        print(f"\n  coordinator approves {RULE[:46]}")
        service.approve(store, case.id)
        for mail in store.outbox(case.id):
            print(f"    [{mail['kind']}] to {mail['to_addr']:<26} :: {mail['subject']}")
        fups = store.followups(case.id)
        print(f"    follow-ups: {sum(1 for f in fups if f['kind'] == 'reminder')} reminders, "
              f"{sum(1 for f in fups if f['kind'] == 'escalation')} escalations "
              f"(Class I cadence: +24h / +48h)")

    # ------------------------------------------------------------------ BEAT 2
    print(f"\n\n### BEAT 2: openFDA record with no press release {RULE[:26]}")
    notice2, enrichment = openfda_notice(store)
    print(f"  {notice2.recall_number}: {notice2.firm} | {[p.name for p in notice2.products]} | "
          f"lots={[l for p in notice2.products for l in p.lots]} | states={notice2.distribution_states}")
    same = service.find_same_recall_case(store, notice2)
    print(f"  already a case for this recall? {same.id if same else 'no'} "
          f"(rule 1: a duplicate would enrich, never re-ping)")
    case2, decision2, reason2, _ = await service.work_notice(
        store, notice2, agent_factory=factory, invocation_extras=extras
    )
    service.attach_enrichment(store, case2, enrichment)
    case2 = store.get_case(case2.id) or case2
    for event in store.events(case2.id):
        if event.kind == "tool_call":
            print(f"  -> tool: {event.detail.split(' ', 1)[0]}")
    show_case(store, case2, with_drafts=bool(case2.notices))
    print(f"\n  outbox for the escalated case: {store.outbox(case2.id)} "
          f"(nothing is sent on an unresolved ambiguity)")

    # ------------------------------------------------------------------ usage
    print(f"\n\n--- token usage {RULE[:60]}")
    total = 0
    for i, agent in enumerate(agents, start=1):
        print(_usage(agent, f"orchestrator{i}"))
        total += agent.event_loop_metrics.accumulated_usage["totalTokens"]
    for agent, label in ((matcher_agent, "matcher"), (writer_agent, "writer"), (sign_agent, "sign writer")):
        print(_usage(agent, label))
        total += agent.event_loop_metrics.accumulated_usage["totalTokens"]
    print(f"  {'TOTAL':<14} {total:>36}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(resume="--resume" in sys.argv)))
