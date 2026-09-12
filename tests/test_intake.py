"""Intake layer tests. Everything here runs OFFLINE against the fixtures in data/fixtures/.

Fixtures were captured live on 2026-09-11 (every URL returned HTTP 200; the openFDA zero-result probe
returned HTTP 404, which is openFDA's normal answer for "no matches"):

  press/hero-great-value-triple-berry.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/frutas-y-hortalizas-del-sur-sa-expands-recall-include-one-lot-great-value-frozen-organic-triple
  press/evergreen-broccoli-sprouts.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/evergreen-fresh-sprouts-llc-recalls-broccoli-sprouts-because-possible-health-risk
  press/made-fresh-salads.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/made-fresh-salads-inc-recalls-ready-eat-deli-style-salads-and-cream-cheese-because-possible-health
  press/ghirardelli-sweet-ground-white-chocolate.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/ghirardelli-chocolate-company-recalls-powdered-beverage-mixes-because-possible-health-risk
  press/freshpoint-chicken-salad-wedge.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/freshpoint-issues-recall-due-improperly-declared-allergen-egg-chicken-salad-wedge-sandwiches
  press/nonfood-bmc-luna-g3-device.html
      https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/bmc-medical-co-ltd-recalls-luna-g3-apap-model-lg3600-firmware-g3-20076-due-firmware-defect
  rss/recalls-2026-09-11.xml           https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml
  openfda/*.json   from https://api.fda.gov/food/enforcement.json with these query strings:
      H-1181-2026.json   search=recall_number:"H-1181-2026"&limit=1
      H-0844-2026.json   search=recall_number:"H-0844-2026"&limit=1
      ongoing-fl.json    search=status:"Ongoing"+AND+distribution_pattern:"FL"&sort=report_date:desc&limit=25
      (a zero-result query, e.g. recall_number:"Z-9999-2099", answers HTTP 404 with a NOT_FOUND body
       and so is reproduced by respx rather than saved as a fixture)

The PLANTED-DEFECT section near the bottom mutates a fixture so that a named property becomes false; each
of those tests was confirmed to FAIL against a deliberately broken implementation of the property it names.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from app.recall_relay.core import intake
from app.recall_relay.core.intake import (
    RawNotice,
    extract_states,
    fetch_url,
    openfda_lookup,
    parse_fda_press_page,
    parse_openfda_payload,
    parse_rss,
    parse_text,
    pdf_to_text,
    poll_fda_rss,
    to_recall_notice,
)
from app.recall_relay.core.models import Channel, Classification, Source

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "fixtures"
PRESS = FIXTURES / "press"
HERO_URL = (
    "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/"
    "frutas-y-hortalizas-del-sur-sa-expands-recall-include-one-lot-great-value-frozen-organic-triple"
)


def _press(slug: str) -> str:
    return (PRESS / f"{slug}.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def hero() -> RawNotice:
    return parse_fda_press_page(_press("hero-great-value-triple-berry"), url=HERO_URL)


@pytest.fixture(scope="module")
def evergreen() -> RawNotice:
    return parse_fda_press_page(_press("evergreen-broccoli-sprouts"))


# ===========================================================================
# Hero press page: the demo recall
# ===========================================================================
def test_hero_header_block(hero: RawNotice):
    assert "Frutas y Hortalizas" in hero.firm
    assert hero.brand_names == ["Great Value"]
    assert "Organic Triple Berry Blend" in hero.product_description
    assert hero.product_type == "Food & Beverages"
    assert hero.company_announcement_date == date(2026, 9, 2)
    assert hero.fda_publish_date == date(2026, 9, 3)
    assert hero.is_food is True
    assert hero.channel is Channel.FDA


def test_hero_codes_are_verbatim(hero: RawNotice):
    assert "7874211226" in hero.upcs_as_printed
    assert "6040 01-6" in hero.lots
    assert any("February 9, 2028" in b for b in hero.best_by)
    assert "10 oz" in [s.lower() for s in hero.sizes]


def test_hero_distribution_and_disposition(hero: RawNotice):
    assert "FL" in hero.distribution_states
    # the press release names 27 states; West Virginia must not be read as Virginia
    assert "WV" in hero.distribution_states and "VA" in hero.distribution_states
    assert "27 states" in hero.distribution_text
    assert hero.disposition_text
    assert "should not consume" in hero.disposition_text.lower()
    assert "full refund" in hero.disposition_text.lower()
    # rule 11: the disposition is copied, so it must not drag the spec block in with it
    assert "Package Size" not in hero.disposition_text


def test_hero_reason(hero: RawNotice):
    # verbatim from the page, which prints "Possible E. Coli Contamination" (FDA's own capitalisation)
    assert "e. coli" in hero.reason.lower()
    assert hero.reason == "Possible E. Coli Contamination"


def test_hero_to_recall_notice(hero: RawNotice):
    seen = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    notice = to_recall_notice(hero, Source.FDA_RSS, seen)
    assert notice.extraction_confidence == 1.0
    assert notice.source is Source.FDA_RSS
    assert notice.source_seen_at == seen
    assert notice.source_url == HERO_URL
    assert notice.classification is None  # openFDA enriches later; rule 2 treats None as Class I
    assert len(notice.products) == 1
    line = notice.products[0]
    assert line.brand == "Great Value"
    assert "Organic Triple Berry Blend" in line.name
    assert "7874211226" in line.upcs_as_printed
    assert "6040 01-6" in line.lots
    assert "FL" in notice.distribution_states
    assert notice.disposition_verbatim == hero.disposition_text
    assert 0 < len(notice.raw_excerpt) <= 1500


# ===========================================================================
# Evergreen: spaced UPC, three states, a five-entry use-by list
# ===========================================================================
def test_evergreen_upc_spacing_preserved(evergreen: RawNotice):
    assert "8 38796 00105 1" in evergreen.upcs_as_printed


def test_evergreen_states(evergreen: RawNotice):
    assert set(evergreen.distribution_states) == {"ID", "MT", "WA"}


def test_evergreen_use_by_list_expands(evergreen: RawNotice):
    assert len(evergreen.best_by) == 5
    assert evergreen.best_by == ["9/07/26", "9/09/26", "9/11/26", "9/14/26", "9/16/26"]


def test_evergreen_disposition(evergreen: RawNotice):
    assert "discard" in evergreen.disposition_text.lower()


# ===========================================================================
# The other cached pages
# ===========================================================================
def test_nonfood_device_page_is_not_food():
    raw = parse_fda_press_page(_press("nonfood-bmc-luna-g3-device"))
    assert raw.product_type == "Medical Devices"
    assert raw.is_food is False
    assert to_recall_notice(raw, Source.FDA_RSS).is_food is False


def test_nonfood_nationwide_yields_text_not_states():
    raw = parse_fda_press_page(_press("nonfood-bmc-luna-g3-device"))
    assert raw.distribution_states == []
    assert "nationwide" in raw.distribution_text.lower()


def test_made_fresh_salads_multi_brand_and_city_distribution():
    raw = parse_fda_press_page(_press("made-fresh-salads"))
    assert raw.brand_names == ["Made Fresh Salads", "Northside"]
    assert raw.distribution_states == ["NY"]  # "New York City" is NY, and only NY
    assert "place of purchase" in raw.disposition_text.lower()
    assert raw.is_food is True


def test_ghirardelli_table_lots_are_rowspan_aware():
    raw = parse_fda_press_page(_press("ghirardelli-sweet-ground-white-chocolate"))
    # the 50 oz (6/3.12lb) white chocolate sweet ground powder lots, from openFDA H-0844-2026
    for lot in ("S394260", "S494260", "S594260"):
        assert lot in raw.lots
    assert raw.firm == "Ghirardelli Chocolate Company"
    assert raw.brand_names == ["Ghirardelli"]
    assert raw.is_food is True


def test_freshpoint_table_upc_and_mislabelled_lot_column():
    """Found by the live smoke run: this page prints its UPC only inside a table, and the firm filled a
    column headed 'LOT CODE' with a use-by range. The cell's own words decide, not the column header."""
    raw = parse_fda_press_page(_press("freshpoint-chicken-salad-wedge"))
    assert raw.upcs_as_printed == ["766375109617"]
    assert raw.lots == [], "a use-by range in a LOT CODE column is not a lot"
    assert any("9/3/2026" in b for b in raw.best_by)
    assert any("9/17/2026" in b for b in raw.best_by)
    assert set(raw.distribution_states) == {"GA", "FL"}
    assert "discard" in raw.disposition_text.lower()
    assert to_recall_notice(raw, Source.FDA_RSS).extraction_confidence == 1.0


