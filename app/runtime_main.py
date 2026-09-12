"""AgentCore Runtime entrypoint for Recall Relay.

Deployed with the AgentCore CLI (CodeZip build, no container). The Runtime is stateless: the dashboard
(FastAPI + SQLite) is the system of record, and this entrypoint reaches it through the same service
functions the dashboard uses. `AGENT_BACKEND` picks the transport: `remote` sends every store call to the
dashboard's allow-listed REST endpoint (`AGENT_DATA_URL` plus `AGENT_DATA_SECRET`, see
recall_relay.core.remote_store), `inprocess` (the default) opens the local SQLite file, which is what
`agentcore dev` wants.

Payload contract (JSON):
  {"mode": "scan"}                          run the daily scan (pinned snapshot + live feed)
  {"mode": "intake", "url": "..."}          intake one FDA press-release URL
  {"mode": "intake", "text": "..."}         intake a pasted/forwarded notice
  {"mode": "approve", "case_id": "..."}     approve the one decision on a case and relay the notices
  {"mode": "followups"}                     send due reminders / escalations
  {"mode": "status"}                        health + counts (the default when no mode is given)
Responses stream as NDJSON events; the final event has {"type": "done", ...}. There is no free-form
prompt door: every mode is one named procedure, so a judge can read what the Runtime is allowed to do.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
log = app.logger


MODES = ("scan", "intake", "approve", "followups", "status")


def _store():
    """The rows this invocation reads and writes, chosen by `AGENT_BACKEND` (DECISIONS #5).

    `remote` is a transport switch, not a storage switch: the register is the same rows either way.
    """
    from recall_relay.core.config import settings

    backend = (settings.agent_backend or "inprocess").strip().lower()
    if backend == "remote":
        if not settings.agent_data_url:
            raise RuntimeError(
                "AGENT_BACKEND=remote needs AGENT_DATA_URL (the dashboard base URL this Runtime calls "
                "back into) and AGENT_DATA_SECRET (the shared secret /api/data/rpc checks). Set both, or "
                "set AGENT_BACKEND=inprocess to work against a local SQLite file."
            )
        from recall_relay.core.remote_store import RemoteStore

        return RemoteStore(settings.agent_data_url, settings.data_secret)
    from recall_relay.core.store import Store

    return Store(settings.db_path)


async def _call(fn, *args, **kwargs):
    """The service façade mixes sync and async functions; run sync ones off the event loop."""
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _dispatch(payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    from recall_relay.agents import service

    store = _store()
    mode = str(payload.get("mode") or "status")
    log.info("recall-relay mode=%s", mode)

    if mode == "scan":
        async for ev in service.scan(store, live=bool(payload.get("live", True)), snapshot=bool(payload.get("snapshot", True))):
            yield ev
        return
    if mode == "intake":
        if payload.get("url"):
            case = await _call(service.intake_url, store, payload["url"])
        else:
            case = await _call(service.intake_text, store, payload.get("text", ""))
        yield {"type": "done", "case_id": case.id, "status": case.status.value, "ping": service.ping_text(case, store)}
        return
    if mode == "approve":
        case = await _call(service.approve, store, payload["case_id"])
        yield {"type": "done", "case_id": case.id, "status": case.status.value, "notices_sent": len(case.notices)}
        return
    if mode == "followups":
        result = await _call(service.run_followups, store)
        if isinstance(result, dict):
            yield {"type": "done", **result}
        else:
            items = list(result or [])
            yield {"type": "done", "sent": len(items), "items": items}
        return
    if mode != "status":
        yield {"type": "error", "error": f"unknown mode {mode!r}; valid modes are {', '.join(MODES)}"}
        return
    # status
    cases = store.list_cases()
    yield {"type": "done", "ok": True, "cases": len(cases), "agencies": len(store.list_agencies()),
           "receipts": len(store.list_receipts())}


@app.entrypoint
async def invoke(payload: dict[str, Any], context):
    try:
        async for ev in _dispatch(payload or {}):
            yield json.dumps(ev, default=str) + "\n"
    except Exception as exc:  # the runtime must never crash mid-stream; report and end
        log.exception("recall-relay failed")
        yield json.dumps({"type": "error", "error": f"{type(exc).__name__}: {exc}"}) + "\n"


if __name__ == "__main__":
    app.run()
