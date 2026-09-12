"""ONE live run of the Strands agent layer against the configured provider.

Builds the hero recall and a minimal receiving ledger in a throwaway SQLite file, runs the real
orchestrator (real matcher and writer sub-agents, real hooks), prints the tool calls as they stream, then
the verdict, the pull list, the ping, and finally -- after approving -- the outbox subjects.

This costs real money. It is deliberately one run.

    .venv/Scripts/python.exe scripts/agent_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone
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
from recall_relay.core.config import settings  # noqa: E402
from recall_relay.core.models import (  # noqa: E402
    Agency,
    Distribution,
    ProductLine,
    Receipt,
    ReceiptChannel,
    RecallNotice,
    Source,
)
from recall_relay.core.store import Store  # noqa: E402

HERO_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "frutas-y-hortalizas-del-sur-sa-expands-recall-include-one-lot-great-value-frozen-organic-triple"
)
HERO_DISPOSITION = (
    "Consumers who have purchased the affected Great Value Organic Triple Berry Blend 10 oz are urged "
    "not to consume the product and to return it to the place of purchase for a full refund."
)


DB_PATH = ROOT / "data" / "runtime" / "agent_smoke.db"


def build_store(*, fresh: bool = True) -> Store:
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
    store = Store(DB_PATH)
    for agency in (
        Agency(
            id="AG-A", name="Redland Church Pantry", kind="church pantry", open_schedule="Tue/Thu 9-12",
            same_day_distribution=False, languages=["en"], contact_name="Maria Ruiz",
            contact_email="pantry-a@example.org", contact_phone="305-555-0101",
        ),
        Agency(
            id="AG-B", name="Homestead Soup Kitchen", kind="soup kitchen", open_schedule="daily 11-1",
            same_day_distribution=True, languages=["en"], contact_name="Andre Pierre",
            contact_email="pantry-b@example.org", contact_phone="305-555-0102",
        ),
        Agency(
            id="AG-C", name="Perrine School Pantry", kind="school pantry",
            open_schedule="first Saturday monthly", same_day_distribution=False, languages=["en"],
            contact_name="Dana Webb", contact_email="pantry-c@example.org", contact_phone="305-555-0103",
        ),
    ):
        store.upsert_agency(agency)

    store.upsert_receipt(
        Receipt(
            id=17, received_at=date(2026, 8, 24), donor="Walmart #1234 retail rescue",
            channel=ReceiptChannel.RETAIL_RESCUE, brand="Great Value",
            product="Organic Triple Berry Blend", size="10 oz", upc="", lot="", cases=30,
            storage="frozen", notes="no lot captured at pickup",
        )
    )
    store.upsert_receipt(
        Receipt(
            id=18, received_at=date(2026, 8, 24), donor="Walmart #1234 retail rescue",
            channel=ReceiptChannel.RETAIL_RESCUE, brand="Great Value", product="Mixed Berries",
            size="16 oz", upc="", lot="B4417", cases=12, storage="frozen",
            notes="decoy: same brand, different product and size",
        )
    )
    store.add_distribution(Distribution(id=1, receipt_id=17, agency_id="AG-A", shipped_at=date(2026, 8, 26), cases=10))
    store.add_distribution(Distribution(id=2, receipt_id=17, agency_id="AG-B", shipped_at=date(2026, 9, 1), cases=6))
    store.add_distribution(Distribution(id=3, receipt_id=17, agency_id="AG-C", shipped_at=date(2026, 9, 4), cases=6))
    return store


def hero_notice() -> RecallNotice:
    return RecallNotice(
        source=Source.FDA_RSS,
        source_url=HERO_URL,
        source_seen_at=datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc),
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[
            ProductLine(
                brand="Great Value", name="Organic Triple Berry Blend", size="10 oz",
                upcs_as_printed=["7874211226"], lots=["6040 01-6"], best_by=["February 9, 2028"],
            )
        ],
        reason="E. coli O145",
        classification=None,
        announcement_date=date(2026, 9, 2),
        publish_date=date(2026, 9, 3),
        distribution_states=["FL", "TX", "GA"],
        distribution_text="shipped to select Walmart stores in 27 states including Florida",
        disposition_verbatim=HERO_DISPOSITION,
        title="Frutas y Hortalizas Del Sur S.A. Expands Recall to Include One Lot of Great Value Frozen "
        "Organic Triple Berry Blend",
    )


def _usage(agent, label: str) -> str:
    try:
        u = agent.event_loop_metrics.accumulated_usage
        return f"  {label:<14} in={u['inputTokens']:>6}  out={u['outputTokens']:>6}  total={u['totalTokens']:>6}"
    except Exception:
        return f"  {label:<14} (no usage recorded)"


async def main(resume: bool = False) -> int:
    print("=" * 78)
    print(f"provider={settings.model_provider}  orchestrator={model_id_for('orchestrator')}")
    print(f"matcher={model_id_for('matcher')}  writer={model_id_for('writer')}")
    print("=" * 78)

    notice = hero_notice()
    matcher_agent = writer_agent = sign_agent = agent = None

    if resume and DB_PATH.exists():
        # Re-print a finished run without paying for the model again.
        store = build_store(fresh=False)
        case = next(
            (c for c in store.list_cases() if c.status.value == "awaiting_approval"), None
        )
        if case is None:
            print("--resume: no awaiting_approval case in the smoke db; run without --resume")
            return 2
        print(f"\n--- resumed from {DB_PATH.name} (no model calls) ---------------------------")
        for event in store.events(case.id):
            if event.kind == "tool_call":
                print(f"  -> tool: {event.detail.split(' ', 1)[0]}")
    else:
        store = build_store()
        matcher_agent = build_matcher_agent()
        writer_agent = build_writer_agent()
        sign_agent = build_sign_agent()
        agent = orch.build_orchestrator(store)

        def sink(event: dict) -> None:
            if event["type"] == "tool":
                print(f"  -> tool: {event['name']}")

        print("\n--- orchestrator run -------------------------------------------------------")
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

    print(f"\nstatus: {case.status.value}")

    print("\n--- verdict ----------------------------------------------------------------")
    if case.verdict is None:
        print("  (none)")
    else:
        v = case.verdict
        print(f"  {v.verdict.value}  rows={v.matched_receipt_ids}  confidence={v.confidence:.2f}  "
              f"widening_applied={v.widening_applied}")
        print(f"  reason: {v.reason}")
        for line in v.evidence:
            print(f"    - {line}")

    print("\n--- pull list --------------------------------------------------------------")
    if case.pull_list is None:
        print("  (none)")
    else:
        p = case.pull_list
        print(f"  {p.summary}")
        for item in p.items:
            where = item.agency_id or "ON HAND (food bank)"
            when = item.shipped_at.isoformat() if item.shipped_at else "-"
            print(f"    receipt {item.receipt_id}: {item.cases:>3} cases -> {where:<22} {when}  "
                  f"lot_known={item.lot_known}  {item.note}")
        print(f"  holds now set: {store.holds()}")

    print("\n--- the one ping -----------------------------------------------------------")
    print(f"  {service.ping_text(case, store)}")

    print("\n--- drafts -----------------------------------------------------------------")
    for n in case.notices:
        print(f"  [{n.agency_id}] {n.subject}")
        print(f"      disposition verbatim intact: "
              f"{n.disposition_verbatim == notice.disposition_verbatim}")
    for s in case.signs:
        print(f"  [{s.agency_id}/{s.language}] {s.title} :: {s.what_to_do[:70]}")

    if case.status.value != "awaiting_approval":
        print("\n(no approval step: the case did not reach awaiting_approval)")
    else:
        print("\n--- coordinator approves ---------------------------------------------------")
        service.approve(store, case.id)
        for mail in store.outbox(case.id):
            print(f"  [{mail['kind']}] to {mail['to_addr']:<24} :: {mail['subject']}")
        print(f"  follow-ups scheduled: "
              f"{[(f['agency_id'], f['kind'], f['due_at']) for f in store.followups(case.id)]}")

    print("\n--- token usage ------------------------------------------------------------")
    if agent is None:
        print("  (resumed run: no model calls were made, so there is no usage to report)")
    else:
        print(_usage(agent, "orchestrator"))
        print(_usage(matcher_agent, "matcher"))
        print(_usage(writer_agent, "writer"))
        print(_usage(sign_agent, "sign writer"))

    print("\n--- audit trail ------------------------------------------------------------")
    for event in store.events(case.id):
        print(f"  {event.actor:<11} {event.kind:<22} {event.detail[:110]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(resume="--resume" in sys.argv)))
