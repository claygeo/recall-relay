"""The demo ledger: South Dade Community Food Bank (fictional), Miami-Dade FL.

Deterministic by construction. The agency and receipt tables are literal data (a real receiving ledger is
not random), and the distribution log is generated from a fixed-seed delivery calendar so every run of
`write_csvs` produces byte-identical CSVs and `load_seed` produces an identical database.

What the ledger is built to show:
  * It is messy on purpose. Retail-rescue rows carry no UPC and no lot, and the brand often lives inside
    the product text instead of the brand column. Lots appear on ~32% of rows (purchases and TEFAP).
  * Receipt 17 is the hero row: Great Value Organic Triple Berry Blend 10 oz, 30 cases, received
    2026-08-24 as Walmart retail rescue with no lot. It is the ledger side of the real FDA recall of
    2026-09-02/03 (Frutas y Hortalizas del Sur S.A. expanded recall, E. coli O145, openFDA H-1181-2026),
    which shipped to Walmart stores in 27 states including Florida. Because the row has no lot, rule 4
    widens the match to all 30 cases; 22 were already shipped, so 8 are still on hand.
  * Three decoys keep the matcher honest: a same-brand/different-product frozen berry row (48), a
    same-brand/different-product Ghirardelli row (2) aimed at the unrelated H-0844-2026 recall, and an
    unbranded "Organic Whole Blueberries 10 oz frozen" row (34) that is genuinely ambiguous against the
    same notice and is the NEEDS_HUMAN beat.
  * One salvage/reclamation repack row (39) has no lot lineage at all: rule 7, physical sort required.

Nothing here names a real food bank or a real partner agency. The retail donors and the recalled products
are real because the recall is real; the ledger that received them is not.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import Any, Optional

from .models import Agency, Distribution, Receipt, ReceiptChannel

# --------------------------------------------------------------------------------------------------
# Pinned identifiers the rest of the app (and the tests) rely on
# --------------------------------------------------------------------------------------------------
SEED = 20260914

HERO_RECEIPT_ID = 17                 # Great Value Organic Triple Berry Blend 10 oz, no lot
DECOY_SAME_BRAND_RECEIPT_ID = 48     # Great Value Mixed Berries 16 oz: same brand, different line
DECOY_GHIRARDELLI_RECEIPT_ID = 2     # Ghirardelli chips: same brand as H-0844-2026, different product
DECOY_AMBIGUOUS_RECEIPT_ID = 34      # unbranded Organic Whole Blueberries 10 oz: genuinely ambiguous
SALVAGE_RECEIPT_ID = 39              # reclamation repack: no lot lineage (rule 7)
FOOD_DRIVE_RECEIPT_ID = 11

N_AGENCIES = 12
N_RECEIPTS = 60
N_DISTRIBUTIONS = 140

LEDGER_START = date(2026, 6, 1)
LEDGER_END = date(2026, 9, 10)
DIST_START = date(2026, 6, 3)
DIST_END = date(2026, 9, 10)

# --------------------------------------------------------------------------------------------------
# Donors
# --------------------------------------------------------------------------------------------------
WM = "Walmart Supercenter #1234 retail rescue"
WMN = "Walmart Neighborhood Market #5589 retail rescue"
PX455 = "Publix #0455 retail rescue"
PX1130 = "Publix #1130 retail rescue"
TGT = "Target T-2261 retail rescue"
ALDI = "Aldi #78 retail rescue"
COOP = "Statewide Food Co-op purchase order"
TEFAP = "USDA Foods TEFAP delivery"
DRIVE = "Cutler Bay High School student food drive"
SALV = "Salvage grocers reclamation lot"

RR = ReceiptChannel.RETAIL_RESCUE
PU = ReceiptChannel.PURCHASE
TF = ReceiptChannel.TEFAP
FD = ReceiptChannel.FOOD_DRIVE
SV = ReceiptChannel.SALVAGE


# --------------------------------------------------------------------------------------------------
# 12 partner agencies
# --------------------------------------------------------------------------------------------------
# id, name, kind, open_schedule, same_day, languages, contact, email-local, phone-suffix, city
_AGENCY_ROWS: list[tuple[str, str, str, str, bool, list[str], str, str, str]] = [
    ("nueva-vida", "Iglesia Nueva Vida Food Pantry", "church pantry",
     "Wednesdays 9-1", False, ["es", "en"], "Marisol Delgado", "01", "Homestead"),
    ("redland-kitchen", "Redland Community Kitchen", "soup kitchen",
     "Mon-Fri 11-2, hot meals served on site", True, ["en", "es"], "Darnell Foster", "02", "Homestead"),
    ("cutler-bay-baptist", "Cutler Bay Baptist Food Ministry", "church pantry",
     "first Saturday monthly, 8-11", False, ["en"], "Gwendolyn Pike", "03", "Cutler Bay"),
    ("naranja-esperanza", "Centro Esperanza Naranja", "church pantry",
     "Tue/Thu 9-12", False, ["es", "ht", "en"], "Ana Lucia Restrepo", "04", "Naranja"),
    ("florida-city-shelter", "Florida City Family Shelter", "shelter",
     "daily 7-7, residents only", True, ["en", "es", "ht"], "Yvette Pierre-Louis", "05", "Florida City"),
    ("goulds-mobile", "Goulds Mobile Pantry Route 3", "mobile pantry",
     "second and fourth Friday, 10-1, curbside", True, ["en", "es"], "Terrance Bell", "06", "Goulds"),
    ("perrine-senior", "Perrine Senior Center Pantry", "senior center",
     "Mon/Wed 10-12", False, ["en", "es"], "Beverly Simmons", "07", "Perrine"),
    ("leisure-city-school", "Leisure City Elementary School Pantry", "school pantry",
     "Thursdays 2-5 during the school year", False, ["es", "en"], "Dianne Okafor", "08", "Leisure City"),
    ("princeton-methodist", "Princeton United Methodist Food Closet", "church pantry",
     "first and third Saturday, 9-11", False, ["en"], "Roy Kittrell", "09", "Princeton"),
    ("palmetto-bay-community", "Palmetto Bay Community Pantry", "community pantry",
     "Tuesdays 4-7", False, ["en", "es"], "Priya Raghunathan", "10", "Palmetto Bay"),
    ("richmond-heights-ame", "Richmond Heights AME Food Pantry", "church pantry",
     "biweekly Saturday, 8-11", False, ["en", "ht"], "Carla Whitfield", "11", "Richmond Heights"),
    ("homestead-bethel", "Eglise Bethel Homestead Pantry", "church pantry",
     "Sundays after service, 12-3", False, ["ht", "en"], "Jean-Baptiste Moise", "12", "Homestead"),
]


def agencies() -> list[Agency]:
    out = []
    for slug, name, kind, sched, same_day, langs, contact, suffix, city in _AGENCY_ROWS:
        out.append(Agency(
            id=slug,
            name=name,
            kind=kind,
            open_schedule=sched,
            same_day_distribution=same_day,
            languages=list(langs),
            contact_name=contact,
            contact_email=f"{slug}@example.org",
            contact_phone=f"305-555-01{suffix}",
            city=city,
        ))
    return out


# --------------------------------------------------------------------------------------------------
# 60 receiving-ledger rows
# --------------------------------------------------------------------------------------------------
# id, received_at, donor, channel, brand, product, size, upc, lot, best_by, cases, storage, notes
_RECEIPT_ROWS: list[tuple[Any, ...]] = [
    (1, "2026-06-01", PX455, RR, "", "Publix Bakery day-old bread assortment", "", "", "", None, 14,
     "dry", ""),
    (2, "2026-06-02", COOP, PU, "Ghirardelli", "Semi-Sweet Chocolate Chips", "12 oz", "747599304309",
     "L6152 B", "2027-03-18", 5, "dry", "baking allocation for holiday boxes"),
    (3, "2026-06-03", TEFAP, TF, "USDA Foods", "Canned Pears in Juice", "15 oz", "", "TEF-2026-1183",
     "2028-01-31", 36, "dry", ""),
    (4, "2026-06-04", WM, RR, "Great Value", "Shredded Mozzarella", "8 oz", "", "", "2026-07-02", 9,
     "refrigerated", ""),
    (5, "2026-06-06", TGT, RR, "", "Good & Gather Baby Spinach 5 oz clamshell", "", "", "", None, 7,
     "refrigerated", "short shelf life, move first"),
    (6, "2026-06-08", ALDI, RR, "", "Millville Crispy Oats cereal 18 oz", "", "", "", None, 11, "dry", ""),
    (7, "2026-06-09", COOP, PU, "Goya", "Black Beans", "15.5 oz", "041331025508", "G6161", "2028-06-01",
     40, "dry", ""),
    (8, "2026-06-11", PX1130, RR, "Publix", "Rotisserie Chicken", "", "", "", None, 6, "refrigerated",
     "same-day move only"),
    (9, "2026-06-12", TEFAP, TF, "USDA Foods", "Frozen Chicken Leg Quarters", "40 lb", "", "TEF-2026-1204",
     "2027-02-28", 24, "frozen", ""),
    (10, "2026-06-15", WM, RR, "", "Great Value Long Grain White Rice 5 lb", "", "", "", None, 18, "dry", ""),
    (11, "2026-06-16", DRIVE, FD, "", "Assorted shelf-stable donations, student drive", "", "", "", None,
     22, "dry", "unsorted, mixed brands, no invoice"),
    (12, "2026-06-18", COOP, PU, "Barilla", "Penne Rigate", "16 oz", "076808280159", "B6155", "2028-02-14",
     32, "dry", ""),
    (13, "2026-06-20", PX455, RR, "Publix", "2% Reduced Fat Milk", "1 gal", "", "", "2026-06-27", 12,
     "refrigerated", ""),
    (14, "2026-06-22", TGT, RR, "", "Good & Gather Frozen Broccoli Florets 12 oz", "", "", "", None, 10,
     "frozen", ""),
    (15, "2026-06-24", TEFAP, TF, "USDA Foods", "Peanut Butter, Smooth", "40 oz", "", "TEF-2026-1231",
     "2027-11-30", 30, "dry", ""),
    (16, "2026-06-26", ALDI, RR, "", "Season's Choice Frozen Mixed Vegetables 16 oz", "", "", "", None, 15,
     "frozen", ""),
    # ---- hero row: the ledger side of openFDA H-1181-2026 -----------------------------------------
    (17, "2026-08-24", WM, RR, "Great Value", "Organic Triple Berry Blend", "10 oz", "", "", None, 30,
     "frozen", "pallet split across three agency deliveries"),
    (18, "2026-06-29", COOP, PU, "Del Monte", "Sliced Peaches in 100% Juice", "15 oz", "024000163008",
     "DM6178", "2028-04-30", 28, "dry", ""),
    (19, "2026-07-01", WMN, RR, "", "Sara Lee Butter Bread 20 oz", "", "", "", "2026-07-08", 16, "dry", ""),
    (20, "2026-07-02", PX1130, RR, "Publix", "Deli Sliced Turkey Breast", "1 lb", "", "", "2026-07-09", 5,
     "refrigerated", ""),
    (21, "2026-07-04", TEFAP, TF, "USDA Foods", "Canned Green Beans", "14.5 oz", "", "TEF-2026-1248",
     "2028-08-31", 34, "dry", ""),
    (22, "2026-07-06", TGT, RR, "", "Good & Gather Whole Wheat Tortillas 12 ct", "", "", "", "2026-07-30",
     8, "dry", ""),
    (23, "2026-07-07", COOP, PU, "Bush's", "Baked Beans, Original", "28 oz", "039400016205", "BB6188",
     "2028-09-30", 26, "dry", ""),
    (24, "2026-07-09", WM, RR, "", "Birds Eye Frozen Sweet Corn 12 oz", "", "", "", None, 13, "frozen", ""),
    (25, "2026-07-11", ALDI, RR, "", "Friendly Farms Greek Yogurt 32 oz", "", "", "", "2026-07-25", 9,
     "refrigerated", ""),
    (26, "2026-07-13", PX455, RR, "Publix", "Bakery Cuban Bread", "", "", "", None, 11, "dry", ""),
    (27, "2026-07-14", COOP, PU, "Kirkland", "Canned Chicken Breast", "12.5 oz", "096619028771", "K6195",
     "2029-01-31", 20, "dry", ""),
    (28, "2026-07-16", TEFAP, TF, "USDA Foods", "Dried Pinto Beans", "2 lb", "", "TEF-2026-1259",
     "2028-05-31", 38, "dry", ""),
    (29, "2026-07-18", WMN, RR, "Great Value", "Creamy Peanut Butter", "40 oz", "", "", "2027-06-30", 14,
     "dry", ""),
    (30, "2026-07-20", TGT, RR, "", "Good & Gather Organic Apple Juice 64 oz", "", "", "", "2027-01-15", 10,
     "dry", ""),
    (31, "2026-07-21", COOP, PU, "General Mills", "Cheerios", "18 oz", "016000275287", "GM6202",
     "2027-12-31", 24, "dry", ""),
    (32, "2026-07-23", PX1130, RR, "", "Publix Frozen Strawberries 16 oz", "", "", "", None, 12,
     "frozen", ""),
    (33, "2026-07-25", ALDI, RR, "", "Specially Selected Sourdough Loaf", "", "", "", "2026-07-31", 7,
     "dry", ""),
    # ---- ambiguous decoy: same recalled product name, no brand column, different grower --------------
    (34, "2026-07-28", PX455, RR, "", "Organic Whole Blueberries 10 oz frozen", "", "", "", None, 6,
     "frozen", "brand not captured at the dock"),
    (35, "2026-07-29", TEFAP, TF, "USDA Foods", "Canned Salmon", "14.75 oz", "", "TEF-2026-1272",
     "2029-03-31", 18, "dry", ""),
    (36, "2026-07-31", WM, RR, "", "Kraft Shredded Sharp Cheddar 8 oz", "", "", "", "2026-08-21", 10,
     "refrigerated", ""),
    (37, "2026-08-01", COOP, PU, "Goya", "Long Grain Rice", "5 lb", "041331020602", "G6214", "2028-07-31",
     35, "dry", ""),
    (38, "2026-08-03", TGT, RR, "", "Good & Gather Chicken Breast Tenderloins 1.5 lb", "", "", "",
     "2026-08-10", 8, "refrigerated", ""),
    # ---- salvage / reclamation repack: no lot lineage at all (rule 7) --------------------------------
    (39, "2026-08-04", SALV, SV, "", "assorted frozen fruit repack", "", "", "", None, 16, "frozen",
     "reclamation pallet, mixed origin, no lot lineage"),
    (40, "2026-08-06", PX455, RR, "Publix", "Whole Seedless Watermelon", "", "", "", None, 9,
     "refrigerated", ""),
    (41, "2026-08-07", TEFAP, TF, "USDA Foods", "Frozen Ground Beef, 80/20", "10 lb", "", "TEF-2026-1288",
     "2027-04-30", 22, "frozen", ""),
    (42, "2026-08-09", ALDI, RR, "", "Fit & Active Frozen Blueberries 12 oz", "", "", "", None, 9,
     "frozen", ""),
    (43, "2026-08-10", COOP, PU, "Del Monte", "Whole Kernel Corn", "15.25 oz", "024000027089", "DM6222",
     "2028-10-31", 30, "dry", ""),
    (44, "2026-08-12", WMN, RR, "", "Crisco Vegetable Oil 48 oz", "", "", "", "2027-09-30", 12, "dry", ""),
    (45, "2026-08-13", PX1130, RR, "Publix", "Large Grade A Eggs", "18 ct", "", "", "2026-08-30", 15,
     "refrigerated", ""),
    (46, "2026-08-15", TGT, RR, "", "Good & Gather Frozen Mango Chunks 16 oz", "", "", "", None, 8,
     "frozen", ""),
    (47, "2026-08-17", COOP, PU, "Driscoll's", "Fresh Blueberries", "18 oz", "784855100341", "DR6231",
     "2026-08-28", 6, "refrigerated", ""),
    # ---- same-brand decoy: Great Value, frozen berries, different line and size ----------------------
    (48, "2026-08-20", WM, RR, "Great Value", "Mixed Berries", "16 oz", "", "", None, 12, "frozen", ""),
    (49, "2026-08-21", TGT, RR, "", "Good & Gather Unsweetened Applesauce Pouches 12 ct", "", "", "",
     "2027-06-30", 28, "dry", ""),
    (50, "2026-08-25", ALDI, RR, "", "Baker's Corner All Purpose Flour 5 lb", "", "", "", "2027-05-31", 14,
     "dry", ""),
    (51, "2026-08-26", PX455, RR, "", "Publix Deli Rotisserie Chicken Soup 24 oz", "", "", "", "2026-09-02",
     6, "refrigerated", ""),
    (52, "2026-08-27", COOP, PU, "Kroger", "Elbow Macaroni", "16 oz", "011110866233", "KR6239",
     "2028-01-31", 26, "dry", ""),
    (53, "2026-08-29", WM, RR, "", "StarKist Chunk Light Tuna in Water 5 oz", "", "", "", "2029-02-28", 20,
     "dry", ""),
    (54, "2026-08-31", TGT, RR, "", "Good & Gather Sliced Cheddar 8 oz", "", "", "", "2026-09-20", 9,
     "refrigerated", ""),
    (55, "2026-09-02", PX1130, RR, "Publix", "Bagged Garden Salad", "12 oz", "", "", "2026-09-08", 7,
     "refrigerated", ""),
    (56, "2026-09-03", ALDI, RR, "", "Goldhen Large Eggs 18 ct", "", "", "", "2026-09-24", 12,
     "refrigerated", ""),
    (57, "2026-09-05", COOP, PU, "Goya", "Pink Beans", "15.5 oz", "041331025614", "G6248", "2028-11-30",
     30, "dry", ""),
    (58, "2026-09-07", ALDI, RR, "", "Season's Choice Frozen Peas 16 oz", "", "", "", None, 11, "frozen", ""),
    (59, "2026-09-09", WMN, RR, "", "Great Value Frozen Strawberries 16 oz", "", "", "", None, 10,
     "frozen", ""),
    (60, "2026-09-10", PX455, RR, "", "Publix Bakery day-old pastry assortment", "", "", "", None, 8,
     "dry", ""),
]


def receipts() -> list[Receipt]:
    out = []
    for (rid, received_at, donor, channel, brand, product, size, upc, lot, best_by, cases, storage,
         notes) in _RECEIPT_ROWS:
        out.append(Receipt(
            id=rid,
            received_at=date.fromisoformat(received_at),
            donor=donor,
            channel=channel,
            brand=brand,
            product=product,
            size=size,
            upc=upc,
            lot=lot,
            best_by=date.fromisoformat(best_by) if best_by else None,
            cases=cases,
            storage=storage,
            notes=notes,
        ))
    return sorted(out, key=lambda r: r.id)


# --------------------------------------------------------------------------------------------------
# 140 distribution-log rows
# --------------------------------------------------------------------------------------------------
# The three hero rows are pinned: 10 + 6 + 6 = 22 of receipt 17's 30 cases, leaving 8 on hand. The
# 2026-09-01 delivery goes to a same-day-distribution agency on purpose, because that is the pantry the
# coordinator has to call rather than email.
_PINNED_DISTRIBUTIONS: list[tuple[int, str, str, int]] = [
    (HERO_RECEIPT_ID, "nueva-vida", "2026-08-26", 10),
    (HERO_RECEIPT_ID, "florida-city-shelter", "2026-09-01", 6),   # same_day_distribution=True
    (HERO_RECEIPT_ID, "perrine-senior", "2026-09-04", 6),
    # decoys carry traffic too, so the pull list has to discriminate rather than pattern-match
    (DECOY_SAME_BRAND_RECEIPT_ID, "goulds-mobile", "2026-08-24", 4),
    (DECOY_SAME_BRAND_RECEIPT_ID, "richmond-heights-ame", "2026-09-02", 3),
    (DECOY_AMBIGUOUS_RECEIPT_ID, "nueva-vida", "2026-08-01", 2),
    (DECOY_AMBIGUOUS_RECEIPT_ID, "leisure-city-school", "2026-08-15", 2),
    (DECOY_GHIRARDELLI_RECEIPT_ID, "princeton-methodist", "2026-06-20", 2),
    (SALVAGE_RECEIPT_ID, "redland-kitchen", "2026-08-14", 5),
]

# food bank -> partner delivery cadence in days (this is the truck schedule, not the pantry's open hours)
_DELIVERY_CADENCE: dict[str, int] = {
    "nueva-vida": 14,
    "redland-kitchen": 14,
    "cutler-bay-baptist": 28,
    "naranja-esperanza": 21,
    "florida-city-shelter": 14,
    "goulds-mobile": 21,
    "perrine-senior": 28,
    "leisure-city-school": 28,
    "princeton-methodist": 21,
    "palmetto-bay-community": 21,
    "richmond-heights-ame": 28,
    "homestead-bethel": 21,
}


def _delivery_calendar() -> list[tuple[date, str]]:
    """Every (date, agency) truck stop in the demo window, in date order."""
    events: list[tuple[date, str]] = []
    for offset, (slug, cadence) in enumerate(_DELIVERY_CADENCE.items()):
        day = DIST_START + timedelta(days=offset)
        while day <= DIST_END:
            events.append((day, slug))
            day += timedelta(days=cadence)
    return sorted(events)


def distributions() -> list[Distribution]:
    """Generate the distribution log. Invariants, asserted below:

    * exactly N_DISTRIBUTIONS rows,
    * no receipt ever ships more cases than it received,
    * nothing ships before it was received,
    * receipt 17 ships exactly the three pinned rows (22 of 30 cases, 8 on hand).
    """
    rng = Random(SEED)
    recs = receipts()
    by_id = {r.id: r for r in recs}
    remaining = {r.id: r.cases for r in recs}
    rows: list[tuple[int, str, date, int]] = []
    seen: set[tuple[int, str, date]] = set()

    def ship(receipt_id: int, agency_id: str, day: date, cases: int) -> bool:
        if cases <= 0 or cases > remaining[receipt_id]:
            return False
        if day < by_id[receipt_id].received_at:
            return False
        key = (receipt_id, agency_id, day)
        if key in seen:
            return False
        seen.add(key)
        remaining[receipt_id] -= cases
        rows.append((receipt_id, agency_id, day, cases))
        return True

    for receipt_id, agency_id, day, cases in _PINNED_DISTRIBUTIONS:
        if not ship(receipt_id, agency_id, date.fromisoformat(day), cases):
            raise AssertionError(f"pinned distribution failed: receipt {receipt_id} -> {agency_id} {day}")

    # receipt 17's on-hand count is part of the demo's arithmetic; the generator must not touch it
    locked = {HERO_RECEIPT_ID}

    events = _delivery_calendar()
    target = N_DISTRIBUTIONS - len(rows)
    counts = [rng.randint(1, 4) for _ in events]
    while sum(counts) > target:
        i = rng.randrange(len(counts))
        if counts[i] > 1:
            counts[i] -= 1
    while sum(counts) < target:
        i = rng.randrange(len(counts))
        if counts[i] < 5:
            counts[i] += 1

    def emit(day: date, agency_id: str, wanted: int) -> int:
        pool = [r.id for r in recs
                if r.received_at <= day and remaining[r.id] >= 1 and r.id not in locked
                and (r.id, agency_id, day) not in seen]
        rng.shuffle(pool)
        made = 0
        for receipt_id in pool:
            if made >= wanted:
                break
            cases = rng.randint(1, min(6, remaining[receipt_id]))
            if ship(receipt_id, agency_id, day, cases):
                made += 1
        return made

    for (day, agency_id), wanted in zip(events, counts):
        emit(day, agency_id, wanted)

    # top-up pass: earlier truck stops can run short when few receipts exist yet
    guard = 0
    while len(rows) < N_DISTRIBUTIONS and guard < 5000:
        guard += 1
        progress = 0
        for day, agency_id in reversed(events):
            if len(rows) >= N_DISTRIBUTIONS:
                break
            progress += emit(day, agency_id, 1)
        if progress == 0:
            break

    if len(rows) != N_DISTRIBUTIONS:
        raise AssertionError(f"generated {len(rows)} distributions, expected {N_DISTRIBUTIONS}")

    rows.sort(key=lambda t: (t[2], t[1], t[0]))
    out = [Distribution(id=i, receipt_id=r, agency_id=a, shipped_at=d, cases=c)
           for i, (r, a, d, c) in enumerate(rows, start=1)]

    shipped: dict[int, int] = {}
    for d_ in out:
        shipped[d_.receipt_id] = shipped.get(d_.receipt_id, 0) + d_.cases
    for receipt_id, total in shipped.items():
        if total > by_id[receipt_id].cases:
            raise AssertionError(f"receipt {receipt_id} over-shipped: {total} > {by_id[receipt_id].cases}")
    if shipped.get(HERO_RECEIPT_ID) != 22:
        raise AssertionError(f"hero receipt shipped {shipped.get(HERO_RECEIPT_ID)} cases, expected 22")
    return out


# --------------------------------------------------------------------------------------------------
# CSV <-> Store
# --------------------------------------------------------------------------------------------------
AGENCY_COLUMNS = ["id", "name", "kind", "open_schedule", "same_day_distribution", "languages",
                  "contact_name", "contact_email", "contact_phone", "city"]
RECEIPT_COLUMNS = ["id", "received_at", "donor", "channel", "brand", "product", "size", "upc", "lot",
                   "best_by", "cases", "storage", "notes"]
DISTRIBUTION_COLUMNS = ["id", "receipt_id", "agency_id", "shipped_at", "cases"]

CSV_FILES = {"agencies": "agencies.csv", "receipts": "receipts.csv", "distributions": "distributions.csv"}


def _b(value: bool) -> str:
    return "true" if value else "false"


def _d(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def _agency_row(a: Agency) -> dict[str, Any]:
    return {"id": a.id, "name": a.name, "kind": a.kind, "open_schedule": a.open_schedule,
            "same_day_distribution": _b(a.same_day_distribution), "languages": "|".join(a.languages),
            "contact_name": a.contact_name, "contact_email": a.contact_email,
            "contact_phone": a.contact_phone, "city": a.city}


def _receipt_row(r: Receipt) -> dict[str, Any]:
    return {"id": r.id, "received_at": r.received_at.isoformat(), "donor": r.donor,
            "channel": r.channel.value, "brand": r.brand, "product": r.product, "size": r.size,
            "upc": r.upc, "lot": r.lot, "best_by": _d(r.best_by), "cases": r.cases,
            "storage": r.storage, "notes": r.notes}


def _distribution_row(d: Distribution) -> dict[str, Any]:
    return {"id": d.id, "receipt_id": d.receipt_id, "agency_id": d.agency_id,
            "shipped_at": d.shipped_at.isoformat(), "cases": d.cases}


def _write(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def write_csvs(directory: Path | str) -> dict[str, int]:
    """Write agencies.csv / receipts.csv / distributions.csv. Byte-identical on every run."""
    directory = Path(directory)
    return {
        "agencies": _write(directory / CSV_FILES["agencies"], AGENCY_COLUMNS,
                           [_agency_row(a) for a in agencies()]),
        "receipts": _write(directory / CSV_FILES["receipts"], RECEIPT_COLUMNS,
                           [_receipt_row(r) for r in receipts()]),
        "distributions": _write(directory / CSV_FILES["distributions"], DISTRIBUTION_COLUMNS,
                                [_distribution_row(d) for d in distributions()]),
    }


def load_seed(store: Any) -> dict[str, int]:
    """Load the demo ledger straight into a Store, without going through CSV. Idempotent (upserts)."""
    ags, recs, dists = agencies(), receipts(), distributions()
    for a in ags:
        store.upsert_agency(a)
    for r in recs:
        store.upsert_receipt(r)
    for d in dists:
        store.add_distribution(d)
    return {"agencies": len(ags), "receipts": len(recs), "distributions": len(dists)}