def test_ghirardelli_has_no_distribution_so_confidence_drops():
    """This release genuinely states no distribution; the parse must admit the gap, not invent one."""
    raw = parse_fda_press_page(_press("ghirardelli-sweet-ground-white-chocolate"))
    assert raw.distribution_states == [] and raw.distribution_text == ""
    notice = to_recall_notice(raw, Source.PASTED_URL)
    assert notice.extraction_confidence == 0.75  # below 0.8: the caller asks the extractor agent


# ===========================================================================
# State extraction properties
# ===========================================================================
@pytest.mark.parametrize(
    "text, expected",
    [
        ("distributed in West Virginia only", ["WV"]),
        ("distributed in Virginia and West Virginia", ["VA", "WV"]),
        ("sold in ID, MT, and WA", ["ID", "MT", "WA"]),
        ("shipped to Washington, D.C. stores", ["DC"]),
        ("shipped to Washington state", ["WA"]),
        ("sold in Arkansas and Kansas", ["AR", "KS"]),
        ("distributed nationwide", []),
        ("shipped to Puerto Rico", ["PR"]),
        ("", []),
    ],
)
def test_extract_states(text, expected):
    assert extract_states(text) == expected


def test_nationwide_is_text_only():
    raw = parse_text("Acme Foods recall\nThe product was distributed nationwide to retail stores.\n")
    assert raw.distribution_states == []
    assert "nationwide" in raw.distribution_text.lower()


