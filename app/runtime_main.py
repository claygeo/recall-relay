"""AgentCore Runtime entrypoint for Recall Relay.

Deployed with the AgentCore CLI (CodeZip build, no container). The Runtime is stateless: the dashboard
(FastAPI + SQLite) is the system of record, and this entrypoint reaches it through the same service
functions the dashboard uses, over REST when `AGENT_DATA_URL` is set (see recall_relay.core.remote_store)
or against a local SQLite file when it is not (useful for `agentcore dev`).

Payload contract (JSON):
  {"mode": "scan"}                          run the daily scan (pinned snapshot + live feed)
  {"mode": "intake", "url": "..."}          intake one FDA press-release URL
  {"mode": "intake", "text": "..."}         intake a pasted/forwarded notice
  {"mode": "approve", "case_id": "..."}     approve the one decision on a case and relay the notices
  {"mode": "followups"}                     send due reminders / escalations
  {"mode": "status"}                        health + counts
  {"prompt": "..."}                         free-form: the orchestrator answers questions about open cases
Responses stream as NDJSON events; the final event has {"type": "done", ...}.
"""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()
log = app.logger


def _store():
    from recall_relay.core.config import settings

    data_url = os.environ.get("AGENT_DATA_URL", "")
    if data_url:
        from recall_relay.core.remote_store import RemoteStore

        return RemoteStore(data_url, os.environ.get("AGENT_DATA_SECRET", ""))
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
    mode = payload.get("mode") or ("prompt" if payload.get("prompt") else "status")
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
    if mode == "prompt":
        answer = getattr(service, "answer", None)
        if answer is None:
            yield {"type": "error", "error": "free-form prompt mode is not enabled in this build"}
            return
        async for ev in answer(store, payload["prompt"]):
            yield ev
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
