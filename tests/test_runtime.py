"""The AgentCore Runtime entrypoint, offline.

`app/runtime_main.py` is the deployed door. Two things about it are load-bearing and were previously
only claimed in prose: `AGENT_BACKEND` actually selects the store, and the mode set is closed. Both are
asserted here without a model call, a network call, or a deployed Runtime.

Windows note (see tests/conftest.py): never a POSIX temp path; tmp_path only.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest
from recall_relay.core import config as config_module
from recall_relay.core.remote_store import RemoteStore
from recall_relay.core.store import Store

import app.runtime_main as runtime_main


@pytest.fixture
def runtime_settings(monkeypatch, tmp_path):
    """The settings singleton `_store()` reads, pointed at this test's own database file."""
    settings = config_module.settings
    monkeypatch.setattr(settings, "db_path", tmp_path / "runtime_test.db")
    monkeypatch.setattr(settings, "agent_backend", "inprocess")
    monkeypatch.setattr(settings, "agent_data_url", "")
    monkeypatch.setattr(settings, "data_secret", "")
    return settings


# ---------------------------------------------------------------------------
# AGENT_BACKEND picks the transport
# ---------------------------------------------------------------------------
def test_inprocess_backend_opens_a_local_sqlite_store(runtime_settings):
    store = runtime_main._store()
    assert isinstance(store, Store)
    assert store.list_cases() == []


def test_backend_defaults_to_inprocess_when_unset(runtime_settings, monkeypatch):
    monkeypatch.setattr(runtime_settings, "agent_backend", "")
    assert isinstance(runtime_main._store(), Store)


def test_remote_backend_returns_a_remote_store_pointed_at_the_dashboard(runtime_settings, monkeypatch):
    monkeypatch.setattr(runtime_settings, "agent_backend", "remote")
    monkeypatch.setattr(runtime_settings, "agent_data_url", "https://relay.example.test/")
    monkeypatch.setattr(runtime_settings, "data_secret", "shared-secret")

    store = runtime_main._store()
    try:
        assert isinstance(store, RemoteStore)
        assert store.base_url == "https://relay.example.test"
        assert store.secret == "shared-secret"
    finally:
        store.close()


def test_remote_backend_without_a_url_names_the_missing_variables(runtime_settings, monkeypatch):
    monkeypatch.setattr(runtime_settings, "agent_backend", "remote")
    monkeypatch.setattr(runtime_settings, "data_secret", "shared-secret")

    with pytest.raises(RuntimeError) as caught:
        runtime_main._store()
    message = str(caught.value)
    assert "AGENT_DATA_URL" in message
    assert "AGENT_DATA_SECRET" in message


# ---------------------------------------------------------------------------
# the mode set is closed: no free-form prompt door
# ---------------------------------------------------------------------------
def _dispatch(payload: dict) -> list[dict]:
    async def collect() -> list[dict]:
        return [event async for event in runtime_main._dispatch(payload)]

    return asyncio.run(collect())


def test_status_is_the_default_mode(runtime_settings):
    events = _dispatch({})
    assert events[-1]["type"] == "done"
    assert events[-1]["ok"] is True
    assert events[-1]["cases"] == 0


def test_prompt_mode_is_gone_and_reports_the_valid_modes(runtime_settings):
    events = _dispatch({"mode": "prompt", "prompt": "what is open?"})
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "error"
    assert "unknown mode" in event["error"]
    for mode in ("scan", "intake", "approve", "followups", "status"):
        assert mode in event["error"]


def test_an_unknown_mode_is_an_error_event_not_a_silent_status(runtime_settings):
    events = _dispatch({"mode": "delete_everything"})
    assert events == [
        {
            "type": "error",
            "error": "unknown mode 'delete_everything'; valid modes are scan, intake, approve, "
                     "followups, status",
        }
    ]


def test_the_declared_mode_set_matches_the_dispatcher_branches():
    """MODES is what the error event advertises, so it has to be what the dispatcher actually branches
    on. A re-added free-form prompt door fails here before anyone deploys it."""
    source = inspect.getsource(runtime_main._dispatch)
    branched = set(re.findall(r'mode == "([a-z_]+)"', source))
    assert branched | {"status"} == set(runtime_main.MODES)
    assert "prompt" not in source