# ===========================================================================
# parse_text: channels and the shared extractors
# ===========================================================================
def test_parse_text_fsis_channel():
    raw = parse_text(
        "FSIS Issues Public Health Alert for Ground Beef Products\n"
        "USDA's Food Safety and Inspection Service (FSIS) announced today.\n"
        "Lot Code: 9921 A4\n"
        "The products were shipped to distributors in FL, GA, and Alabama.\n"
        "Consumers who have purchased these products are urged to discard them.\n"
    )
    assert raw.channel is Channel.FSIS
    assert raw.is_food is True  # FSIS regulates nothing but food (rule 6)
    assert raw.lots == ["9921 A4"]
    assert set(raw.distribution_states) == {"FL", "GA", "AL"}
    assert "discard" in raw.disposition_text.lower()


def test_parse_text_usda_foods_channel():
    raw = parse_text(
        "USDA Foods Hold and Recall Notice - TEFAP\n"
        "Product Description: Canned Peaches in Light Syrup\n"
        "Lot 44-221 shipped to FL.\n"
        "Do not consume the product; hold for pickup.\n"
    )
    assert raw.channel is Channel.USDA_FOODS
    assert raw.product_description == "Canned Peaches in Light Syrup"
    assert raw.lots == ["44-221"]


def test_parse_text_inline_header_block_round_trips():
    raw = parse_text(
        "Great Value Organic Triple Berry Blend recall\n"
        "Company Name: Frutas y Hortalizas del Sur S.A.\n"
        "Brand Name: Great Value\n"
        "Product Description: Organic Triple Berry Blend\n"
        "Reason for Announcement: Possible E. Coli Contamination\n"
        "UPC : 7874211226\n"
        "Lot Code : 6040 01-6\n"
        "Best If Used By Date: February 9, 2028\n"
        "The product was shipped to Walmart stores in Florida and Georgia.\n"
        "Consumers should not consume it and should return it for a full refund.\n",
        source_url="paste://1",
    )
    assert raw.channel is Channel.FDA
    assert raw.firm == "Frutas y Hortalizas del Sur S.A."
    assert raw.brand_names == ["Great Value"]
    assert raw.upcs_as_printed == ["7874211226"]
    assert raw.lots == ["6040 01-6"]
    assert raw.best_by == ["February 9, 2028"]
    assert raw.distribution_states == ["FL", "GA"]
    assert to_recall_notice(raw, Source.PASTED_TEXT).extraction_confidence == 1.0


def test_parse_text_empty_is_safe():
    raw = parse_text("")
    assert raw.title == "" and raw.body_text == "" and raw.upcs_as_printed == []


