"""The dashboard, offline.

Every test here runs against a temp SQLite database and a stubbed agent façade: no network, no model
calls, no strands import required. What is being tested is the web layer's own contract -- what renders,
what is refused, what is audited, and what the demo reset is allowed to destroy.

Windows note (see tests/conftest.py): never a POSIX temp path; tmp_path only.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from recall_relay.core.models import (
    AgencyNotice,
    CaseStatus,
    Classification,
    MatchVerdict,
    ProductLine,
    PullList,
    PullListItem,
    RecallCase,
    RecallNotice,
    ResponseStatus,
    Source,
    Verdict,
)
from recall_relay.core.rules import RESPONSE_OPTIONS
from recall_relay.core.store import Store
from recall_relay.web.app import create_app
from recall_relay.web.security import reset_all_guards, scan_flight, sign_token

HERO_RECEIPT_ID = 17


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_guards():
    """The limiter, the budget and the scan flight are process-wide singletons."""
    reset_all_guards()
    yield
    reset_all_guards()


@pytest.fixture
def web_store(tmp_path) -> Store:
    """A store carrying the demo ledger, or a minimal inline one if the seeder is unavailable."""
    store = Store(tmp_path / "web_test.db")
    try:
        from recall_relay.core import seed as seed_module

        seed_module.load_seed(store)
    except Exception:  # pragma: no cover - seeder mid-edit: build just enough ledger to render
        from recall_relay.core.models import Agency, Distribution, Receipt, ReceiptChannel

        store.upsert_agency(
            Agency(
                id="AG-01",
                name="Iglesia Nueva Vida Pantry",
                kind="church pantry",
                open_schedule="Tue/Thu 9-12",
                languages=["en", "es"],
                contact_name="Marta Ruiz",
                contact_email="pantry@example.org",
                contact_phone="305-555-0101",
            )
        )
        store.upsert_receipt(
            Receipt(
                id=HERO_RECEIPT_ID,
                received_at=datetime(2026, 8, 24, tzinfo=timezone.utc).date(),
                donor="Walmart Supercenter #1234 retail rescue",
                channel=ReceiptChannel.RETAIL_RESCUE,
                brand="Great Value",
                product="Organic Triple Berry Blend",
                size="10 oz",
                cases=30,
                storage="frozen",
            )
        )
        store.add_distribution(
            Distribution(
                id=1,
                receipt_id=HERO_RECEIPT_ID,
                agency_id="AG-01",
                shipped_at=datetime(2026, 9, 4, tzinfo=timezone.utc).date(),
                cases=22,
            )
        )
    return store


@pytest.fixture
def stub_service(monkeypatch):
    """A stand-in for recall_relay.agents.service: the web layer must not need the agent stack to be
    importable, and the tests must never reach a model."""
    import recall_relay.agents as agents_pkg

    module = types.ModuleType("recall_relay.agents.service")
    module.calls = []

    def ping_text(case, store=None):
        return f"PING for {case.id}"

    async def scan(store, *, live=True, snapshot=True):
        module.calls.append(("scan", live, snapshot))
        yield {"type": "feed", "source": "snapshot", "url": "fixture://rss", "items": 2}
        yield {"type": "item", "index": 1, "total": 2, "title": "A non-food recall",
               "link": "https://example.test/1", "decision": "skipped_non_food"}
        yield {"type": "item", "index": 2, "total": 2, "title": "Great Value Organic Triple Berry Blend",
               "link": "https://example.test/2", "decision": "processed",
               "case_id": "RC-TEST-001", "status": "awaiting_approval"}
        yield {"type": "done", "tally": {"seen": 2, "skipped_non_food": 1, "already_seen": 0,
                                         "dismissed": 0, "processed": 1, "needs_human": 0,
                                         "awaiting_approval": 1, "errors": 0}}

    def record_response(store, token, status, count=None, free_text="", **kwargs):
        module.calls.append(("record_response", token, getattr(status, "value", status), count, free_text))
        resolved = store.resolve_token(token)
        if resolved is None:
            raise KeyError(token)
        case_id, agency_id = resolved
        store.record_response(case_id, agency_id, ResponseStatus(status), count, free_text, token)
        return store.get_case(case_id)

    def approve(store, case_id):
        module.calls.append(("approve", case_id))
        case = store.get_case(case_id)
        case.status = CaseStatus.RELAYING
        case.approved_at = store.now()
        store.save_case(case)
        return case

    def dismiss(store, case_id, reason):
        module.calls.append(("dismiss", case_id, reason))
        case = store.get_case(case_id)
        case.status = CaseStatus.DISMISSED
        case.dismissed_reason = reason
        store.save_case(case)
        return case

    def resolve_needs_human(store, case_id, decision_text, treat_as, **kwargs):
        module.calls.append(("resolve_needs_human", case_id, decision_text, treat_as))
        return store.get_case(case_id)

    def run_followups(store, *, now=None):
        module.calls.append(("run_followups",))
        return []

    def close_case(store, case_id):
        module.calls.append(("close_case", case_id))
        case = store.get_case(case_id)
        case.status = CaseStatus.CLOSED
        case.closed_at = store.now()
        store.save_case(case)
        return case

    async def intake_url(store, url, **kwargs):
        raise RuntimeError(f"stub intake refuses {url}")

    async def intake_text(store, text, **kwargs):
        raise RuntimeError("stub intake refuses text")

    async def intake_pdf(store, data, **kwargs):
        raise RuntimeError("stub intake refuses pdf")

    for fn in (ping_text, scan, record_response, approve, dismiss, resolve_needs_human,
               run_followups, close_case, intake_url, intake_text, intake_pdf):
        setattr(module, fn.__name__, fn)

    monkeypatch.setitem(sys.modules, "recall_relay.agents.service", module)
    monkeypatch.setattr(agents_pkg, "service", module, raising=False)
    return module


@pytest.fixture
def client(web_store, stub_service) -> TestClient:
    return TestClient(create_app(store=web_store))


# ---------------------------------------------------------------------------
# helpers: a finished case, built inline
# ---------------------------------------------------------------------------
def build_closed_case(store: Store, *, case_id: str = "RC-2026-0912-001") -> RecallCase:
    """A case that has been all the way round the loop: matched, relayed, answered, closed."""
    agency = store.list_agencies()[0]
    seen = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
    notice = RecallNotice(
        source=Source.FDA_RSS,
        source_url="https://www.fda.gov/safety/recalls/test-triple-berry",
        source_seen_at=seen,
        recall_number="H-1181-2026",
        firm="Frutas y Hortalizas del Sur S.A.",
        products=[
            ProductLine(
                brand="Great Value",
                name="Organic Triple Berry Blend",
                size="10 oz",
                upcs_as_printed=["0 78742 12345 6"],
                lots=["L2026-0714"],
                best_by=["2027-07-14"],
            )
        ],
        reason="possible E. coli O145 contamination",
        classification=Classification.CLASS_I,
        announcement_date=seen.date(),
        distribution_states=["FL", "GA", "AL"],
        distribution_text="Distributed to Walmart stores in 27 states including Florida.",
        disposition_verbatim="Consumers should not eat the product and should return it to the place of purchase.",
        title="Great Value Organic Triple Berry Blend recalled",
    )
    case = RecallCase(
        id=case_id,
        status=CaseStatus.RELAYING,
        created_at=seen + timedelta(hours=1),
        notice=notice,
        verdict=MatchVerdict(
            verdict=Verdict.MATCH,
            matched_receipt_ids=[HERO_RECEIPT_ID],
            evidence=[f"receipt {HERO_RECEIPT_ID}: brand and line match; receipt carries no lot"],
            confidence=0.92,
            widening_applied=True,
            reason="brand and product line match; the receipt has no lot so the whole line is affected",
        ),
        pull_list=PullList(
            case_id=case_id,
            on_hand_cases=8,
            items=[
                PullListItem(receipt_id=HERO_RECEIPT_ID, agency_id=None, cases=8, lot="", lot_known=False,
                             note="still on hand; HOLD tag applied"),
                PullListItem(receipt_id=HERO_RECEIPT_ID, agency_id=agency.id, cases=22,
                             shipped_at=datetime(2026, 9, 4, tzinfo=timezone.utc).date(),
                             lot="", lot_known=False, note="shipped two days after the press release"),
            ],
            agencies_affected=[agency.id],
            widening_applied=True,
            summary="30 cases affected: 8 on hand, 22 shipped to 1 agency.",
        ),
        notices=[
            AgencyNotice(
                agency_id=agency.id,
                recall_number="H-1181-2026",
                classification="Class I",
                product="Great Value Organic Triple Berry Blend 10 oz",
                lots=["L2026-0714"],
                best_by=["2027-07-14"],
                reason="possible E. coli O145 contamination",
                disposition_verbatim=notice.disposition_verbatim,
                cases_shipped=22,
                shipped_dates=["2026-09-04"],
                subject="Urgent recall: Great Value Organic Triple Berry Blend",
                body="Please pull this product from your shelves today and tell us what you find.",
                actions=list(RESPONSE_OPTIONS),
            )
        ],
        approved_at=seen + timedelta(hours=2),
    )
    store.create_case(case)
    store.set_hold(HERO_RECEIPT_ID, case.id, True)
    store.audit(case.id, "system", "case_opened", "fda_rss :: test fixture")
    store.audit(case.id, "coordinator", "approved", "1 notice approved for relay")

    token = store.issue_token(case.id, agency.id)
    store.record_mail(
        case_id=case.id,
        agency_id=agency.id,
        to_addr=agency.contact_email,
        subject="Urgent recall: Great Value Organic Triple Berry Blend",
        body="Please pull this product from your shelves today.",
        kind="notice",
        token=token,
    )
    store.record_response(case.id, agency.id, ResponseStatus.PULLED, 22, "found all 22", token)
    store.audit(case.id, "agency", "response", f"{agency.id}: pulled (22 cases)")

    case.status = CaseStatus.CLOSED
    case.closed_at = seen + timedelta(hours=20)
    store.save_case(case)
    store.audit(case.id, "system", "closed", "audit packet filed; 1 notice, 1 response")
    return case


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------
def test_ledger_renders_with_the_receipt_count(client, web_store):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    expected = len(web_store.list_receipts())
    assert f"Receipts <b>{expected}</b>" in body
    assert "Receiving ledger" in body
    # the accession-number column is the receipt id in mono, zero padded
    assert f'class="id">{HERO_RECEIPT_ID:04d}<' in body
    # never a default admin-template look: no icon font, no dark mode, no framework
    assert "font-awesome" not in body.lower()
    assert "bootstrap" not in body.lower()


def test_ledger_shows_the_hold_tag_inline(client, web_store):
    case = build_closed_case(web_store)
    body = client.get("/ledger").text
    assert 'class="tag tag--hold"' in body
    assert f'href="/cases/{case.id}"' in body


def test_ledger_lists_agencies_and_the_distribution_log(client, web_store):
    body = client.get("/ledger").text
    assert "Distribution log" in body
    assert "Partner agencies" in body
    for agency in web_store.list_agencies()[:3]:
        assert agency.name in body


# ---------------------------------------------------------------------------
# cases and the case page
# ---------------------------------------------------------------------------
def test_cases_and_case_page_render(client, web_store):
    case = build_closed_case(web_store)
    listing = client.get("/cases")
    assert listing.status_code == 200
    assert case.id in listing.text

    page = client.get(f"/cases/{case.id}")
    assert page.status_code == 200
    body = page.text
    assert "Great Value Organic Triple Berry Blend 10 oz" in body   # the 38px headline
    assert f"PING for {case.id}" in body                             # service.ping_text, verbatim
    assert "Urgent recall: Great Value Organic Triple Berry Blend" in body  # the drafted notice preview
    assert "H-1181-2026" in body


def test_unknown_case_is_a_sheet_not_a_stack_trace(client):
    response = client.get("/cases/RC-NOPE-001")
    assert response.status_code == 404
    assert "There is no case RC-NOPE-001." in response.text


# ---------------------------------------------------------------------------
# the inbox mirror
# ---------------------------------------------------------------------------
def test_inbox_renders_outbox_rows(client, web_store):
    case = build_closed_case(web_store)
    agency = web_store.list_agencies()[0]

    response = client.get("/inbox")
    assert response.status_code == 200
    body = response.text
    assert "Urgent recall: Great Value Organic Triple Berry Blend" in body
    assert agency.contact_email in body
    assert "Messages <b>1</b>" in body
    # the four one-click replies are underlined text links, not buttons
    for option in RESPONSE_OPTIONS:
        assert f"?status={option.status.value}" in body

    per_agency = client.get(f"/inbox/{agency.id}")
    assert per_agency.status_code == 200
    assert case.id in per_agency.text

    assert client.get("/inbox/AG-NOPE").status_code == 404


# ---------------------------------------------------------------------------
# the agency response door
# ---------------------------------------------------------------------------
def test_response_link_with_a_bad_signature_is_refused(client, web_store, stub_service):
    build_closed_case(web_store)
    token = web_store.outbox()[0]["token"]
    tampered = sign_token(token)[:-4] + "xxxx"

    response = client.get(f"/r/{tampered}?status=never_received")
    assert response.status_code == 400
    assert "That link is not valid" in response.text
    assert not [c for c in stub_service.calls if c[0] == "record_response"]


def test_response_link_with_a_good_signature_records_the_answer(client, web_store, stub_service):
    case = build_closed_case(web_store)
    agency = web_store.list_agencies()[0]
    token = web_store.issue_token(case.id, agency.id)

    response = client.get(f"/r/{sign_token(token)}?status=never_received")
    assert response.status_code == 200
    assert "Thank you, recorded" in response.text
    assert "Never received" in response.text

    recorded = [c for c in stub_service.calls if c[0] == "record_response"]
    assert recorded == [("record_response", token, "never_received", None, "")]


def test_pulled_asks_for_a_count_before_recording(client, web_store, stub_service):
    case = build_closed_case(web_store)
    agency = web_store.list_agencies()[0]
    token = web_store.issue_token(case.id, agency.id)
    signed = sign_token(token)

    form = client.get(f"/r/{signed}?status=pulled")
    assert form.status_code == 200
    assert "How many cases" in form.text
    assert not [c for c in stub_service.calls if c[0] == "record_response"]

    posted = client.post(f"/r/{signed}", data={"status": "pulled", "count": "12", "free_text": "two pallets"})
    assert posted.status_code == 200
    assert "Thank you, recorded" in posted.text
    assert [c for c in stub_service.calls if c[0] == "record_response"] == [
        ("record_response", token, "pulled", 12, "two pallets")
    ]


def test_raw_token_from_the_mail_body_still_resolves(client, web_store, stub_service):
    case = build_closed_case(web_store)
    agency = web_store.list_agencies()[0]
    token = web_store.issue_token(case.id, agency.id)

    response = client.get(f"/r/{token}?status=already_distributed")
    assert response.status_code == 200
    assert [c for c in stub_service.calls if c[0] == "record_response"][0][1] == token


# ---------------------------------------------------------------------------
# the scan: one at a time, and a daily budget
# ---------------------------------------------------------------------------
def _drain_scan(client, job: str) -> str:
    with client.stream("GET", f"/api/scan/stream?job={job}") as stream:
        return "".join(chunk for chunk in stream.iter_text())


def test_scan_streams_events_and_finishes(client, stub_service, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_RUNS_PER_DAY", "10")
    started = client.post("/api/scan", headers={"Accept": "application/json"})
    assert started.status_code == 200
    job = started.json()["job"]

    body = _drain_scan(client, job)
    assert '"type": "feed"' in body
    assert '"decision": "skipped_non_food"' in body
    assert '"type": "done"' in body
    assert '"type": "closed"' in body
    assert [c for c in stub_service.calls if c[0] == "scan"]


def test_scan_is_one_at_a_time(client, monkeypatch):
    monkeypatch.setenv("MAX_AGENT_RUNS_PER_DAY", "10")
    scan_flight.acquire("already-running")
    try:
        refused = client.post("/api/scan", headers={"Accept": "application/json"})
        assert refused.status_code == 429
        assert "already running" in refused.json()["headline"].lower()
    finally:
        scan_flight.reset()


def test_scan_respects_the_daily_agent_budget(client, monkeypatch):
    """Planted defect check: with the budget set to one run, the second scan must be refused."""
    monkeypatch.setenv("MAX_AGENT_RUNS_PER_DAY", "1")

    first = client.post("/api/scan", headers={"Accept": "application/json"})
    assert first.status_code == 200
    _drain_scan(client, first.json()["job"])

    second = client.post("/api/scan", headers={"Accept": "application/json"})
    assert second.status_code == 429
    payload = second.json()
    assert "budget" in payload["headline"].lower()
    assert second.headers["retry-after"]

    # and a browser (not fetch) gets the same refusal as a readable sheet
    sheet = client.post("/api/scan")
    assert sheet.status_code == 429
    assert "text/html" in sheet.headers["content-type"]


def test_unknown_scan_job_is_404(client):
    assert client.get("/api/scan/stream?job=nope").status_code == 404


# ---------------------------------------------------------------------------
# the data RPC the deployed Runtime uses
# ---------------------------------------------------------------------------
def test_data_rpc_without_the_secret_is_forbidden(client, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_SECRET", "s3cret")
    response = client.post("/api/data/rpc", json={"method": "list_receipts"})
    assert response.status_code == 403


def test_data_rpc_is_closed_when_no_secret_is_configured(client, monkeypatch):
    monkeypatch.delenv("AGENT_DATA_SECRET", raising=False)
    response = client.post("/api/data/rpc", json={"method": "list_receipts"},
                           headers={"X-Relay-Secret": "anything"})
    assert response.status_code == 403


def test_data_rpc_refuses_a_method_that_is_not_allow_listed(client, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_SECRET", "s3cret")
    response = client.post(
        "/api/data/rpc",
        json={"method": "wipe_all", "args": [], "kwargs": {}},
        headers={"X-Relay-Secret": "s3cret"},
    )
    assert response.status_code == 400
    assert "not allow-listed" in response.json()["error"]


def test_data_rpc_returns_json_for_an_allow_listed_method(client, web_store, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_SECRET", "s3cret")
    headers = {"X-Relay-Secret": "s3cret"}

    listing = client.post("/api/data/rpc", json={"method": "list_receipts"}, headers=headers)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["ok"] is True
    assert len(payload["result"]) == len(web_store.list_receipts())
    assert payload["result"][0]["received_at"]  # dates arrive ISO-8601, not as objects

    one = client.post("/api/data/rpc", json={"method": "get_receipt", "args": [HERO_RECEIPT_ID]}, headers=headers)
    assert one.json()["result"]["id"] == HERO_RECEIPT_ID

    holds = client.post("/api/data/rpc", json={"method": "holds"}, headers=headers)
    assert holds.json()["result"] == {}  # int-keyed dict survives the JSON round trip as strings

    clock = client.post("/api/data/rpc", json={"method": "now"}, headers=headers)
    assert datetime.fromisoformat(clock.json()["result"]).tzinfo is not None


def test_remote_store_speaks_the_same_api(client, web_store, monkeypatch):
    """RemoteStore is what makes the deployed Runtime stateless: same names, same models, over HTTP."""
    from recall_relay.core.remote_store import RemoteStore

    monkeypatch.setenv("AGENT_DATA_SECRET", "s3cret")
    remote = RemoteStore("http://testserver", "s3cret", client=client)

    assert len(remote.list_receipts()) == len(web_store.list_receipts())
    assert remote.get_receipt(HERO_RECEIPT_ID).id == HERO_RECEIPT_ID
    assert {a.id for a in remote.list_agencies()} == {a.id for a in web_store.list_agencies()}
    assert remote.now().tzinfo is not None
    assert remote.ledger_brands() == web_store.ledger_brands()

    case = build_closed_case(web_store)
    assert remote.get_case(case.id).notice.recall_number == "H-1181-2026"
    assert remote.holds() == {HERO_RECEIPT_ID: case.id}
    assert remote.resolve_token(web_store.outbox()[0]["token"])[0] == case.id


# ---------------------------------------------------------------------------
# the audit packet
# ---------------------------------------------------------------------------
def test_packet_page_renders_the_timeline(client, web_store):
    case = build_closed_case(web_store)
    response = client.get(f"/cases/{case.id}/packet")
    assert response.status_code == 200
    body = response.text
    assert "Audit packet" in body
    assert "case_opened" in body and "closed" in body
    assert "Consumers should not eat the product" in body  # disposition, verbatim
    assert "@media print" not in body  # the print rules live in the stylesheet, not inline


def test_packet_pdf_is_a_real_pdf(client, web_store):
    case = build_closed_case(web_store)
    response = client.get(f"/cases/{case.id}/packet.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content[:5] == b"%PDF-"
    assert len(response.content) > 3000, "a packet with a matched row, a response and a timeline is not tiny"
    assert case.id.encode() in response.content or b"/Title" in response.content


def test_packet_tab_redirects_to_the_newest_packet(client, web_store):
    case = build_closed_case(web_store)
    response = client.get("/packet")
    assert response.status_code == 200
    assert f"/cases/{case.id}/packet" in str(response.url)


def test_packet_tab_is_an_empty_state_when_nothing_is_filed(client):
    response = client.get("/packet")
    assert response.status_code == 200
    assert "No packet has been filed yet" in response.text


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------
def test_approve_and_dismiss_go_through_the_service_and_audit(client, web_store, stub_service):
    case = build_closed_case(web_store)

    approved = client.post(f"/api/cases/{case.id}/approve", headers={"Accept": "application/json"})
    assert approved.status_code == 200
    assert approved.json()["redirect"] == f"/cases/{case.id}"
    assert ("approve", case.id) in stub_service.calls

    dismissed = client.post(f"/api/cases/{case.id}/dismiss", data={"reason": "wrong brand"},
                            follow_redirects=False)
    assert dismissed.status_code == 303
    assert ("dismiss", case.id, "wrong brand") in stub_service.calls

    kinds = [e.kind for e in web_store.events(case.id)]
    assert "ui_approve" in kinds and "ui_dismiss" in kinds


def test_clock_advance_moves_the_demo_clock(client, web_store):
    before = web_store.now()
    response = client.post("/api/clock/advance", json={"hours": 24}, headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert web_store.now() - before >= timedelta(hours=23, minutes=59)


def test_followups_control_calls_the_service(client, stub_service):
    response = client.post("/api/followups/run", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert ("run_followups",) in stub_service.calls


def test_intake_without_a_source_is_a_readable_refusal(client):
    response = client.post("/api/intake", data={"url": "", "text": ""})
    assert response.status_code == 400
    assert "Nothing to read" in response.text


# ---------------------------------------------------------------------------
# health and the demo reset
# ---------------------------------------------------------------------------
def test_health(client, web_store):
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["counts"]["receipts"] == len(web_store.list_receipts())
    assert payload["counts"]["agencies"] == len(web_store.list_agencies())
    assert payload["agent_runs_today"]["limit"] >= 1


def test_demo_reset_keeps_the_ledger_and_clears_the_cases(client, web_store):
    """Planted defect check: the reset must be surgical. The ledger is the food bank's own data."""
    build_closed_case(web_store)

    # a row the seeder does not know about: a reset that wipes and reseeds the ledger loses it,
    # a reset that only clears case state keeps it. This is what "keeps the ledger" has to mean.
    from recall_relay.core.models import Receipt, ReceiptChannel

    web_store.upsert_receipt(
        Receipt(
            id=9001,
            received_at=datetime(2026, 9, 9, tzinfo=timezone.utc).date(),
            donor="Hand-entered correction by the coordinator",
            channel=ReceiptChannel.PURCHASE,
            brand="Local Co-op",
            product="Brown rice 25 lb",
            cases=4,
        )
    )
    receipts_before = len(web_store.list_receipts())
    agencies_before = len(web_store.list_agencies())
    distributions_before = len(web_store.list_distributions())
    assert receipts_before and web_store.list_cases() and web_store.outbox() and web_store.holds()

    response = client.post("/api/demo/reset", headers={"Accept": "application/json"})
    assert response.status_code == 200

    assert len(web_store.list_receipts()) == receipts_before
    assert web_store.get_receipt(9001) is not None, "the reset destroyed the food bank's own ledger row"
    assert len(web_store.list_agencies()) == agencies_before
    assert len(web_store.list_distributions()) == distributions_before
    assert web_store.list_cases() == []
    assert web_store.outbox() == []
    assert web_store.holds() == {}


def test_demo_reset_is_throttled(client):
    assert client.post("/api/demo/reset", headers={"Accept": "application/json"}).status_code == 200
    second = client.post("/api/demo/reset", headers={"Accept": "application/json"})
    assert second.status_code == 429


def test_the_service_boundary_tolerates_an_async_facade(client, web_store, stub_service, monkeypatch):
    """The façade's contract was specified async and is currently implemented sync. Either must work,
    because the web layer and the agent layer were built by different hands against the same brief."""
    case = build_closed_case(web_store)

    async def async_close_case(store, case_id):
        stub_service.calls.append(("close_case_async", case_id))
        c = store.get_case(case_id)
        c.status = CaseStatus.CLOSED
        store.save_case(c)
        return c

    monkeypatch.setattr(stub_service, "close_case", async_close_case)
    response = client.post(f"/api/cases/{case.id}/close", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.json()["redirect"] == f"/cases/{case.id}/packet"
    assert ("close_case_async", case.id) in stub_service.calls
