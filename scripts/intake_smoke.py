"""Live smoke test for the intake layer. Hits the network; prints, never asserts.

    .venv/Scripts/python.exe scripts/intake_smoke.py

Polls the FDA recall RSS feed, fetches the first few food press pages, prints what the deterministic
parser pulled out of each, and looks up the hero recall (H-1181-2026) on openFDA. Nothing here calls a
model. Use it to confirm fda.gov is still answering a browser User-Agent and that the page layout has
not drifted away from the cached fixtures.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.recall_relay.core.config import settings  # noqa: E402
from app.recall_relay.core.intake import (  # noqa: E402
    fetch_url,
    openfda_lookup,
    parse_fda_press_page,
    poll_fda_rss,
    to_recall_notice,
)
from app.recall_relay.core.models import Source  # noqa: E402
from app.recall_relay.core.rules import looks_like_food  # noqa: E402

HERO_RECALL_NUMBER = "H-1181-2026"
MAX_PAGES = 3


def _line(char: str = "-", width: int = 88) -> None:
    print(char * width)


def _show(label: str, value, width: int = 200) -> None:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > width:
        text = text[: width - 3] + "..."
    print(f"  {label:<22} {text}")


def main() -> int:
    cache_dir = ROOT / "data" / "runtime" / "fetch_cache"
    print(f"user-agent : {settings.user_agent}")
    print(f"cache dir  : {cache_dir}")

    # ---------------------------------------------------------------- RSS
    _line("=")
    print("1. FDA recall RSS")
    _line("=")
    # snapshots go to runtime (gitignored), never into the pinned fixtures dir DECISIONS.md #6 relies on
    items = poll_fda_rss(cache_dir=ROOT / "data" / "runtime" / "rss_snapshots")
    print(f"items: {len(items)}")
    for item in items[:10]:
        print(f"  {item.published:%Y-%m-%d %H:%M UTC}  {item.title[:80]}")
    if not items:
        print("  !! the feed returned nothing -- check the network or the feed URL")

    # ------------------------------------------------------- press pages
    _line("=")
    print(f"2. First {MAX_PAGES} food press pages")
    _line("=")
    fetched = 0
    for item in items:
        if fetched >= MAX_PAGES:
            break
        # cheap pre-filter on the headline; Product Type on the page is the real decision
        if not looks_like_food(item.title, ""):
            print(f"\n[skip, headline looks non-food] {item.title[:70]}")
            continue

        print()
        _line()
        print(item.title[:88])
        print(item.link)
        _line()
        res = fetch_url(item.link, cache_dir=cache_dir)
        _show("http status", res.status)
        _show("blocked (abuse wall)", res.blocked)
        _show("from cache", res.from_cache)
        if res.blocked:
            _show("final url", res.final_url)
            print("  !! fda.gov walled this fetch; the cached fixture is the fallback")
            continue
        if res.status != 200 or not res.text:
            print("  !! no body to parse")
            continue

        fetched += 1
        raw = parse_fda_press_page(res.text, url=item.link)
        _show("product type", raw.product_type)
        _show("is_food", raw.is_food)
        _show("channel", raw.channel.value)
        _show("firm", raw.firm)
        _show("brands", raw.brand_names)
        _show("product", raw.product_description)
        _show("reason", raw.reason)
        _show("announced / published", f"{raw.company_announcement_date} / {raw.fda_publish_date}")
        _show("upcs as printed", raw.upcs_as_printed)
        _show("lots", raw.lots[:10] + (["..."] if len(raw.lots) > 10 else []))
        _show("best by", raw.best_by[:10] + (["..."] if len(raw.best_by) > 10 else []))
        _show("sizes", raw.sizes[:6])
        _show("states", raw.distribution_states)
        _show("distribution", raw.distribution_text)
        _show("disposition", raw.disposition_text)

        notice = to_recall_notice(raw, Source.FDA_RSS)
        _show("-> confidence", notice.extraction_confidence)
        _show("-> product lines", [p.name for p in notice.products])
        if notice.extraction_confidence < 0.8:
            print("  (below 0.8: the orchestrator would hand this to the extractor agent)")

    # ------------------------------------------------------------ openFDA
    _line("=")
    print(f"3. openFDA enforcement lookup: {HERO_RECALL_NUMBER} (the demo recall)")
    _line("=")
    enrichments = openfda_lookup(recall_number=HERO_RECALL_NUMBER, limit=1)
    if not enrichments:
        print("  no results (openFDA answers 404 for zero matches; normal for anything recent)")
    for e in enrichments:
        _show("recall number", e.recall_number)
        _show("classification", e.classification.value if e.classification else None)
        _show("status", e.status)
        _show("recalling firm", e.recalling_firm)
        _show("product", e.product_description)
        _show("code info", e.code_info)
        _show("distribution pattern", e.distribution_pattern)
        _show("initiated / reported", f"{e.recall_initiation_date} / {e.report_date}")

    print()
    print("4. openFDA zero-result probe (expect 0 results, not an exception)")
    print(f"   -> {len(openfda_lookup(recall_number='Z-9999-2099', limit=1))} results")

    print()
    print("smoke run complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