def test_parse_fda_press_page_empty_is_safe():
    raw = parse_fda_press_page("")
    assert raw.firm == "" and raw.body_text == ""


# ===========================================================================
# to_recall_notice
# ===========================================================================
def test_confidence_penalties_are_quarter_each():
    base = RawNotice(firm="Acme", product_description="Peaches", reason="Listeria",
                     distribution_states=["FL"])
    assert to_recall_notice(base, Source.DRILL).extraction_confidence == 1.0

    no_firm = RawNotice(product_description="Peaches", reason="Listeria", distribution_states=["FL"])
    assert to_recall_notice(no_firm, Source.DRILL).extraction_confidence == 0.75

    no_firm_no_reason = RawNotice(product_description="Peaches", distribution_states=["FL"])
    assert to_recall_notice(no_firm_no_reason, Source.DRILL).extraction_confidence == 0.5

    nothing = RawNotice()
    assert to_recall_notice(nothing, Source.DRILL).extraction_confidence == 0.0


def test_distribution_text_alone_satisfies_the_distribution_field():
    raw = RawNotice(firm="Acme", product_description="Peaches", reason="Listeria",
                    distribution_text="distributed nationwide")
    assert to_recall_notice(raw, Source.DRILL).extraction_confidence == 1.0


def test_multiple_product_descriptions_become_multiple_lines():
    raw = RawNotice(firm="Acme", reason="Listeria", distribution_states=["FL"],
                    brand_names=["Acme"], sizes=["10 oz"], lots=["L1"],
                    product_description="Diced Peaches; Sliced Pears\nWhole Plums")
    notice = to_recall_notice(raw, Source.PASTED_TEXT)
    assert [p.name for p in notice.products] == ["Diced Peaches", "Sliced Pears", "Whole Plums"]
    assert all(p.brand == "Acme" and p.size == "10 oz" and p.lots == ["L1"] for p in notice.products)


def test_naive_seen_at_is_treated_as_utc():
    notice = to_recall_notice(RawNotice(firm="Acme"), Source.DRILL, datetime(2026, 9, 11, 8, 0))
    assert notice.source_seen_at.tzinfo is timezone.utc


# ===========================================================================
# RSS
# ===========================================================================
def test_rss_fixture_parses_newest_first():
    items = parse_rss((FIXTURES / "rss" / "recalls-2026-09-11.xml").read_text(encoding="utf-8"))
    assert len(items) == 20
    assert all(items[i].published >= items[i + 1].published for i in range(len(items) - 1))
    assert items[0].published.tzinfo is timezone.utc
    # "Thu, 10 Sep 2026 16:37:00 EDT" in the feed is 20:37 UTC
    assert items[0].published == datetime(2026, 9, 10, 20, 37, tzinfo=timezone.utc)
    assert "Evergreen Fresh Sprouts" in items[0].title
    assert all(i.link.startswith("http") for i in items)


def test_rss_fixture_contains_the_hero_and_non_food_items():
    items = parse_rss((FIXTURES / "rss" / "recalls-2026-09-11.xml").read_text(encoding="utf-8"))
    titles = " | ".join(i.title for i in items)
    assert "Frutas y Hortalizas" in titles  # the demo recall, pinned for the scan
    assert "BMC Medical" in titles          # the feed mixes devices in; intake does not filter


@respx.mock
def test_poll_fda_rss_live(tmp_path: Path):
    xml = (FIXTURES / "rss" / "recalls-2026-09-11.xml").read_text(encoding="utf-8")
    respx.get(intake.FDA_RSS_URL).mock(
        return_value=httpx.Response(200, text=xml, headers={"content-type": "application/rss+xml"})
    )
    items = poll_fda_rss(cache_dir=tmp_path)
    assert len(items) == 20
    assert list(tmp_path.glob("recalls-*.xml")), "a dated snapshot should be written"


