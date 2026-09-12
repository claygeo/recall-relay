"""Strands hooks: the invariants that do not depend on the model behaving.

Three providers, all registered on the orchestrator:

* `AuditHook`      -- every tool call and every tool result lands in the case's audit trail. Nothing the
                      agent does is off the record.
* `ApprovalGuard`  -- rule 12. `send_notices` is cancelled unless the case carries `approved_at`, and any
                      send is cancelled outright on a drill (rule 14). A prompt can be talked out of this;
                      a hook cannot.
* `TerminalToolGuard` -- rule 12 again, from the other side: once the agent has asked for the human
                      decision (or dismissed, or escalated), the run ends. The model is not given a chance
                      to keep going and ping twice.

Cancellation mechanism (verified against strands 1.55.1 `strands/hooks/events.py`): `BeforeToolCallEvent`
declares `_can_write` over `cancel_tool`, `selected_tool` and `tool_use`; the documented cancel is
`event.cancel_tool = "<message>"`, which the tool executor turns into an error ToolResult carrying that
message (`strands/tools/executors/_executor.py`). Setting `selected_tool = None` is NOT the cancel path --
it is the "tool lookup failed" path -- so `cancel_tool` is what this module uses.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from strands.hooks import (
    AfterToolCallEvent,
    AfterToolsEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from ..core.store import Store

# Tools that put bytes in front of a partner agency. Guarded.
SEND_TOOLS = frozenset({"send_notices"})

# Tools that end the run (rule 12: one human decision per case).
TERMINAL_TOOLS = frozenset({"request_approval", "dismiss_case", "mark_needs_human"})

# The structured-output forcing tool strands injects; not worth an audit row.
_NOISE_PREFIXES = ("StructuredOutput", "structured_output")


def _case_id_from(event: Any, fallback: str = "") -> str:
    state = getattr(event, "invocation_state", None) or {}
    cid = state.get("case_id") or ""
    if not cid:
        tool_use = getattr(event, "tool_use", None) or {}
        cid = (tool_use.get("input") or {}).get("case_id") or ""
    return cid or fallback


def _is_noise(name: str) -> bool:
    return any(name.startswith(p) for p in _NOISE_PREFIXES)


def _summarize(value: Any, limit: int = 600) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - defensive
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class AuditHook(HookProvider):
    """Write one audit row before each tool call and one after its result."""

    def __init__(self, store: Store, case_id: str = ""):
        self.store = store
        self.case_id = case_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        name = (event.tool_use or {}).get("name", "?")
        if _is_noise(name):
            return
        case_id = _case_id_from(event, self.case_id)
        args = _summarize((event.tool_use or {}).get("input", {}))
        self.store.audit(case_id, "agent", "tool_call", f"{name} {args}")

    def after_tool(self, event: AfterToolCallEvent) -> None:
        name = (event.tool_use or {}).get("name", "?")
        if _is_noise(name):
            return
        case_id = _case_id_from(event, self.case_id)
        result = event.result or {}
        status = result.get("status", "?")
        blocks = result.get("content", []) or []
        parts: list[str] = []
        for block in blocks:
            if "text" in block:
                parts.append(str(block["text"]))
            elif "json" in block:
                parts.append(_summarize(block["json"]))
        detail = f"{name} -> {status}: {_summarize(' '.join(parts))}"
        self.store.audit(case_id, "agent", "tool_result", detail)


class ApprovalGuard(HookProvider):
    """Rule 12 / rule 14, enforced in code.

    No notice leaves the building until a human approved the case, and a drill never mails anyone.
    """

    def __init__(self, store: Store, case_id: str = ""):
        self.store = store
        self.case_id = case_id

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.guard)

    def guard(self, event: BeforeToolCallEvent) -> None:
        name = (event.tool_use or {}).get("name", "")
        if name not in SEND_TOOLS:
            return
        case_id = _case_id_from(event, self.case_id)
        case = self.store.get_case(case_id) if case_id else None

        if case is None:
            reason = f"blocked {name}: no case in scope, so approval cannot be verified"
            event.cancel_tool = reason
            self.store.audit(case_id, "system", "send_blocked", reason)
            return

        if case.is_drill:
            reason = f"blocked {name}: case {case.id} is a drill; drills never mail an agency (rule 14)"
            event.cancel_tool = reason
            self.store.audit(case.id, "system", "send_blocked", reason)
            return

        if case.approved_at is None:
            reason = (
                f"blocked {name}: case {case.id} has no approved_at. A human approves before anything is "
                f"relayed (rule 12). Call request_approval and stop."
            )
            event.cancel_tool = reason
            self.store.audit(case.id, "system", "send_blocked", reason)
            return


class TerminalToolGuard(HookProvider):
    """Rule 12: after request_approval / dismiss_case / mark_needs_human the run is over.

    The system prompt says so too, but this makes it true regardless of what the model wants next.
    """

    def __init__(self, store: Optional[Store] = None, case_id: str = ""):
        self.store = store
        self.case_id = case_id
        self.terminal_reason: str = ""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.reset)
        registry.add_callback(AfterToolCallEvent, self.note)
        registry.add_callback(AfterToolsEvent, self.stop)

    def reset(self, event: BeforeInvocationEvent) -> None:
        self.terminal_reason = ""

    def note(self, event: AfterToolCallEvent) -> None:
        name = (event.tool_use or {}).get("name", "")
        if name in TERMINAL_TOOLS:
            self.terminal_reason = name

    def stop(self, event: AfterToolsEvent) -> None:
        if not self.terminal_reason:
            return
        event.end_turn = (
            f"Run ended after {self.terminal_reason} (rule 12: one human decision per case)."
        )
        if self.store is not None:
            case_id = _case_id_from(event, self.case_id)
            self.store.audit(case_id, "system", "run_ended", f"terminal tool {self.terminal_reason}")
