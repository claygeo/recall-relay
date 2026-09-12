"""Outbound mail. One protocol, one implemented backend.

The demo path is the in-app agency inbox mirror: `MirrorMailer` sends nothing and every message is
recorded in `store.outbox` anyway. That is the point -- the mirror IS the record, so the audit packet is
identical whether the bytes left the building or not. SES/Resend are wired as named backends that raise
a clear message rather than half-working.
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..core.config import settings
from ..core.store import Store


@runtime_checkable
class Mailer(Protocol):
    name: str

    def send(self, to: str, subject: str, body: str) -> str:
        """Deliver one message. Returns a provider id (opaque string)."""
        ...


class MirrorMailer:
    """No-op backend: the in-app inbox is the delivery surface."""

    name = "mirror"

    def send(self, to: str, subject: str, body: str) -> str:
        return "mirror"


class _Unimplemented:
    def __init__(self, name: str, hint: str):
        self.name = name
        self._hint = hint

    def send(self, to: str, subject: str, body: str) -> str:
        raise NotImplementedError(
            f"EMAIL_BACKEND={self.name!r} is not implemented in this build. {self._hint} "
            f"Set EMAIL_BACKEND=mirror to use the in-app agency inbox (the demo path)."
        )


def get_mailer(backend: Optional[str] = None) -> Mailer:
    """Resolve the configured mail backend."""
    name = (backend or settings.email_backend or "mirror").strip().lower()
    if name == "mirror":
        return MirrorMailer()
    if name == "ses":
        return _Unimplemented("ses", "Production SES was deliberately cut: sandbox verification only.")
    if name == "resend":
        return _Unimplemented("resend", "Needs RESEND_API_KEY and a verified sending domain.")
    raise ValueError(f"unknown EMAIL_BACKEND {name!r} (expected mirror|ses|resend)")


def send_and_record(
    store: Store,
    *,
    case_id: str,
    agency_id: Optional[str],
    to_addr: str,
    subject: str,
    body: str,
    kind: str,
    token: str = "",
    mailer: Optional[Mailer] = None,
) -> int:
    """Send through the configured backend and mirror the message into the outbox, always.

    Every send in the system goes through here so that `store.outbox` is a complete record whatever the
    backend is. Returns the outbox row id.
    """
    mailer = mailer or get_mailer()
    provider_id = mailer.send(to_addr, subject, body)
    return store.record_mail(
        case_id=case_id,
        agency_id=agency_id,
        to_addr=to_addr,
        subject=subject,
        body=body,
        kind=kind,
        token=token,
        backend=getattr(mailer, "name", "mirror"),
        provider_id=provider_id,
    )
