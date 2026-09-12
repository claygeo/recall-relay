"""Store: the operational register (SQLite). Cases, verdicts, notices, responses, follow-ups, audit events,
coordinator decisions, and the demo clock. This is a database, not agent memory: AgentCore Memory holds
the coordinator's long-term decisions when deployed; the register always lives here.

Contract (implemented in this file):
  Store(db_path) -> creates schema if missing.
  Ledger:      list_agencies, get_agency, upsert_agency, list_receipts, get_receipt, upsert_receipt,
               list_distributions(receipt_id=None), add_distribution, ledger_brands, import_csv(kind, path)
  Holds:       set_hold(receipt_id, case_id, on), holds()
  Cases:       create_case, get_case, save_case, list_cases(status=None, include_drills=True),
               find_case_by_source(url_or_recall_number) -> RecallCase|None
  Responses:   record_response(case_id, agency_id, status, count, free_text, token) ; responses(case_id)
  Follow-ups:  schedule_followup(case_id, agency_id, due_at, kind) ; due_followups(now) ; mark_followup(id, sent_at)
  Tokens:      issue_token(case_id, agency_id) -> str ; resolve_token(token) -> (case_id, agency_id)|None
  Audit:       audit(case_id, actor, kind, detail) ; events(case_id)
  Decisions:   remember(text, kind) ; decisions(query='') -> list[Decision]
  Clock:       now() -> datetime (real time + demo offset) ; advance_clock(hours) ; reset_clock()
  Demo:        reset_demo() -> wipes cases/holds/responses/followups/events/clock, keeps ledger
All methods are synchronous; FastAPI calls them in threadpool. Timestamps are ISO-8601 UTC strings in SQLite.
"""
from __future__ import annotations