@respx.mock
def test_poll_fda_rss_falls_back_to_the_snapshot_when_the_network_dies(tmp_path: Path):
    (tmp_path / "recalls-2026-09-11.xml").write_text(
        (FIXTURES / "rss" / "recalls-2026-09-11.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    respx.get(intake.FDA_RSS_URL).mock(side_effect=httpx.ConnectError("down"))
    items = poll_fda_rss(cache_dir=tmp_path)
    assert len(items) == 20  # the demo cannot be sunk by a fetch failure


@respx.mock
def test_poll_fda_rss_without_a_snapshot_returns_empty(tmp_path: Path):
    respx.get(intake.FDA_RSS_URL).mock(side_effect=httpx.ConnectError("down"))
    assert poll_fda_rss(cache_dir=tmp_path) == []


def test_parse_rss_empty():
    assert parse_rss("") == []


# ===========================================================================
# openFDA
# ===========================================================================
def _openfda(name: str) -> dict:
    return json.loads((FIXTURES / "openfda" / f"{name}.json").read_text(encoding="utf-8"))


def test_openfda_hero_enrichment_fixture():
    [e] = parse_openfda_payload(_openfda("H-1181-2026"))
    assert e.recall_number == "H-1181-2026"
    assert e.classification is Classification.CLASS_I
    assert e.report_date == date(2026, 7, 22)
    assert e.recall_initiation_date == date(2026, 7, 3)
    assert e.status == "Ongoing"
    assert "Frutas y Hortalizas" in e.recalling_firm
    assert "6040 01" in e.code_info
    assert "FL" in e.distribution_pattern


def test_openfda_ghirardelli_enrichment_fixture():
    [e] = parse_openfda_payload(_openfda("H-0844-2026"))
    assert e.recall_number == "H-0844-2026"
    assert e.classification is Classification.CLASS_I
    assert "S394260" in e.code_info
    assert "FL" in e.distribution_pattern


def test_openfda_broad_query_fixture():
    enrichments = parse_openfda_payload(_openfda("ongoing-fl"))
    assert len(enrichments) == 25
    assert all(e.recall_number for e in enrichments)
    assert all(e.report_date is not None for e in enrichments)


def test_openfda_payload_edges():
    assert parse_openfda_payload({}) == []
    assert parse_openfda_payload({"results": []}) == []
    [e] = parse_openfda_payload({"results": [{"recall_number": "X-1", "classification": "Class IV"}]})
    assert e.classification is None  # an unknown class is dropped, never guessed


def test_openfda_search_builder():
    assert intake.build_openfda_search(recall_number="H-1181-2026") == 'recall_number:"H-1181-2026"'
    # an exact recall number is unique, so no date window is bolted on to exclude it
    assert "report_date" not in intake.build_openfda_search(recall_number="H-1181-2026", since_days=7)
    s = intake.build_openfda_search(firm="Acme", product_terms="blueberries", since_days=30)
    assert s.startswith('recalling_firm:"Acme"+AND+product_description:"blueberries"+AND+report_date:[')
    assert intake.build_openfda_search(since_days=0) == ""
    assert openfda_lookup(since_days=0) == []  # no criteria: no request at all


@respx.mock
def test_openfda_lookup_returns_typed_enrichment():
    respx.get(host="api.fda.gov").mock(return_value=httpx.Response(200, json=_openfda("H-1181-2026")))
    [e] = openfda_lookup(recall_number="H-1181-2026", limit=1)
    assert e.recall_number == "H-1181-2026"
    assert e.report_date == date(2026, 7, 22)


@respx.mock
def test_openfda_404_is_zero_results_not_an_error():
    """openFDA answers 404 for 'no matches' -- normal for a recall younger than ~11 days."""
    body = {"error": {"code": "NOT_FOUND", "message": "No matches found!"}}
    respx.get(host="api.fda.gov").mock(return_value=httpx.Response(404, json=body))
    assert openfda_lookup(recall_number="H-9999-2099") == []


@respx.mock
def test_openfda_network_failure_returns_empty():
    respx.get(host="api.fda.gov").mock(side_effect=httpx.ConnectError("down"))
    assert openfda_lookup(recall_number="H-1181-2026") == []


@respx.mock
def test_openfda_bad_json_returns_empty():
    respx.get(host="api.fda.gov").mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    assert openfda_lookup(recall_number="H-1181-2026") == []


# ===========================================================================
# fetch_url
# ===========================================================================
@respx.mock
def test_fetch_url_sends_a_browser_user_agent():
    route = respx.get(HERO_URL).mock(return_value=httpx.Response(200, text="<html>ok</html>"))
    res = fetch_url(HERO_URL)
    assert res.status == 200 and res.blocked is False and res.from_cache is False
    assert "Mozilla" in route.calls[0].request.headers["user-agent"]


@respx.mock
def test_fetch_url_caches_then_serves_from_cache(tmp_path: Path):
    route = respx.get(HERO_URL).mock(return_value=httpx.Response(200, text="<html>hero</html>"))
    first = fetch_url(HERO_URL, cache_dir=tmp_path)
    assert first.from_cache is False and "hero" in first.text
    assert len(list(tmp_path.glob("*.html"))) == 1

    second = fetch_url(HERO_URL, cache_dir=tmp_path)
    assert second.from_cache is True and second.status == 200 and "hero" in second.text
    assert route.call_count == 1, "the second fetch must be served from cache, not the network"


@respx.mock
def test_fetch_url_http_error_returns_status_not_an_exception():
    respx.get(HERO_URL).mock(return_value=httpx.Response(503, text="oops"))
    res = fetch_url(HERO_URL)
    assert res.status == 503 and res.text == "" and res.blocked is False


@respx.mock
def test_fetch_url_network_failure_is_status_zero():
    respx.get(HERO_URL).mock(side_effect=httpx.ConnectError("no route to host"))
    res = fetch_url(HERO_URL)
    assert res.status == 0 and res.text == "" and res.blocked is False


@respx.mock
def test_fetch_url_does_not_cache_a_failure(tmp_path: Path):
    respx.get(HERO_URL).mock(return_value=httpx.Response(500, text="boom"))
    fetch_url(HERO_URL, cache_dir=tmp_path)
    assert list(tmp_path.glob("*.html")) == []


# ===========================================================================
# PDF
# ===========================================================================
def test_pdf_to_text_missing_file_is_empty_string():
    assert pdf_to_text("no-such-file.pdf") == ""


def test_pdf_to_text_garbage_bytes_is_empty_string():
    assert pdf_to_text(b"not a pdf at all") == ""


def test_pdf_to_text_reads_a_text_layer(tmp_path: Path):
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    path = tmp_path / "notice.pdf"
    c = reportlab.Canvas(str(path))
    c.drawString(72, 720, "Lot Code: 6040 01-6")
    c.save()
    assert "6040 01-6" in pdf_to_text(path)


# ===========================================================================
# PLANTED-DEFECT TESTS
# Each mutates a fixture so a named property becomes false. They fail if the code stops enforcing it.
# ===========================================================================
@respx.mock
def test_planted_abuse_wall_marker_in_body_is_blocked():
    """PROPERTY: a walled response is never returned as content.

    Planted defect: the abuse-wall marker is injected into an otherwise normal 200 HTML body. If the
    body check is dropped, fetch_url reports blocked=False and hands the wall page to the parser, which
    would silently produce an empty recall.
    """
    walled = _press("hero-great-value-triple-berry").replace(
        "<body", f'<body data-x="/apology_objects/{intake.ABUSE_WALL_MARKER}.html"', 1
    )
    assert intake.ABUSE_WALL_MARKER in walled
    respx.get(HERO_URL).mock(return_value=httpx.Response(200, text=walled))

    res = fetch_url(HERO_URL)
    assert res.blocked is True
    assert res.text == "", "a blocked fetch must not hand the wall page on as content"


@respx.mock
def test_planted_abuse_wall_redirect_is_blocked(tmp_path: Path):
    """The live shape observed 2026-09-11: a non-browser UA redirects to the apology URL and 404s there."""
    apology = f"https://www.fda.gov/apology_objects/{intake.ABUSE_WALL_MARKER}.html"
    respx.get(HERO_URL).mock(return_value=httpx.Response(302, headers={"location": apology}))
    respx.get(apology).mock(return_value=httpx.Response(404, text="<html>sorry</html>"))

    res = fetch_url(HERO_URL, cache_dir=tmp_path)
    assert res.blocked is True
    assert intake.ABUSE_WALL_MARKER in res.final_url
    assert list(tmp_path.glob("*.html")) == [], "a wall page must never be cached as the recall"


def test_planted_product_type_drugs_is_not_food():
    """PROPERTY: is_food comes from the page's own Product Type, via rules.looks_like_food.

    Planted defect: the hero header's Product Type is changed from "Food & Beverages" to "Drugs" while
    every food-sounding word in the title and body is left intact. A parser that infers is_food from the
    text, or hardcodes True, still says food -- and fails here.
    """
    mutated = _press("hero-great-value-triple-berry").replace(
        '<dd class="cell-2_3">Food &amp; Beverages', '<dd class="cell-2_3">Drugs', 1
    )
    raw = parse_fda_press_page(mutated, url=HERO_URL)
    assert raw.product_type == "Drugs"
    assert raw.is_food is False
    assert to_recall_notice(raw, Source.FDA_RSS).is_food is False
    # the rest of the parse is untouched, so the mutation really is isolated to Product Type
    assert "Frutas y Hortalizas" in raw.firm
    assert "7874211226" in raw.upcs_as_printed


def test_planted_missing_lot_yields_no_lots_but_full_confidence():
    """PROPERTY: a lot is evidence, not a confidence field (rule 4 widens, it never drops).

    Planted defect: the lot string is deleted from the hero body, leaving the "Lot Code:" label behind.
    A lot extractor that grabs whatever follows the label would invent "(in front of the package)"; a
    confidence score that counted lots would drop below 1.0. Both fail here.
    """
    mutated = _press("hero-great-value-triple-berry").replace("6040 01-6", "")
    assert "6040 01-6" not in mutated

    raw = parse_fda_press_page(mutated, url=HERO_URL)
    assert raw.lots == [], f"no lot is printed any more, got {raw.lots!r}"

    notice = to_recall_notice(raw, Source.FDA_RSS)
    assert notice.products[0].lots == []
    assert notice.extraction_confidence == 1.0, "lot is not one of the four confidence fields"
    # the fields that DO carry confidence are still there
    assert notice.firm and notice.reason and notice.distribution_states
    assert "7874211226" in notice.products[0].upcs_as_printed


def test_planted_lot_label_without_a_code_does_not_invent_one():
    """Guard on the same property from the other side, in plain text."""
    raw = parse_text("Acme recall\nLot Code: see the package for details.\nShipped to FL.\n")
    assert raw.lots == []


# ===========================================================================
# Regression guards on the regex extractors
# ===========================================================================
def test_upc_label_is_not_read_as_a_lot():
    """Evergreen prints 'UPC code 8 38796 00105 1'; a bare 'code' trigger would file it as a lot."""
    raw = parse_text("Acme recall\nUPC code 8 38796 00105 1 on the bag.\nShipped to WA.\n")
    assert raw.upcs_as_printed == ["8 38796 00105 1"]
    assert raw.lots == []


def test_lot_extraction_stops_at_the_first_non_code_word():
    raw = parse_text("Acme recall\nConsumers with lot code 6040 01-6 should not consume it.\nSold in FL.\n")
    assert raw.lots == ["6040 01-6"]


def test_distribution_sentence_without_a_place_is_ignored():
    raw = parse_text(
        "Acme recall\n"
        "Products are distributed in 30 lb. and 5 lb. white plastic tubs.\n"
        "The salads were distributed in Brooklyn and New York City.\n"
    )
    assert raw.distribution_states == ["NY"]
    assert "plastic tubs" not in raw.distribution_text


def test_sentence_splitter_keeps_abbreviations_whole():
    raw = parse_text(
        "Acme recall\n"
        "Frutas y Hortalizas del Sur S.A. shipped the product to Florida. Consumers should discard it.\n"
    )
    assert "S.A. shipped" in raw.distribution_text
    assert raw.distribution_states == ["FL"]
    assert raw.disposition_text == "Consumers should discard it."


def test_raw_excerpt_is_capped_at_1500_chars():
    raw = RawNotice(firm="Acme", body_text="x" * 5000)
    assert len(to_recall_notice(raw, Source.DRILL).raw_excerpt) == 1500


def test_every_fixture_press_page_parses_without_raising():
    pages = sorted(PRESS.glob("*.html"))
    assert len(pages) >= 6, "at least the six press fixtures cached on 2026-09-11 (plus the pinned feed's pages)"
    for page in pages:
        raw = parse_fda_press_page(page.read_text(encoding="utf-8"), url=f"https://www.fda.gov/{page.stem}")
        assert raw.title and raw.firm and raw.body_text
        notice = to_recall_notice(raw, Source.FDA_RSS)
        assert 0.0 <= notice.extraction_confidence <= 1.0
        assert re.match(r"^\S", notice.firm)
