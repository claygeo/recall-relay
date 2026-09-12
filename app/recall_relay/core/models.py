"""Domain models for Recall Relay.

Every artifact the agent produces is a typed model. The LLM never emits free prose that changes state;
it fills these shapes (via Strands structured output) and deterministic code does the rest.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Source(str, Enum):
    FORWARDED_ALERT = "forwarded_alert"   # the Feeding America / firm notice the food bank already gets
    PASTED_URL = "pasted_url"
    PASTED_TEXT = "pasted_text"
    UPLOADED_PDF = "uploaded_pdf"
    FDA_RSS = "fda_rss"                   # same-day press releases
    OPENFDA = "openfda"                   # weekly enforcement ledger; enrichment only (median 33d lag)
    DRILL = "drill"


class Channel(str, Enum):
    FDA = "FDA"                 # FDA-regulated foods (the feeds cover only this)
    FSIS = "FSIS"               # USDA meat/poultry/egg: paste/forward only (API is bot-walled)
    USDA_FOODS = "USDA_FOODS"   # TEFAP hold-and-recall notices: paste/forward only
    UNKNOWN = "UNKNOWN"


class Classification(str, Enum):
    CLASS_I = "Class I"
    CLASS_II = "Class II"
    CLASS_III = "Class III"
    MARKET_WITHDRAWAL = "Market Withdrawal"


class Verdict(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class CaseStatus(str, Enum):
    NEW = "new"
    DISMISSED = "dismissed"                  # silent; reason logged; never pinged
    NEEDS_HUMAN = "needs_human"              # ambiguous match awaiting coordinator resolution
    AWAITING_APPROVAL = "awaiting_approval"  # the one ping
    RELAYING = "relaying"                    # notices sent, responses coming in
    CHASING = "chasing"                      # reminders / escalations in flight
    CLOSED = "closed"                        # audit packet produced


class ResponseStatus(str, Enum):
    PULLED = "pulled"
    NEVER_RECEIVED = "never_received"
    ALREADY_DISTRIBUTED = "already_distributed"
    NEED_PICKUP = "need_pickup"


class ReceiptChannel(str, Enum):
    RETAIL_RESCUE = "retail_rescue"   # store donations; UPC-less, lot rarely captured
    PURCHASE = "purchase"
    TEFAP = "tefap"                   # USDA Foods
    FOOD_DRIVE = "food_drive"
    SALVAGE = "salvage"               # reclamation / repack: no lot lineage (rule 7)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
class Agency(BaseModel):
    id: str
    name: str
    kind: str = Field(description="church pantry, soup kitchen, school pantry, shelter, mobile pantry")
    open_schedule: str = Field(description="human text, e.g. Tue/Thu 9-12 or first Saturday monthly")
    same_day_distribution: bool = False
    languages: list[str] = Field(default_factory=lambda: ["en"])
    contact_name: str
    contact_email: str
    contact_phone: str
    city: str = "Miami"


class Receipt(BaseModel):
    """One receiving-ledger row. Deliberately messy: store brands, blank UPCs, lots on ~30% of rows."""
    id: int
    received_at: date
    donor: str
    channel: ReceiptChannel
    brand: str = ""
    product: str
    size: str = ""
    upc: str = ""
    lot: str = ""
    best_by: Optional[date] = None
    cases: int
    storage: Literal["frozen", "refrigerated", "dry"] = "dry"
    notes: str = ""


class Distribution(BaseModel):
    """One distribution-log row: cases from a receipt shipped to an agency on a date."""
    id: int
    receipt_id: int
    agency_id: str
    shipped_at: date
    cases: int


# ---------------------------------------------------------------------------
# Recall intake
# ---------------------------------------------------------------------------
class ProductLine(BaseModel):
    brand: str = ""
    name: str
    size: str = ""
    upcs_as_printed: list[str] = Field(default_factory=list, description="verbatim; never a join key")
    lots: list[str] = Field(default_factory=list)
    best_by: list[str] = Field(default_factory=list, description="verbatim date strings")


class RecallNotice(BaseModel):
    source: Source
    source_url: str = ""
    source_seen_at: datetime
    channel: Channel = Channel.FDA
    recall_number: str = ""
    event_id: str = ""
    firm: str
    products: list[ProductLine]
    reason: str
    classification: Optional[Classification] = None
    product_type: str = "Food & Beverages"
    announcement_date: Optional[date] = None
    publish_date: Optional[date] = None
    distribution_states: list[str] = Field(default_factory=list, description="two-letter codes")
    distribution_text: str = ""
    disposition_verbatim: str = Field("", description="copied from the source notice, never invented (rule 11)")
    is_food: bool = True
    raw_excerpt: str = Field("", description="first ~1500 chars of the normalized notice body")
    extraction_confidence: float = 1.0
    title: str = ""


class OpenFDAEnrichment(BaseModel):
    recall_number: str
    classification: Optional[Classification] = None
    code_info: str = ""
    distribution_pattern: str = ""
    recall_initiation_date: Optional[date] = None
    report_date: Optional[date] = None
    status: str = ""
    product_description: str = ""
    recalling_firm: str = ""
    reason_for_recall: str = ""


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
class Candidate(BaseModel):
    receipt_id: int
    brand: str
    product: str
    size: str
    lot: str
    received_at: date
    cases: int
    channel: ReceiptChannel
    brand_score: float
    product_score: float
    size_score: float
    in_window: bool
    lot_relation: Literal["match", "mismatch", "receipt_has_no_lot", "notice_has_no_lot"]
    deterministic_score: float = Field(description="0-100 composite; threshold logic lives in rules.py")
    note: str = ""


class MatchVerdict(BaseModel):
    verdict: Verdict
    matched_receipt_ids: list[int] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, description="one line per matched/rejected candidate")
    confidence: float = Field(ge=0, le=1)
    widening_applied: bool = Field(False, description="rule 4: receipt had no lot, whole line treated as affected")
    reason: str


# ---------------------------------------------------------------------------
# Pull list, notices, signs
# ---------------------------------------------------------------------------
class PullListItem(BaseModel):
    receipt_id: int
    agency_id: Optional[str] = Field(None, description="None means still on hand at the food bank")
    cases: int
    shipped_at: Optional[date] = None
    lot: str = ""
    lot_known: bool
    physical_sort_required: bool = Field(False, description="rule 7: salvage/repack rows")
    note: str = ""


class PullList(BaseModel):
    case_id: str
    on_hand_cases: int
    items: list[PullListItem]
    agencies_affected: list[str]
    widening_applied: bool
    summary: str


class ResponseOption(BaseModel):
    status: ResponseStatus
    label: str


class AgencyNotice(BaseModel):
    agency_id: str
    recall_number: str
    classification: str
    product: str
    lots: list[str]
    best_by: list[str]
    reason: str
    disposition_verbatim: str
    cases_shipped: int
    shipped_dates: list[str]
    subject: str
    body: str = Field(description="plain text email body; cites recall number and source URL")
    actions: list[ResponseOption]


class ClientSign(BaseModel):
    agency_id: str
    language: Literal["en", "es", "ht"]
    title: str
    product_line: str
    what_to_do: str
    symptoms: str
    agency_contact: str
    source_url: str


class CallScript(BaseModel):
    agency_id: str
    contact_name: str
    contact_phone: str
    opening: str
    ask: str
    if_pulled: str
    if_distributed: str
    close: str


# ---------------------------------------------------------------------------
# Case register and audit
# ---------------------------------------------------------------------------
class AuditEvent(BaseModel):
    at: datetime
    case_id: str
    actor: Literal["agent", "coordinator", "agency", "system"]
    kind: str
    detail: str


class RecallCase(BaseModel):
    id: str
    status: CaseStatus
    is_drill: bool = False
    created_at: datetime
    notice: RecallNotice
    enrichment: Optional[OpenFDAEnrichment] = None
    verdict: Optional[MatchVerdict] = None
    pull_list: Optional[PullList] = None
    notices: list[AgencyNotice] = Field(default_factory=list)
    signs: list[ClientSign] = Field(default_factory=list)
    approved_at: Optional[datetime] = None
    dismissed_reason: str = ""
    closed_at: Optional[datetime] = None


class AuditPacket(BaseModel):
    case_id: str
    is_drill: bool
    recall_number: str
    generated_at: datetime
    sources: list[str]
    matched_receipts: list[int]
    pull_list: Optional[PullList]
    notices_sent: int
    responses: list[dict]
    followups: list[dict]
    disposition_verbatim: str
    approvals: list[str]
    elapsed_notice_to_full_trace: Optional[str]
    events: list[AuditEvent]


class Decision(BaseModel):
    """Coordinator decisions the agent must remember (rule 14)."""
    id: int
    created_at: datetime
    text: str
    kind: Literal["brand_never_received", "agency_closed", "row_correction", "dismissal", "other"] = "other"
