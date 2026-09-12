"""Intake: turn a recall from any source into a RawNotice, deterministically, with no LLM.

The LLM step (agents/extractor.py) only runs when the deterministic parse leaves gaps, and it fills the
typed RecallNotice from RawNotice.body_text. Nothing in this module calls a model.

Public API (implemented in this file; keep signatures stable, other modules code against them):

    fetch_url(url, *, cache_dir=None, timeout=25) -> FetchResult
        GET with a real browser User-Agent. Detects the fda.gov abuse wall (302/200 to
        /apology_objects/abuse-detection-apology.html) and returns blocked=True instead of raising.
        Caches successful HTML bodies under cache_dir/<sha1(url)>.html when cache_dir is given; serves
        from cache first so the demo cannot be sunk by a fetch failure.

    parse_fda_press_page(html, url) -> RawNotice
        Extracts the FDA press-page header block (Company Announcement Date, FDA Publish Date, Product Type,
        Reason for Announcement, Company Name, Brand Name(s), Product Description) and the Company
        Announcement body, then regex-extracts: UPCs as printed (with spaces/dashes preserved), lot/code
        strings, best-by / use-by strings, US state codes and full state names from the distribution
        sentence(s), the disposition sentence(s) ("consumers who have purchased ... are urged to ...",
        "return ... for a full refund", "discard", "do not consume"), and is_food from Product Type.

    parse_text(text, source_url="") -> RawNotice
        Same regex extraction over pasted or forwarded plain text (a Feeding America alert, a firm letter,
        an FSIS notice). Sets channel=FSIS when the text names FSIS/USDA meat/poultry/egg, USDA_FOODS when it
        names TEFAP/USDA Foods hold-and-recall, else FDA.

    pdf_to_text(path_or_bytes) -> str
        pypdf text layer only (no OCR). Empty string when there is no text layer.

    poll_fda_rss(*, cache_dir=None) -> list[RssItem]
        Parses https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml with feedparser.
        Each item: title, link, published (datetime, UTC). No filtering here; is_food is decided from the
        press page's Product Type (rules.looks_like_food) after fetching.

    openfda_lookup(*, recall_number="", firm="", product_terms="", since_days=120, limit=10) -> list[OpenFDAEnrichment]
        Queries https://api.fda.gov/food/enforcement.json (keyless). Returns [] on 404 (openFDA returns 404
        for zero results, which is expected for anything younger than ~11 days). Never raises on HTTP errors.

    to_recall_notice(raw, source, seen_at) -> RecallNotice
        Deterministic best-effort mapping RawNotice -> RecallNotice. extraction_confidence < 0.8 when any of
        firm, product name, reason, or distribution is missing; the caller then asks the extractor agent.

Fixtures: data/fixtures/press/<slug>.html (cached hero + decoy pages), data/fixtures/rss/recalls.xml,
data/fixtures/openfda/<recall_number>.json. Tests must run offline against these fixtures.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from .config import settings
from .models import Channel, Classification, OpenFDAEnrichment, ProductLine, RecallNotice, Source
from .rules import looks_like_food

ABUSE_WALL_MARKER = "abuse-detection-apology"
FDA_RSS_URL = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/recalls/rss.xml"
OPENFDA_URL = "https://api.fda.gov/food/enforcement.json"


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    blocked: bool = False
    from_cache: bool = False
    final_url: str = ""


@dataclass
class RssItem:
    title: str
    link: str
    published: datetime


@dataclass
class RawNotice:
    source_url: str = ""
    title: str = ""
    company_announcement_date: Optional[date] = None
    fda_publish_date: Optional[date] = None
    product_type: str = ""
    reason: str = ""
    firm: str = ""
    brand_names: list[str] = field(default_factory=list)
    product_description: str = ""
    body_text: str = ""
    upcs_as_printed: list[str] = field(default_factory=list)
    lots: list[str] = field(default_factory=list)
    best_by: list[str] = field(default_factory=list)
    distribution_states: list[str] = field(default_factory=list)
    distribution_text: str = ""
    disposition_text: str = ""
    sizes: list[str] = field(default_factory=list)
    channel: Channel = Channel.FDA
    is_food: bool = True


# ---------------------------------------------------------------------------
# States. Full names and territories map to the two-letter codes the ledger uses.
# ---------------------------------------------------------------------------
STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    # territories and the district; these appear verbatim in FDA distribution lists
    "district of columbia": "DC", "washington, d.c.": "DC", "washington d.c.": "DC",
    "washington, dc": "DC", "puerto rico": "PR", "guam": "GU", "american samoa": "AS",
    "virgin islands": "VI", "u.s. virgin islands": "VI", "northern mariana islands": "MP",
}
STATE_CODES: set[str] = set(STATE_NAMES.values())

# Longest first so "West Virginia" wins over "Virginia" and "Washington, D.C." (DC) over "Washington" (WA).
# The boundaries are lookarounds rather than \b because several names end in '.', after which \b never
# matches a following space -- which silently downgraded "Washington, D.C." to Washington state.
_STATE_NAME_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(n) for n in sorted(STATE_NAMES, key=len, reverse=True)) + r")(?!\w)",
    re.I,
)
_STATE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2})(?![A-Za-z0-9])")

# Two-letter tokens that are state codes but, standing alone in prose, are not meant as one.
_CODE_STOPLIST: set[str] = set()

_NATIONWIDE_RE = re.compile(
    r"nationwide|all 50 states|all fifty states|throughout the (?:united states|u\.?s\.?a?\.?)|"
    r"across the (?:united states|u\.?s\.?a?\.?)|in all states",
    re.I,
)

# ---------------------------------------------------------------------------
# Field-level regexes
# ---------------------------------------------------------------------------
_UPC_LABEL_RE = re.compile(
    r"\bU\.?P\.?C\.?s?\b[ \t]*(?:codes?|numbers?|nos?\.?|#)?[ \t]*[:\-–]?[ \t\r\n]*"
    r"([0-9][0-9 \-]{8,24}[0-9])",
    re.I,
)
_UPC_MORE_RE = re.compile(r"[ \t]*(?:,|;|/|and|&)[ \t]*([0-9][0-9 \-]{8,24}[0-9])", re.I)

_LOT_LABEL_RE = re.compile(
    r"\b(?:lot|batch)[ \t]*(?:codes?|numbers?|nos?\.?|#|s)?\b[ \t]*[:\-–]?[ \t\r\n]*",
    re.I,
)
_LOT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/.]*$")

_BEST_BY_LABEL_RE = re.compile(
    r"\b(?:best[ \t\-]*(?:if[ \t]*)?used?[ \t\-]*by|best[ \t\-]*by|used?[ \t\-]*by|sell[ \t\-]*by|"
    r"enjoy[ \t\-]*by|expiration|expiry)\b[ \t]*(?:dates?)?[ \t]*(?:of|are|is)?[ \t]*[:\-–]?",
    re.I,
)
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec")
_DATE_NUMERIC_RE = re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b")
_DATE_TEXT_RE = re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s*\d{{4}}\b", re.I)

_SIZE_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ounces?|lbs?|pounds?|grams?|kg|kilograms?|ml|liters?|"
    r"ct|count|packs?|pk)\b",
    re.I,
)
_SIZE_LABEL_RE = re.compile(
    r"\b(?:package\s*size|pack\s*size|net\s*wt\.?|net\s*weight|size)\b[ \t]*[:\-–]?[ \t]*"
    r"(\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|ounces?|lbs?|pounds?|grams?|kg|ml|liters?|ct|count|packs?|pk))",
    re.I,
)

_DISTRIBUTION_CUE_RE = re.compile(
    r"\b(?:shipped|ship|distributed|distribut\w*|sold|sell|sale|delivered|deliver\w*|available|"
    r"retail\w*|export\w*|wholesale|nationwide|market\w*)\b",
    re.I,
)
_DISPOSITION_CUE_RE = re.compile(
    r"(?:urged to|advised to|asked to|should not (?:consume|eat|drink|use|serve)|"
    r"do not (?:consume|eat|drink|use|serve)|not to (?:consume|eat|drink|use|serve)|"
    r"discard\w*|throw (?:it |them |the product )?away|dispose of|destroy(?:ed)?|"
    r"return(?:ed|ing)? (?:it|them|the product|any|all|to)|place of purchase|full refund|"
    r"stop using|quarantine)",
    re.I,
)
_FSIS_RE = re.compile(
    r"\bFSIS\b|USDA'?s? Food Safety and Inspection Service|Food Safety and Inspection Service|"
    r"\b(?:meat|poultry|beef|pork|chicken|turkey|egg products?|ground beef)\b[^.]{0,80}\brecall",
    re.I,
)
_USDA_FOODS_RE = re.compile(r"\bTEFAP\b|USDA Foods|hold[ \-]and[ \-]recall|Commodity Supplemental Food", re.I)

_MONTH_NUM = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# FDA press-page header labels, in page order. The value sits on the following line(s); FDA repeats the
# label as a bare line for some fields (Brand Name: / "Brand Name(s)" / "Great Value").
_HEADER_LABELS = [
    ("company_announcement_date", "Company Announcement Date"),
    ("fda_publish_date", "FDA Publish Date"),
    ("product_type", "Product Type"),
    ("reason", "Reason for Announcement"),
    ("firm", "Company Name"),
    ("brand", "Brand Name"),
    ("product_description", "Product Description"),
]
_REPEAT_MARKERS = {
    "recallreasondescription", "brandnames", "brandname", "productdescription", "producttype",
    "companyname", "reasonforannouncement", "companyannouncementdate", "fdapublishdate",
}
_BODY_START_MARKERS = {"company announcement"}
_BODY_END_MARKERS = {
    "company contact information", "product photos", "content current as of", "regulated product(s)",
    "follow fda", "media contact information",
}
_EMPTY_BRANDS = {"no brand name", "no brand", "none", "n/a", "na", "not applicable", "unbranded"}

_TRAILING_ABBREV_RE = re.compile(
    r"(?:\b(?:[A-Z]|Inc|Ltd|Co|Corp|Cos|Dr|Mr|Mrs|Ms|St|Ave|No|Nos|vs|approx|Jr|Sr|Dept|Univ|Est)"
    r"|\b(?:[A-Z]\.){1,4}[A-Z])\.\s*$"
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def _strip_tags(html: str) -> str:
    """HTML -> newline-separated visible text, block elements preserved as line breaks."""
    s = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(p|div|li|tr|h[1-6]|td|th|section|article|table|thead|tbody|ul|ol)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_lib.unescape(s)
    s = s.replace("\xa0", " ").replace("​", "")
    s = re.sub(r"[ \t]+", " ", s)
    return "\n".join(line.strip() for line in s.split("\n"))


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _sentences(text: str) -> list[str]:
    """Split into sentences without breaking on S.A. / U.S. / Inc. style abbreviations.

    A line break is a hard boundary: FDA press pages set each spec line ('Lot Code : 6040 01-6') on its own
    block, and running them together would let one unterminated line swallow the disposition sentence.
    """
    out: list[str] = []
    for line in str(text).split("\n"):
        flat = re.sub(r"[ \t]+", " ", line).strip()
        if not flat:
            continue
        parts = re.split(r'(?<=[.!?])\s+(?=[A-Z“"\[(])', flat)
        for i, part in enumerate(parts):
            # merge only WITHIN a line: 'S.A. expands' is one sentence, two lines never are
            if i and out and _TRAILING_ABBREV_RE.search(out[-1]):
                out[-1] = out[-1] + " " + part
            else:
                out.append(part)
    return [p.strip() for p in out if p.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        norm = re.sub(r"\s+", " ", it).strip()
        key = norm.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(norm)
    return out


def _parse_date(text: str) -> Optional[date]:
    """'September 02, 2026', '09/03/2026', '2026-09-03', '20260722' -> date."""
    if not text:
        return None
    t = str(text).strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", t, re.I)
    if m:
        mm = _MONTH_NUM.get(m.group(1).lower())
        if mm:
            try:
                return date(int(m.group(3)), mm, int(m.group(2)))
            except ValueError:
                return None
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b", t)
    if m:
        yy = int(m.group(3))
        yy = yy + 2000 if yy < 100 else yy
        try:
            return date(yy, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Field extractors (shared by parse_fda_press_page and parse_text)
# ---------------------------------------------------------------------------
def extract_states(text: str) -> list[str]:
    """Two-letter codes for every state/territory named in `text`, in order of appearance.

    Full names win over bare codes (so 'West Virginia' never yields VA). 'nationwide' is deliberately NOT
    expanded to 50 codes: rules.distribution_gate reads distribution_text for that.
    """
    if not text:
        return []
    hits: list[tuple[int, str]] = []
    consumed: list[tuple[int, int]] = []
    for m in _STATE_NAME_RE.finditer(text):
        code = STATE_NAMES[re.sub(r"\s+", " ", m.group(1).lower())]
        consumed.append((m.start(), m.end()))
        hits.append((m.start(), code))
    for m in _STATE_CODE_RE.finditer(text):
        code = m.group(1)
        if code not in STATE_CODES or code in _CODE_STOPLIST:
            continue
        if any(s <= m.start() < e for s, e in consumed):
            continue
        hits.append((m.start(), code))
    hits.sort(key=lambda h: h[0])
    out: list[str] = []
    for _, code in hits:
        if code not in out:
            out.append(code)
    return out


def extract_upcs(text: str) -> list[str]:
    """UPCs exactly as printed. Spacing and dashes are preserved: a UPC is evidence, never a join key."""
    out: list[str] = []
    for m in _UPC_LABEL_RE.finditer(text):
        out.append(m.group(1).strip(" -"))
        pos = m.end()
        while True:
            nxt = _UPC_MORE_RE.match(text, pos)
            if not nxt:
                break
            out.append(nxt.group(1).strip(" -"))
            pos = nxt.end()
    return _dedupe(out)


def extract_lots(text: str) -> list[str]:
    """Lot/batch codes following an explicit 'Lot'/'Batch' label.

    Only tokens containing a digit are accepted, so 'lot code 6040 01-6 should not consume it' stops at
    'should' and yields '6040 01-6'. A bare 'code' is never a trigger: 'UPC code 8 38796 00105 1' is a UPC.
    """
    out: list[str] = []
    for m in _LOT_LABEL_RE.finditer(text):
        window = text[m.end():m.end() + 80].split("\n\n")[0]
        tokens: list[str] = []
        for rawtok in window.split():
            tok = rawtok.strip("(),;:“”\"'[]").rstrip(".")
            if not tok or not _LOT_TOKEN_RE.match(tok) or not any(c.isdigit() for c in tok):
                break
            tokens.append(tok)
            if len(tokens) >= 4:
                break
        if tokens:
            out.append(" ".join(tokens))
    return _dedupe(out)


def _dates_in(window: str) -> list[str]:
    stop = re.search(r"(?<=[a-zA-Z])\.(?:\s|$)", window)
    if stop:
        window = window[: stop.start()]
    return ([mm.group(0) for mm in _DATE_TEXT_RE.finditer(window)]
            + [mm.group(0) for mm in _DATE_NUMERIC_RE.finditer(window)])


def extract_best_by(text: str) -> list[str]:
    """Best-by / use-by strings, verbatim. A labelled list ('Use By dates of a, b, c and d') expands.

    The label and its dates are usually one line, but a table cell wraps them ('Use By Dates of' /
    '9/3/2026 to' / '9/17/2026'), so a label that yields nothing on its own line looks two lines further.
    """
    out: list[str] = []
    for m in _BEST_BY_LABEL_RE.finditer(text):
        tail_text = text[m.end():m.end() + 260]
        lines = tail_text.split("\n")
        first_line = lines[0]
        vals = _dates_in(first_line) or _dates_in(" ".join(lines[:3]))
        if vals:
            out.extend(vals)
            continue
        tail = first_line.strip(" :–-,")
        if tail and len(tail) <= 60 and any(c.isdigit() for c in tail):
            out.append(tail)
    return _dedupe(out)


def _is_date_cell(val: str) -> bool:
    """True when a table cell holds dates rather than a code.

    Firms mislabel columns: FreshPoint's 'LOT CODE' column actually holds 'Use By Dates of 9/3/2026 to
    9/17/2026'. Filing that as a lot would make rules.lot_relation report a mismatch against every real
    receipt lot and cut the match score, so the cell's own words decide, not the column header.
    """
    val = val.strip()
    if not val:
        return False
    if _BEST_BY_LABEL_RE.match(val):
        return True
    if not (_DATE_NUMERIC_RE.search(val) or _DATE_TEXT_RE.search(val)):
        return False
    remainder = _DATE_TEXT_RE.sub("", _DATE_NUMERIC_RE.sub("", val))
    return bool(re.fullmatch(r"[\s,;/&\-]*(?:to|and|thru|through|-)?[\s,;/&\-]*", remainder, re.I))


def extract_sizes(text: str) -> list[str]:
    labelled = [m.group(1).strip() for m in _SIZE_LABEL_RE.finditer(text)]
    loose = [m.group(0).strip() for m in _SIZE_TOKEN_RE.finditer(text)]
    return _dedupe(labelled + loose)


def extract_distribution(text: str) -> tuple[list[str], str]:
    """(state codes, the distribution sentence(s) verbatim).

    A sentence counts as distribution only when it carries a distribution cue AND either names a state or
    says nationwide, which keeps 'Products are distributed in 30 lb. tubs' out of the record.
    """
    keep: list[str] = []
    states: list[str] = []
    for sent in _sentences(text):
        if not _DISTRIBUTION_CUE_RE.search(sent):
            continue
        found = extract_states(sent)
        if not found and not _NATIONWIDE_RE.search(sent):
            continue
        keep.append(sent)
        for code in found:
            if code not in states:
                states.append(code)
    return states, " ".join(keep[:4])


def extract_disposition(text: str) -> str:
    """The disposition sentence(s), verbatim (rule 11: copied, never invented)."""
    keep = [s for s in _sentences(text) if _DISPOSITION_CUE_RE.search(s)]
    return " ".join(keep[:4])


def _clean_cell(cell: str) -> str:
    s = re.sub(r"(?s)<[^>]+>", " ", cell)
    s = html_lib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _extract_table_codes(html: str) -> tuple[list[str], list[str], list[str]]:
    """(lots, best_by, upcs) out of an FDA product table, rowspan-aware.

    FDA lists multi-product recalls (Ghirardelli) as a table whose product column is rowspan'd, so rows
    after the first carry fewer cells; the column index shifts left by exactly that many. A lot-column
    cell that actually holds dates is routed to best_by instead (see _is_date_cell).
    """
    lots: list[str] = []
    bests: list[str] = []
    upcs: list[str] = []
    for table in re.findall(r"(?is)<table[^>]*>.*?</table>", html):
        headers = [_clean_cell(c) for c in re.findall(r"(?is)<th[^>]*>(.*?)</th>", table)]
        if not headers:
            first_row = re.search(r"(?is)<tr[^>]*>(.*?)</tr>", table)
            if not first_row:
                continue
            headers = [_clean_cell(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", first_row.group(1))]
        lot_idx: Optional[int] = None
        bb_idx: Optional[int] = None
        upc_idx: Optional[int] = None
        for i, h in enumerate(headers):
            hl = h.lower()
            if upc_idx is None and ("upc" in hl or "u.p.c" in hl or "barcode" in hl):
                upc_idx = i
                continue
            if lot_idx is None and ("lot" in hl or "batch" in hl or "code" in hl):
                lot_idx = i
            if bb_idx is None and ("best" in hl or "use by" in hl or "used by" in hl or "expir" in hl):
                bb_idx = i
        if lot_idx is None and bb_idx is None and upc_idx is None:
            continue
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table):
            if re.search(r"(?is)<th[^>]*>", row):
                continue
            cells = [_clean_cell(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            if not cells:
                continue
            shift = len(headers) - len(cells)
            if shift < 0:
                continue
            for idx, sink in ((lot_idx, lots), (bb_idx, bests), (upc_idx, upcs)):
                if idx is None:
                    continue
                j = idx - shift
                if not (0 <= j < len(cells)):
                    continue
                val = cells[j]
                if not val or len(val) > 60 or not any(c.isdigit() for c in val):
                    continue
                if sink is upcs:
                    if re.fullmatch(r"[0-9][0-9 \-]{8,24}[0-9]", val):
                        upcs.append(val)
                elif sink is lots and _is_date_cell(val):
                    bests.append(val)  # the column says LOT, the cell says dates; the cell wins
                else:
                    sink.append(val)
    return _dedupe(lots), _dedupe(bests), _dedupe(upcs)


def _detect_channel(text: str) -> Channel:
    """Rule 6: regulatory scope is explicit. FDA feeds only; FSIS / USDA Foods arrive by paste, tagged."""
    if _USDA_FOODS_RE.search(text):
        return Channel.USDA_FOODS
    if _FSIS_RE.search(text):
        return Channel.FSIS
    return Channel.FDA


def _split_brands(value: str) -> list[str]:
    if not value or value.strip().lower() in _EMPTY_BRANDS:
        return []
    parts = [p.strip(" .") for p in re.split(r"[,;/]", value)]
    return [p for p in parts if p and p.lower() not in _EMPTY_BRANDS]


def _fill_from_body(raw: RawNotice, body: str, html: str = "") -> None:
    """Run every regex extractor over the announcement body and populate `raw` in place."""
    raw.upcs_as_printed = extract_upcs(body)
    raw.lots = extract_lots(body)
    raw.best_by = extract_best_by(body)
    if html:
        table_lots, table_best, table_upcs = _extract_table_codes(html)
        raw.lots = _dedupe(raw.lots + table_lots)
        raw.best_by = _dedupe(raw.best_by + table_best)
        raw.upcs_as_printed = _dedupe(raw.upcs_as_printed + table_upcs)
    raw.distribution_states, raw.distribution_text = extract_distribution(body)
    raw.disposition_text = extract_disposition(body)
    raw.sizes = extract_sizes(f"{raw.product_description}\n{body}")


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------
def _cache_path(cache_dir: Path, url: str) -> Path:
    return Path(cache_dir) / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".html")


def _norm_url(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def fixture_for_url(url: str) -> Optional[str]:
    """Read-only fallback: the committed fixture HTML for a URL listed in data/fixtures/press/index.json, else None.

    fetch_url itself never consults fixtures (its tests mock the network). The service layer calls this when a
    live fetch is blocked or fails, so the demo's press pages are served without network and nothing is ever
    written into the fixtures directory.
    """
    try:
        press_dir = Path(settings.fixtures_dir) / "press"
        index = press_dir / "index.json"
        if not index.is_file():
            return None
        import json

        mapping = json.loads(index.read_text(encoding="utf-8"))
        want = _norm_url(url)
        for key, filename in mapping.items():
            if _norm_url(key) == want:
                f = press_dir / filename
                if f.is_file():
                    return f.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return None


def fetch_url(url: str, *, cache_dir: Optional[Path] = None, timeout: float = 25.0) -> FetchResult:
    """GET a page with a real browser UA. Never raises: a dead network is a status-0 FetchResult.

    fda.gov answers a non-browser UA with a redirect to /apology_objects/abuse-detection-apology.html
    (observed 2026-09-11: HTTP 404 at that final URL, marker in the URL rather than the body). That is
    reported as blocked=True, never as content, so a walled fetch can never be parsed as an empty recall.
    """
    if cache_dir:
        cached = _cache_path(Path(cache_dir), url)
        if cached.is_file():
            text = cached.read_text(encoding="utf-8", errors="replace")
            return FetchResult(url=url, status=200, text=text, from_cache=True, final_url=url)

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            resp = client.get(url)
    except Exception:
        return FetchResult(url=url, status=0, text="", final_url=url)

    final_url = str(resp.url)
    text = resp.text or ""
    if ABUSE_WALL_MARKER in final_url or ABUSE_WALL_MARKER in text:
        return FetchResult(url=url, status=resp.status_code, text="", blocked=True, final_url=final_url)
    if resp.status_code != 200:
        return FetchResult(url=url, status=resp.status_code, text="", final_url=final_url)

    if cache_dir:
        try:
            d = Path(cache_dir)
            d.mkdir(parents=True, exist_ok=True)
            _cache_path(d, url).write_text(text, encoding="utf-8")
        except OSError:
            pass
    return FetchResult(url=url, status=200, text=text, final_url=final_url)


# ---------------------------------------------------------------------------
# parse_fda_press_page
# ---------------------------------------------------------------------------
def parse_fda_press_page(html: str, url: str = "") -> RawNotice:
    """Parse an FDA 'Company Announcement' press page into a RawNotice. No model calls."""
    raw = RawNotice(source_url=url)
    if not html:
        return raw

    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip()
        raw.title = re.sub(r"\s*\|\s*FDA\s*$", "", title)

    text = _strip_tags(html)
    lines = [ln for ln in (raw_line.strip() for raw_line in text.split("\n")) if ln]
    label_by_norm = {_norm_label(lbl): key for key, lbl in _HEADER_LABELS}

    # --- locate the "Company Announcement" section header that opens the body ---
    body_start_idx = len(lines)
    for i, line in enumerate(lines):
        if line.lower().rstrip(":") not in _BODY_START_MARKERS or line.endswith(":"):
            continue
        # the real section header follows the summary labels; the all-caps banner near the top does not
        if any(_norm_label(x.rstrip(":")) in label_by_norm for x in lines[max(0, i - 16):i]):
            body_start_idx = i
            break

    # --- header block -------------------------------------------------------
    values: dict[str, str] = {}
    for i, line in enumerate(lines[:body_start_idx]):
        if not line.endswith(":"):
            continue
        key = label_by_norm.get(_norm_label(line.rstrip(":")))
        if not key or key in values:
            continue
        for nxt in lines[i + 1: i + 4]:
            if not nxt.endswith(":") and _norm_label(nxt) in _REPEAT_MARKERS:
                continue  # FDA repeats the label as a bare line before the value
            if nxt.endswith(":") and label_by_norm.get(_norm_label(nxt.rstrip(":"))):
                break
            values[key] = nxt
            break

    raw.company_announcement_date = _parse_date(values.get("company_announcement_date", ""))
    raw.fda_publish_date = _parse_date(values.get("fda_publish_date", ""))
    raw.product_type = values.get("product_type", "").strip()
    raw.reason = values.get("reason", "").strip()
    raw.firm = values.get("firm", "").strip()
    raw.brand_names = _split_brands(values.get("brand", ""))
    raw.product_description = values.get("product_description", "").strip()

    # --- announcement body --------------------------------------------------
    body_lines: list[str] = []
    for line in lines[body_start_idx + 1:]:
        if line.lower().rstrip(":") in _BODY_END_MARKERS:
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:  # unfamiliar layout: fall back to the whole page text
        body = "\n".join(lines)
    raw.body_text = body

    _fill_from_body(raw, body, html=html)
    raw.channel = _detect_channel(f"{raw.title}\n{raw.product_type}\n{body}")
    raw.is_food = looks_like_food(raw.title, raw.product_type)
    if raw.channel in (Channel.FSIS, Channel.USDA_FOODS):
        raw.is_food = True  # FSIS and USDA Foods regulate nothing but food
    return raw


# ---------------------------------------------------------------------------
# parse_text
# ---------------------------------------------------------------------------
def parse_text(text: str, source_url: str = "") -> RawNotice:
    """Parse a pasted / forwarded plain-text notice. Same extractors; channel inferred from the wording."""
    raw = RawNotice(source_url=source_url)
    if not text or not text.strip():
        return raw

    body = text.replace("\r\n", "\n").replace("\xa0", " ").strip()
    non_empty = [ln.strip() for ln in body.split("\n") if ln.strip()]
    label_by_norm = {_norm_label(lbl): key for key, lbl in _HEADER_LABELS}

    # an inline header block (someone pasted the FDA summary) still parses
    values: dict[str, str] = {}
    for i, line in enumerate(non_empty):
        m = re.match(r"^([A-Za-z][A-Za-z &()/]{2,40}?)\s*:\s*(.*)$", line)
        if not m:
            continue
        key = label_by_norm.get(_norm_label(m.group(1)))
        if not key or key in values:
            continue
        inline = m.group(2).strip()
        if inline:
            values[key] = inline
            continue
        for nxt in non_empty[i + 1: i + 4]:
            if not nxt.endswith(":") and _norm_label(nxt) in _REPEAT_MARKERS:
                continue
            values[key] = nxt
            break

    raw.title = non_empty[0] if non_empty else ""
    raw.company_announcement_date = _parse_date(values.get("company_announcement_date", ""))
    raw.fda_publish_date = _parse_date(values.get("fda_publish_date", ""))
    raw.product_type = values.get("product_type", "").strip()
    raw.reason = values.get("reason", "").strip()
    raw.firm = values.get("firm", "").strip()
    raw.brand_names = _split_brands(values.get("brand", ""))
    raw.product_description = values.get("product_description", "").strip()
    raw.body_text = body

    _fill_from_body(raw, body)
    raw.channel = _detect_channel(body)
    raw.is_food = looks_like_food(raw.title, raw.product_type)
    if raw.channel in (Channel.FSIS, Channel.USDA_FOODS):
        raw.is_food = True
    return raw


# ---------------------------------------------------------------------------
# pdf_to_text
# ---------------------------------------------------------------------------
def pdf_to_text(path_or_bytes) -> str:
    """Text layer only (pypdf). A scanned PDF has none, and this returns '' rather than guessing."""
    try:
        from io import BytesIO

        from pypdf import PdfReader

        if isinstance(path_or_bytes, (bytes, bytearray)):
            reader = PdfReader(BytesIO(bytes(path_or_bytes)))
        else:
            reader = PdfReader(str(path_or_bytes))
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n".join(pages).strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# poll_fda_rss
# ---------------------------------------------------------------------------
def parse_rss(xml: str) -> list[RssItem]:
    """Parse an FDA recall RSS document into RssItems, newest first."""
    import calendar

    import feedparser

    if not xml:
        return []
    parsed = feedparser.parse(xml)
    items: list[RssItem] = []
    for entry in parsed.entries:
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if struct:
            published = datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
        else:
            published = datetime.now(timezone.utc)
        items.append(RssItem(
            title=re.sub(r"\s+", " ", html_lib.unescape(entry.get("title", ""))).strip(),
            link=(entry.get("link") or "").strip(),
            published=published,
        ))
    items.sort(key=lambda i: i.published, reverse=True)
    return items


def poll_fda_rss(*, cache_dir: Optional[Path] = None) -> list[RssItem]:
    """The FDA recall feed, newest first. Mixes food, drugs, devices and pet food; nothing is filtered here.

    The live feed is always fetched (never served out of the fetch cache) so new recalls appear; cache_dir
    receives a dated snapshot, which doubles as the fallback when the network is down.
    """
    xml = ""
    res = fetch_url(FDA_RSS_URL)  # deliberately no cache_dir: the feed must stay live
    if res.status == 200 and res.text:
        xml = res.text
        if cache_dir:
            try:
                d = Path(cache_dir)
                d.mkdir(parents=True, exist_ok=True)
                (d / f"recalls-{datetime.now(timezone.utc):%Y-%m-%d}.xml").write_text(xml, encoding="utf-8")
            except OSError:
                pass
    elif cache_dir:
        snaps = sorted(Path(cache_dir).glob("recalls-*.xml"))
        if snaps:
            xml = snaps[-1].read_text(encoding="utf-8", errors="replace")
    return parse_rss(xml)


# ---------------------------------------------------------------------------
# openfda_lookup
# ---------------------------------------------------------------------------
def parse_openfda_payload(payload: dict) -> list[OpenFDAEnrichment]:
    """Map an openFDA food/enforcement payload onto typed enrichments."""
    out: list[OpenFDAEnrichment] = []
    for res in (payload or {}).get("results", []) or []:
        cls: Optional[Classification] = None
        raw_cls = (res.get("classification") or "").strip()
        if raw_cls:
            try:
                cls = Classification(raw_cls)
            except ValueError:
                cls = None
        out.append(OpenFDAEnrichment(
            recall_number=(res.get("recall_number") or "").strip(),
            classification=cls,
            code_info=(res.get("code_info") or "").strip(),
            distribution_pattern=(res.get("distribution_pattern") or "").strip(),
            recall_initiation_date=_parse_date(res.get("recall_initiation_date") or ""),
            report_date=_parse_date(res.get("report_date") or ""),
            status=(res.get("status") or "").strip(),
            product_description=(res.get("product_description") or "").strip(),
            recalling_firm=(res.get("recalling_firm") or "").strip(),
            reason_for_recall=(res.get("reason_for_recall") or "").strip(),
        ))
    return out


def build_openfda_search(*, recall_number: str = "", firm: str = "", product_terms: str = "",
                         since_days: int = 120) -> str:
    """openFDA search string. '+AND+' is sent literally; the API rejects a percent-encoded one."""
    clauses: list[str] = []
    if recall_number:
        clauses.append(f'recall_number:"{recall_number.strip()}"')
    if firm:
        clauses.append(f'recalling_firm:"{firm.strip()}"')
    if product_terms:
        clauses.append(f'product_description:"{product_terms.strip()}"')
    # an exact recall number is already unique; a date window could only risk excluding it
    if not recall_number and since_days and since_days > 0:
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=since_days)).strftime("%Y%m%d")
        clauses.append(f"report_date:[{start}+TO+{today:%Y%m%d}]")
    return "+AND+".join(clauses)


def openfda_lookup(*, recall_number: str = "", firm: str = "", product_terms: str = "", since_days: int = 120,
                   limit: int = 10) -> list[OpenFDAEnrichment]:
    """openFDA food enforcement lookup. Enrichment only (rule 1): it never creates a case.

    openFDA answers HTTP 404 for zero results, which is the normal answer for anything younger than about
    11 days (median classification lag 33 days). That is an empty list, not an error.
    """
    search = build_openfda_search(recall_number=recall_number, firm=firm, product_terms=product_terms,
                                  since_days=since_days)
    if not search:
        return []
    url = f"{OPENFDA_URL}?search={search}&limit={max(1, min(int(limit), 100))}"
    try:
        with httpx.Client(follow_redirects=True, timeout=25.0,
                          headers={"User-Agent": settings.user_agent}) as client:
            resp = client.get(url)
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    try:
        return parse_openfda_payload(resp.json())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# to_recall_notice
# ---------------------------------------------------------------------------
_CONFIDENCE_PENALTY = 0.25


def to_recall_notice(raw: RawNotice, source: Source, seen_at: Optional[datetime] = None) -> RecallNotice:
    """RawNotice -> RecallNotice, deterministically.

    extraction_confidence starts at 1.0 and loses 0.25 for each missing field the coordinator cannot work
    without: firm, product name, reason, and any distribution signal. Lots and UPCs are deliberately NOT
    confidence fields: plenty of real recalls name no lot at all, and rule 4 widens rather than drops.
    """
    seen = seen_at or datetime.now(timezone.utc)
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)

    brand = raw.brand_names[0] if raw.brand_names else ""
    size = raw.sizes[0] if raw.sizes else ""

    names = [p.strip(" .;•-") for p in re.split(r"[;\n]+", raw.product_description or "")]
    names = [n for n in names if n]
    if not names:
        names = [raw.title.strip()] if raw.title.strip() else [""]

    products = [
        ProductLine(
            brand=brand,
            name=name,
            size=size,
            upcs_as_printed=list(raw.upcs_as_printed),
            lots=list(raw.lots),
            best_by=list(raw.best_by),
        )
        for name in names
    ]

    missing = 0
    if not raw.firm.strip():
        missing += 1
    if not any(p.name.strip() for p in products):
        missing += 1
    if not raw.reason.strip():
        missing += 1
    if not raw.distribution_states and not raw.distribution_text.strip():
        missing += 1
    confidence = max(0.0, round(1.0 - _CONFIDENCE_PENALTY * missing, 4))

    return RecallNotice(
        source=source,
        source_url=raw.source_url,
        source_seen_at=seen,
        channel=raw.channel,
        firm=raw.firm.strip(),
        products=products,
        reason=raw.reason.strip(),
        classification=None,  # openFDA enriches this later; rule 2 treats None as Class I meanwhile
        product_type=raw.product_type.strip() or "Food & Beverages",
        announcement_date=raw.company_announcement_date,
        publish_date=raw.fda_publish_date,
        distribution_states=list(raw.distribution_states),
        distribution_text=raw.distribution_text.strip(),
        disposition_verbatim=raw.disposition_text.strip(),
        is_food=raw.is_food,
        raw_excerpt=raw.body_text[:1500],
        extraction_confidence=confidence,
        title=raw.title.strip(),
    )