import csv
import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

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

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS distributions (id INTEGER PRIMARY KEY, receipt_id INTEGER NOT NULL, agency_id TEXT NOT NULL,
    shipped_at TEXT NOT NULL, cases INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS holds (receipt_id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, set_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, status TEXT NOT NULL, is_drill INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, source_url TEXT, recall_number TEXT, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS responses (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, agency_id TEXT NOT NULL,
    status TEXT NOT NULL, count INTEGER, free_text TEXT, token TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, agency_id TEXT NOT NULL,
    kind TEXT NOT NULL, due_at TEXT NOT NULL, sent_at TEXT);
CREATE TABLE IF NOT EXISTS tokens (token TEXT PRIMARY KEY, case_id TEXT NOT NULL, agency_id TEXT NOT NULL, issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, case_id TEXT NOT NULL,
    actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY CHECK (id = 1), offset_hours REAL NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO clock (id, offset_hours) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, agency_id TEXT,
    to_addr TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, token TEXT, sent_at TEXT NOT NULL,
    backend TEXT NOT NULL, provider_id TEXT);
"""


def _utc(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    # ----------------------------------------------------------------- clock
    def now(self) -> datetime:
        off = self._conn.execute("SELECT offset_hours FROM clock WHERE id=1").fetchone()[0]
        return datetime.now(timezone.utc) + timedelta(hours=off)

    def advance_clock(self, hours: float) -> datetime:
        self._conn.execute("UPDATE clock SET offset_hours = offset_hours + ? WHERE id=1", (hours,))
        self._conn.commit()
        return self.now()

    def reset_clock(self) -> None:
        self._conn.execute("UPDATE clock SET offset_hours = 0 WHERE id=1")
        self._conn.commit()

    # ---------------------------------------------------------------- ledger
    def upsert_agency(self, a: Agency) -> None:
        self._conn.execute("INSERT OR REPLACE INTO agencies (id, json) VALUES (?, ?)", (a.id, a.model_dump_json()))
        self._conn.commit()

    def list_agencies(self) -> list[Agency]:
        return [Agency.model_validate_json(r["json"]) for r in self._conn.execute("SELECT json FROM agencies ORDER BY id")]

    def get_agency(self, agency_id: str) -> Optional[Agency]:
        r = self._conn.execute("SELECT json FROM agencies WHERE id=?", (agency_id,)).fetchone()
        return Agency.model_validate_json(r["json"]) if r else None

    def upsert_receipt(self, rc: Receipt) -> None:
        self._conn.execute("INSERT OR REPLACE INTO receipts (id, json) VALUES (?, ?)", (rc.id, rc.model_dump_json()))
        self._conn.commit()

    def list_receipts(self) -> list[Receipt]:
        return [Receipt.model_validate_json(r["json"]) for r in self._conn.execute("SELECT json FROM receipts ORDER BY id")]

    def get_receipt(self, receipt_id: int) -> Optional[Receipt]:
        r = self._conn.execute("SELECT json FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return Receipt.model_validate_json(r["json"]) if r else None

    def ledger_brands(self) -> set[str]:
        out = set()
        for rc in self.list_receipts():
            if rc.brand:
                out.add(rc.brand)
        return out

    def add_distribution(self, d: Distribution) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO distributions (id, receipt_id, agency_id, shipped_at, cases) VALUES (?, ?, ?, ?, ?)",
            (d.id, d.receipt_id, d.agency_id, d.shipped_at.isoformat(), d.cases),
        )
        self._conn.commit()

    def list_distributions(self, receipt_id: Optional[int] = None) -> list[Distribution]:
        q = "SELECT * FROM distributions" + (" WHERE receipt_id=?" if receipt_id is not None else "") + " ORDER BY shipped_at, id"
        rows = self._conn.execute(q, (receipt_id,) if receipt_id is not None else ()).fetchall()
        return [Distribution(id=r["id"], receipt_id=r["receipt_id"], agency_id=r["agency_id"],
                             shipped_at=date.fromisoformat(r["shipped_at"]), cases=r["cases"]) for r in rows]

    def on_hand(self, receipt_id: int) -> int:
        rc = self.get_receipt(receipt_id)
        if not rc:
            return 0
        shipped = sum(d.cases for d in self.list_distributions(receipt_id))
        return max(rc.cases - shipped, 0)

    def import_csv(self, kind: str, path: Path | str) -> int:
        """kind in {'agencies','receipts','distributions'}; columns match the model fields. Returns rows imported."""
        n = 0
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row = {k: (v if v != "" else None) for k, v in row.items()}
                if kind == "agencies":
                    if row.get("languages"):
                        row["languages"] = [s.strip() for s in str(row["languages"]).split("|")]
                    row["same_day_distribution"] = str(row.get("same_day_distribution") or "").lower() in ("1", "true", "yes")
                    self.upsert_agency(Agency.model_validate({k: v for k, v in row.items() if v is not None}))
                elif kind == "receipts":
                    self.upsert_receipt(Receipt.model_validate({k: v for k, v in row.items() if v is not None}))
                elif kind == "distributions":
                    self.add_distribution(Distribution.model_validate({k: v for k, v in row.items() if v is not None}))
                else:
                    raise ValueError(kind)
                n += 1
        return n

    # ----------------------------------------------------------------- holds
    def set_hold(self, receipt_id: int, case_id: str, on: bool) -> None:
        if on:
            self._conn.execute("INSERT OR REPLACE INTO holds (receipt_id, case_id, set_at) VALUES (?, ?, ?)",
                               (receipt_id, case_id, _utc(self.now())))
        else:
            self._conn.execute("DELETE FROM holds WHERE receipt_id=?", (receipt_id,))
        self._conn.commit()

    def holds(self) -> dict[int, str]:
        return {r["receipt_id"]: r["case_id"] for r in self._conn.execute("SELECT receipt_id, case_id FROM holds")}

    # ----------------------------------------------------------------- cases
    def create_case(self, case: RecallCase) -> RecallCase:
        self._conn.execute(
            "INSERT INTO cases (id, status, is_drill, created_at, source_url, recall_number, json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (case.id, case.status.value, int(case.is_drill), _utc(case.created_at), case.notice.source_url,
             case.notice.recall_number, case.model_dump_json()),
        )
        self._conn.commit()
        return case

    def save_case(self, case: RecallCase) -> None:
        self._conn.execute(
            "UPDATE cases SET status=?, is_drill=?, source_url=?, recall_number=?, json=? WHERE id=?",
            (case.status.value, int(case.is_drill), case.notice.source_url, case.notice.recall_number,
             case.model_dump_json(), case.id),
        )
        self._conn.commit()

    def get_case(self, case_id: str) -> Optional[RecallCase]:
        r = self._conn.execute("SELECT json FROM cases WHERE id=?", (case_id,)).fetchone()
        return RecallCase.model_validate_json(r["json"]) if r else None

    def list_cases(self, status: Optional[CaseStatus] = None, include_drills: bool = True) -> list[RecallCase]:
        q, args = "SELECT json FROM cases", []
        conds = []
        if status is not None:
            conds.append("status=?"); args.append(status.value)
        if not include_drills:
            conds.append("is_drill=0")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC"
        return [RecallCase.model_validate_json(r["json"]) for r in self._conn.execute(q, args)]

    def find_case_by_source(self, url_or_recall_number: str) -> Optional[RecallCase]:
        """Rule 14: never re-ping. A case already created for this URL or recall number is returned as-is."""
        key = (url_or_recall_number or "").strip()
        if not key:
            return None
        r = self._conn.execute(
            "SELECT json FROM cases WHERE (source_url=? OR (recall_number<>'' AND recall_number=?)) AND is_drill=0 ORDER BY created_at DESC LIMIT 1",
            (key, key),
        ).fetchone()
        return RecallCase.model_validate_json(r["json"]) if r else None

    # ------------------------------------------------------------- responses
    def issue_token(self, case_id: str, agency_id: str) -> str:
        tok = secrets.token_urlsafe(18)
        self._conn.execute("INSERT INTO tokens (token, case_id, agency_id, issued_at) VALUES (?, ?, ?, ?)",
                           (tok, case_id, agency_id, _utc(self.now())))
        self._conn.commit()
        return tok

    def resolve_token(self, token: str) -> Optional[tuple[str, str]]:
        r = self._conn.execute("SELECT case_id, agency_id FROM tokens WHERE token=?", (token,)).fetchone()
        return (r["case_id"], r["agency_id"]) if r else None

    def record_response(self, case_id: str, agency_id: str, status: ResponseStatus, count: Optional[int] = None,
                        free_text: str = "", token: str = "") -> None:
        self._conn.execute(
            "INSERT INTO responses (case_id, agency_id, status, count, free_text, token, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (case_id, agency_id, status.value, count, free_text, token, _utc(self.now())),
        )
        self._conn.commit()

    def responses(self, case_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM responses WHERE case_id=? ORDER BY at", (case_id,))]

    def latest_response(self, case_id: str, agency_id: str) -> Optional[dict]:
        r = self._conn.execute("SELECT * FROM responses WHERE case_id=? AND agency_id=? ORDER BY at DESC LIMIT 1",
                               (case_id, agency_id)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------- followups
    def schedule_followup(self, case_id: str, agency_id: str, due_at: datetime, kind: str) -> int:
        cur = self._conn.execute("INSERT INTO followups (case_id, agency_id, kind, due_at) VALUES (?, ?, ?, ?)",
                                 (case_id, agency_id, kind, _utc(due_at)))
        self._conn.commit()
        return int(cur.lastrowid)

    def due_followups(self, now: Optional[datetime] = None) -> list[dict]:
        now = now or self.now()
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM followups WHERE sent_at IS NULL AND due_at <= ? ORDER BY due_at", (_utc(now),))]

    def followups(self, case_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM followups WHERE case_id=? ORDER BY due_at", (case_id,))]

    def mark_followup(self, followup_id: int, sent_at: Optional[datetime] = None) -> None:
        self._conn.execute("UPDATE followups SET sent_at=? WHERE id=?", (_utc(sent_at or self.now()), followup_id))
        self._conn.commit()

    def cancel_followups(self, case_id: str, agency_id: str) -> None:
        self._conn.execute("DELETE FROM followups WHERE case_id=? AND agency_id=? AND sent_at IS NULL", (case_id, agency_id))
        self._conn.commit()

    # ----------------------------------------------------------------- audit
    def audit(self, case_id: str, actor: str, kind: str, detail: str) -> None:
        self._conn.execute("INSERT INTO events (at, case_id, actor, kind, detail) VALUES (?, ?, ?, ?, ?)",
                           (_utc(self.now()), case_id, actor, kind, detail[:4000]))
        self._conn.commit()

    def events(self, case_id: str) -> list[AuditEvent]:
        return [AuditEvent(at=_parse(r["at"]), case_id=r["case_id"], actor=r["actor"], kind=r["kind"], detail=r["detail"])
                for r in self._conn.execute("SELECT * FROM events WHERE case_id=? ORDER BY id", (case_id,))]

    def recent_events(self, limit: int = 200) -> list[AuditEvent]:
        return [AuditEvent(at=_parse(r["at"]), case_id=r["case_id"], actor=r["actor"], kind=r["kind"], detail=r["detail"])
                for r in self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))][::-1]

    # ------------------------------------------------------------- decisions
    def remember(self, text: str, kind: str = "other") -> Decision:
        cur = self._conn.execute("INSERT INTO decisions (created_at, text, kind) VALUES (?, ?, ?)",
                                 (_utc(self.now()), text, kind))
        self._conn.commit()
        return Decision(id=int(cur.lastrowid), created_at=self.now(), text=text, kind=kind)  # type: ignore[arg-type]

    def decisions(self, query: str = "") -> list[Decision]:
        rows = self._conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
        out = [Decision(id=r["id"], created_at=_parse(r["created_at"]), text=r["text"], kind=r["kind"]) for r in rows]
        if query:
            q = query.lower()
            out = [d for d in out if any(tok in d.text.lower() for tok in q.split())]
        return out

    # ---------------------------------------------------------------- outbox
    def record_mail(self, case_id: str, agency_id: Optional[str], to_addr: str, subject: str, body: str,
                    kind: str, token: str = "", backend: str = "mirror", provider_id: str = "") -> int:
        """Every email the system sends is mirrored here, whatever the backend. kind: notice|reminder|escalation|client_sign|digest."""
        cur = self._conn.execute(
            "INSERT INTO outbox (case_id, agency_id, to_addr, subject, body, kind, token, sent_at, backend, provider_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, agency_id, to_addr, subject, body, kind, token, _utc(self.now()), backend, provider_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def outbox(self, case_id: Optional[str] = None, agency_id: Optional[str] = None) -> list[dict]:
        q, args, conds = "SELECT * FROM outbox", [], []
        if case_id:
            conds.append("case_id=?"); args.append(case_id)
        if agency_id:
            conds.append("agency_id=?"); args.append(agency_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    # ------------------------------------------------------------------ demo
    def reset_demo(self) -> None:
        for t in ("holds", "cases", "responses", "followups", "tokens", "events", "outbox"):
            self._conn.execute(f"DELETE FROM {t}")
        self._conn.execute("UPDATE clock SET offset_hours = 0 WHERE id=1")
        self._conn.commit()

    def wipe_all(self) -> None:
        for t in ("agencies", "receipts", "distributions"):
            self._conn.execute(f"DELETE FROM {t}")
        self._conn.commit()
        self.reset_demo()
