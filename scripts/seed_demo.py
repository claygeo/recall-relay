#!/usr/bin/env python
"""Rebuild the demo database from the committed ledger CSVs.

    python scripts/seed_demo.py                     # wipe + reseed settings.db_path
    python scripts/seed_demo.py --db data/x.db      # somewhere else
    python scripts/seed_demo.py --regenerate        # rewrite data/fixtures/ledger/*.csv first
    python scripts/seed_demo.py --keep-cases        # reseed the ledger without dropping case state

Idempotent: the ledger tables are wiped and re-imported every run, so running it twice leaves exactly
12 agencies, 60 receipts and 140 distributions. By default it also clears case state (holds, cases,
responses, follow-ups, tokens, events, outbox) and resets the demo clock, which is what you want before
a recorded demo run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "app") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "app"))

from recall_relay.core import seed as seed_module            # noqa: E402
from recall_relay.core.config import settings                # noqa: E402
from recall_relay.core.store import Store                    # noqa: E402

LEDGER_DIR = settings.fixtures_dir / "ledger"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Recall Relay demo ledger.")
    parser.add_argument("--db", type=Path, default=settings.db_path,
                        help=f"SQLite path to seed (default: {settings.db_path})")
    parser.add_argument("--csv-dir", type=Path, default=LEDGER_DIR,
                        help=f"directory holding the three ledger CSVs (default: {LEDGER_DIR})")
    parser.add_argument("--regenerate", action="store_true",
                        help="rewrite the CSVs from the generator before importing them")
    parser.add_argument("--keep-cases", action="store_true",
                        help="keep cases, holds, responses, follow-ups and the outbox")
    args = parser.parse_args(argv)

    if args.regenerate:
        written = seed_module.write_csvs(args.csv_dir)
        print(f"regenerated CSVs in {args.csv_dir}: "
              f"{written['agencies']} / {written['receipts']} / {written['distributions']}")

    missing = [name for name in seed_module.CSV_FILES.values() if not (args.csv_dir / name).exists()]
    if missing:
        print(f"error: missing ledger CSVs in {args.csv_dir}: {', '.join(missing)}", file=sys.stderr)
        print("       run again with --regenerate to write them.", file=sys.stderr)
        return 2

    store = Store(args.db)
    if args.keep_cases:
        _wipe_ledger_only(store)
    else:
        store.wipe_all()

    counts = {kind: store.import_csv(kind, args.csv_dir / name)
              for kind, name in seed_module.CSV_FILES.items()}

    print(f"seeded {args.db}")
    print(f"  agencies      {counts['agencies']:>4}")
    print(f"  receipts      {counts['receipts']:>4}")
    print(f"  distributions {counts['distributions']:>4}")
    hero = store.get_receipt(seed_module.HERO_RECEIPT_ID)
    if hero:
        shipped = sum(d.cases for d in store.list_distributions(hero.id))
        print(f"  hero receipt {hero.id}: {hero.brand} {hero.product} {hero.size}, "
              f"{hero.cases} cases received, {shipped} shipped, {store.on_hand(hero.id)} on hand")

    expected = (seed_module.N_AGENCIES, seed_module.N_RECEIPTS, seed_module.N_DISTRIBUTIONS)
    actual = (counts["agencies"], counts["receipts"], counts["distributions"])
    if actual != expected:
        print(f"error: expected {expected}, imported {actual}", file=sys.stderr)
        return 1
    return 0


def _wipe_ledger_only(store: Store) -> None:
    """Clear only the ledger tables, leaving case state alone (used by --keep-cases)."""
    for table in ("agencies", "receipts", "distributions"):
        store._conn.execute(f"DELETE FROM {table}")   # noqa: SLF001 - no public ledger-only wipe exists
    store._conn.commit()                              # noqa: SLF001


if __name__ == "__main__":
    raise SystemExit(main())
