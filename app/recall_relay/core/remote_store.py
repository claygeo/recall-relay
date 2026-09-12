"""RemoteStore: the Store API over HTTP, so a deployed agent can be stateless.

DECISIONS #5: the dashboard is the system of record. Locally the agent tools hold a `Store` and talk to
SQLite in process; on AgentCore Runtime the same tools hold a `RemoteStore` and every call becomes one
POST to the dashboard's allow-listed `/api/data/rpc` with a shared secret. `AGENT_BACKEND` is a transport
switch, never a storage switch -- the register is the same rows either way.

The method names, arguments and return types match `core.store.Store` exactly, so nothing above this line
needs to know which one it is holding.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

import httpx
from pydantic import BaseModel

from .models import (
    Agency,
    AuditEvent,
    CaseStatus,
    Decision,
    Distribution,
    Receipt,
    RecallCase,
    ResponseStatus,
)

DEFAULT_TIMEOUT = 30.0
RPC_PATH = "/api/data/rpc"


class RemoteStoreError(RuntimeError):
    """The dashboard refused or could not answer an RPC."""


def _wire(value: Any) -> Any:
    """Arguments, in a shape JSON can carry and the dashboard's Store can take back."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_wire(v) for v in value]
    return value


class RemoteStore:
    """Every `Store` method the agent layer is allowed to call, over one HTTP endpoint."""

    def __init__(self, base_url: str, secret: str, *, timeout: float = DEFAULT_TIMEOUT,
                 client: Optional[httpx.Client] = None):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self._timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------ wire
    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        payload = {"method": method, "args": [_wire(a) for a in args],
                   "kwargs": {k: _wire(v) for k, v in kwargs.items()}}
        try:
            response = self._client.post(
                self.base_url + RPC_PATH,
                json=payload,
                headers={"X-Relay-Secret": self.secret, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise RemoteStoreError(f"{method}: {type(exc).__name__}: {exc}") from exc

        if response.status_code == 403:
            raise RemoteStoreError(f"{method}: the dashboard refused the shared secret (403)")
        try:
            body = response.json()
        except ValueError as exc:
            raise RemoteStoreError(f"{method}: {response.status_code} with a non-JSON body") from exc
        if response.status_code >= 400 or not body.get("ok"):
            raise RemoteStoreError(f"{method}: {body.get('error', response.status_code)}")
        return body.get("result")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RemoteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- ledger
    def list_agencies(self) -> list[Agency]:
        return [Agency.model_validate(r) for r in self.call("list_agencies") or []]

    def get_agency(self, agency_id: str) -> Optional[Agency]:
        raw = self.call("get_agency", agency_id)
        return Agency.model_validate(raw) if raw else None

    def list_receipts(self) -> list[Receipt]:
        return [Receipt.model_validate(r) for r in self.call("list_receipts") or []]

    def get_receipt(self, receipt_id: int) -> Optional[Receipt]:
        raw = self.call("get_receipt", receipt_id)
        return Receipt.model_validate(raw) if raw else None

    def list_distributions(self, receipt_id: Optional[int] = None) -> list[Distribution]:
        return [Distribution.model_validate(r) for r in self.call("list_distributions", receipt_id) or []]

    def on_hand(self, receipt_id: int) -> int:
        return int(self.call("on_hand", receipt_id) or 0)

    def ledger_brands(self) -> set[str]:
        """Derived locally: the allow-list keeps the RPC surface to rows, not conveniences."""
        return {r.brand for r in self.list_receipts() if r.brand}

    # ----------------------------------------------------------------- holds
    def set_hold(self, receipt_id: int, case_id: str, on: bool) -> None:
        self.call("set_hold", receipt_id, case_id, on)

    def holds(self) -> dict[int, str]:
        raw = self.call("holds") or {}
        return {int(k): v for k, v in raw.items()}

    # ----------------------------------------------------------------- cases
    def create_case(self, case: RecallCase) -> RecallCase:
        raw = self.call("create_case", case)
        return RecallCase.model_validate(raw) if raw else case

    def save_case(self, case: RecallCase) -> None:
        self.call("save_case", case)

    def get_case(self, case_id: str) -> Optional[RecallCase]:
        raw = self.call("get_case", case_id)
        return RecallCase.model_validate(raw) if raw else None

    def list_cases(self, status: Optional[CaseStatus] = None, include_drills: bool = True) -> list[RecallCase]:
        raw = self.call("list_cases", status, include_drills) or []
        return [RecallCase.model_validate(r) for r in raw]

    def find_case_by_source(self, url_or_recall_number: str) -> Optional[RecallCase]:
        raw = self.call("find_case_by_source", url_or_recall_number)
        return RecallCase.model_validate(raw) if raw else None

    # ------------------------------------------------------------- responses
    def issue_token(self, case_id: str, agency_id: str) -> str:
        return str(self.call("issue_token", case_id, agency_id))

    def resolve_token(self, token: str) -> Optional[tuple[str, str]]:
        raw = self.call("resolve_token", token)
        if not raw:
            return None
        return (raw[0], raw[1])

    def record_response(self, case_id: str, agency_id: str, status: ResponseStatus,
                        count: Optional[int] = None, free_text: str = "", token: str = "") -> None:
        self.call("record_response", case_id, agency_id, status, count, free_text, token)

    def responses(self, case_id: str) -> list[dict]:
        return list(self.call("responses", case_id) or [])

    def latest_response(self, case_id: str, agency_id: str) -> Optional[dict]:
        return self.call("latest_response", case_id, agency_id) or None

    # ------------------------------------------------------------- followups
    def schedule_followup(self, case_id: str, agency_id: str, due_at: datetime, kind: str) -> int:
        return int(self.call("schedule_followup", case_id, agency_id, due_at, kind))

    def due_followups(self, now: Optional[datetime] = None) -> list[dict]:
        return list(self.call("due_followups", now) or [])

    def followups(self, case_id: str) -> list[dict]:
        return list(self.call("followups", case_id) or [])

    def mark_followup(self, followup_id: int, sent_at: Optional[datetime] = None) -> None:
        self.call("mark_followup", followup_id, sent_at)

    def cancel_followups(self, case_id: str, agency_id: str) -> None:
        self.call("cancel_followups", case_id, agency_id)

    # ----------------------------------------------------------------- audit
    def audit(self, case_id: str, actor: str, kind: str, detail: str) -> None:
        self.call("audit", case_id, actor, kind, detail)

    def events(self, case_id: str) -> list[AuditEvent]:
        return [AuditEvent.model_validate(r) for r in self.call("events", case_id) or []]

    # ------------------------------------------------------------- decisions
    def remember(self, text: str, kind: str = "other") -> Decision:
        return Decision.model_validate(self.call("remember", text, kind))

    def decisions(self, query: str = "") -> list[Decision]:
        return [Decision.model_validate(r) for r in self.call("decisions", query) or []]

    # ----------------------------------------------------------------- clock
    def now(self) -> datetime:
        return datetime.fromisoformat(str(self.call("now")))

    # ---------------------------------------------------------------- outbox
    def record_mail(self, case_id: str, agency_id: Optional[str], to_addr: str, subject: str, body: str,
                    kind: str, token: str = "", backend: str = "mirror", provider_id: str = "") -> int:
        return int(self.call("record_mail", case_id, agency_id, to_addr, subject, body, kind, token,
                             backend, provider_id))

    def outbox(self, case_id: Optional[str] = None, agency_id: Optional[str] = None) -> list[dict]:
        return list(self.call("outbox", case_id, agency_id) or [])


__all__ = ["RemoteStore", "RemoteStoreError"]
