"""Guards for a public demo: signed response links, in-memory rate limits, a daily LLM budget.

There is no login (DECISIONS #12). Reading is open; the two routes that cost money -- the scan and the
manual intake -- are rate limited per process and capped per day, and the reset control is throttled so a
judge cannot wipe the demo in a loop. Nothing here is a substitute for auth on a real deployment; it is
the smallest honest set of brakes for a hackathon demo that anyone can click.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer

from ..core.config import settings

# ---------------------------------------------------------------------------
# signed response links
# ---------------------------------------------------------------------------
_SALT = "recall-relay/response-token"


def _serializer() -> URLSafeSerializer:
    # built per call so a test that swaps settings.secret_key is honoured
    return URLSafeSerializer(settings.secret_key or "dev-only-change-me", salt=_SALT)


def sign_token(token: str) -> str:
    """Wrap a store response token in a signature, for the links the inbox mirror renders."""
    return _serializer().dumps(token)


def unsign_token(value: str) -> Optional[str]:
    """Return the inner token, or None when the value is not a valid signature of one."""
    try:
        out = _serializer().loads(value)
    except BadSignature:
        return None
    return out if isinstance(out, str) else None


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------
class LimitExceeded(Exception):
    """Raised by a guard. The route turns it into a 429 sheet."""

    def __init__(self, message: str, *, headline: str = "Slow down", retry_after: int = 60):
        super().__init__(message)
        self.message = message
        self.headline = headline
        self.retry_after = retry_after


@dataclass
class _Bucket:
    limit: int
    window_s: float
    hits: deque[float] = field(default_factory=deque)


class RateLimiter:
    """Fixed-window-ish sliding limiter. Per process, in memory, deliberately simple."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def configure(self, name: str, limit: int, window_s: float) -> None:
        with self._lock:
            self._buckets[name] = _Bucket(limit=limit, window_s=window_s)

    def check(self, name: str, *, message: str, headline: str = "Slow down") -> None:
        with self._lock:
            bucket = self._buckets.get(name)
            if bucket is None:
                return
            now = time.monotonic()
            while bucket.hits and now - bucket.hits[0] > bucket.window_s:
                bucket.hits.popleft()
            if len(bucket.hits) >= bucket.limit:
                wait = int(bucket.window_s - (now - bucket.hits[0])) + 1
                raise LimitExceeded(message, headline=headline, retry_after=max(wait, 1))
            bucket.hits.append(now)

    def reset(self) -> None:
        with self._lock:
            for bucket in self._buckets.values():
                bucket.hits.clear()


class DailyBudget:
    """The LLM spend guard. One counter for every agent run the dashboard starts."""

    def __init__(self, env_var: str = "MAX_AGENT_RUNS_PER_DAY", default: int = 60):
        self.env_var = env_var
        self.default = default
        self._day = ""
        self._used = 0
        self._lock = threading.Lock()

    def limit(self) -> int:
        raw = os.environ.get(self.env_var, "")
        try:
            return int(raw) if raw.strip() else self.default
        except ValueError:
            return self.default

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day:
            self._day = today
            self._used = 0

    def state(self) -> tuple[int, int]:
        """(used, limit) for today."""
        with self._lock:
            self._roll()
            return self._used, self.limit()

    def spend(self, n: int = 1) -> None:
        with self._lock:
            self._roll()
            limit = self.limit()
            if self._used + n > limit:
                raise LimitExceeded(
                    f"The daily agent budget for this demo is {limit} runs and it is spent. "
                    "The ledger, the cases, the inbox and the audit packet are all still readable, "
                    "and the budget resets at midnight UTC.",
                    headline="Daily agent budget spent",
                    retry_after=3600,
                )
            self._used += n

    def refund(self, n: int = 1) -> None:
        with self._lock:
            self._used = max(0, self._used - n)

    def reset(self) -> None:
        with self._lock:
            self._day = ""
            self._used = 0


class SingleFlight:
    """One scan at a time. Holds the job id of the run in flight."""

    def __init__(self, what: str = "scan"):
        self.what = what
        self._lock = threading.Lock()
        self._active: Optional[str] = None

    @property
    def active(self) -> Optional[str]:
        return self._active

    def acquire(self, job_id: str) -> None:
        with self._lock:
            if self._active is not None:
                raise LimitExceeded(
                    f"A {self.what} is already running (job {self._active}). Watch that one finish, "
                    "or reload the run page to reattach to its log.",
                    headline=f"A {self.what} is already running",
                    retry_after=10,
                )
            self._active = job_id

    def release(self, job_id: str) -> None:
        with self._lock:
            if self._active == job_id:
                self._active = None

    def reset(self) -> None:
        with self._lock:
            self._active = None


# ---------------------------------------------------------------------------
# the process-wide guards the routes use
# ---------------------------------------------------------------------------
limiter = RateLimiter()
limiter.configure("scan", limit=6, window_s=3600)
limiter.configure("intake", limit=12, window_s=3600)
limiter.configure("reset", limit=1, window_s=60)

budget = DailyBudget()
scan_flight = SingleFlight("scan")


def reset_all_guards() -> None:
    """Test helper: clear every in-memory limit."""
    limiter.reset()
    budget.reset()
    scan_flight.reset()


def data_secret() -> str:
    """The shared secret the AgentCore Runtime presents on /api/data/rpc.

    Read from the environment (AGENT_DATA_SECRET) with a settings attribute as the override hook, so the
    web layer does not have to edit core/config.py to exist. Unset means the endpoint is closed.
    """
    return (getattr(settings, "data_secret", "") or os.environ.get("AGENT_DATA_SECRET", "")).strip()
