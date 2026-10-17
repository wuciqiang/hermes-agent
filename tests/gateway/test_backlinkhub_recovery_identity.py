"""Durable identity contracts for cold BacklinkHub recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


def _runner(tmp_path, parent_id="session-original"):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(parent_id, source="feishu")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = db
    now = datetime.now()
    source = SessionSource(platform=Platform.FEISHU, chat_id="chat", user_id="user")
    entry = SessionEntry(
        session_key="agent:main:feishu:dm:chat:user", session_id=parent_id,
        created_at=now, updated_at=now, origin=source, platform=Platform.FEISHU,
    )
    with store._lock:
        store._entries[entry.session_key] = entry
        store._loaded = True
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_db = AsyncSessionDB(db)
    return runner, db, entry.session_key, source


def test_cold_recovery_builder_uses_opaque_session_id_and_async_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, db, session_key, source = _runner(tmp_path)
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs["session_id"]

    runner._resolve_session_agent_runtime = lambda **_: ("model", {"api_key": "test"})
    runner._resolve_turn_toolsets = lambda *_args, **_kwargs: ([], [])
    runner._resolve_session_reasoning_config = lambda **_: None
    runner._resolve_session_service_tier = lambda **_: None
    with (
        patch("run_agent.AIAgent", FakeAgent),
        patch("gateway.run._checkpoint_agent_kwargs", return_value={}),
        patch("gateway.run._current_max_iterations", return_value=3),
        patch("gateway.run._load_gateway_config", return_value={}),
        patch("gateway.run._platform_config_key", return_value="feishu"),
    ):
        parent = runner._build_recovery_parent_agent(source, session_key, "session-original")

    assert parent.session_id == "session-original"
    assert captured["session_id"] == "session-original"
    assert asyncio.run(
        runner._resolve_recovery_parent_session_id(
            {"parent_session_id": "session-original"}, session_key,
        )
    ) == ("deliver", "session-original")
    assert asyncio.run(runner._classify_completion_target("session-original")) == "deliver"

    from tools import async_delegation as ad

    finished = threading.Event()
    dispatched = ad.dispatch_async_delegation(
        goal="recovery", context=None, toolsets=[], role="leaf", model="model",
        session_key=session_key, parent_session_id=parent.session_id,
        runner=lambda: (finished.set() or {"status": "completed"}), max_async_children=1,
    )
    assert dispatched["status"] == "dispatched"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(tmp_path / "state.db") as conn:
            state = conn.execute(
                "SELECT state FROM async_delegations WHERE delegation_id=?",
                (dispatched["delegation_id"],),
            ).fetchone()
        if state == ("completed",):
            break
        time.sleep(0.01)
    assert state == ("completed",)
    assert finished.wait(5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(tmp_path / "state.db") as conn:
            state = conn.execute(
                "SELECT state FROM async_delegations WHERE delegation_id=?",
                (dispatched["delegation_id"],),
            ).fetchone()
        if state == ("completed",):
            break
        time.sleep(0.01)
    assert state == ("completed",)
    with sqlite3.connect(tmp_path / "state.db") as conn:
        row = conn.execute(
            "SELECT parent_session_id FROM async_delegations WHERE delegation_id=?",
            (dispatched["delegation_id"],),
        ).fetchone()
    assert row == ("session-original",)
    db.close()


def test_recovery_identity_retries_db_uncertainty_and_rejects_route_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, db, session_key, source = _runner(tmp_path)
    event = {"parent_session_id": "session-original"}

    class BrokenDB:
        async def get_session(self, _session_id):
            raise OSError("temporary state.db outage")

    runner._session_db = BrokenDB()
    state, identity = asyncio.run(runner._resolve_recovery_parent_session_id(event, session_key))
    assert (state, identity) == ("retry", None)

    state, identity = asyncio.run(
        runner._resolve_recovery_parent_session_id(
            {"parent_session_id": session_key}, session_key,
        )
    )
    assert (state, identity) == ("terminal", None)
    runner._session_db = AsyncSessionDB(db)

    # A /new boundary closes the original opaque session and repoints the route
    # key at a newer session. The old recovery marker must be terminal and must
    # not be dispatched into the newer route owner.
    db.end_session("session-original", "session_reset")
    db.create_session("session-new", source="feishu")
    newer = SessionEntry(
        session_key=session_key, session_id="session-new", created_at=datetime.now(),
        updated_at=datetime.now(), origin=source, platform=Platform.FEISHU,
    )
    with runner.session_store._lock:
        runner.session_store._entries[session_key] = newer
    state, identity = asyncio.run(
        runner._resolve_recovery_parent_session_id(
            {"parent_session_id": "session-original"}, session_key,
        )
    )
    assert (state, identity) == ("terminal", None)

    from tools import async_delegation as ad

    finished = threading.Event()
    dispatched = ad.dispatch_async_delegation(
        goal="recovery", context=None, toolsets=[], role="leaf", model="model",
        session_key=session_key, parent_session_id="session-original",
        runner=lambda: (finished.set() or {"status": "completed"}), max_async_children=1,
    )
    assert dispatched["status"] == "dispatched"
    assert finished.wait(5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(tmp_path / "state.db") as conn:
            state = conn.execute(
                "SELECT state FROM async_delegations WHERE delegation_id=?",
                (dispatched["delegation_id"],),
            ).fetchone()
        if state == ("completed",):
            break
        time.sleep(0.01)
    assert state == ("completed",)
    marker = {
        "recovery_continuation": {"checkpoint": {"stage": "browser_started"}},
        "recovery_task": {"goal": "continue", "role": "leaf"},
    }
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute(
            "UPDATE async_delegations SET event_json=? WHERE delegation_id=?",
            (json.dumps(marker), dispatched["delegation_id"]),
        )
    event = {
            **marker,
            "type": "async_delegation",
            "delegation_id": dispatched["delegation_id"],
            "session_key": session_key,
            "parent_session_id": "session-original",
        }
    with patch("tools.delegate_tool.delegate_task") as delegate_task:
        assert asyncio.run(runner._dispatch_recovery_continuation(event)) is True
        with sqlite3.connect(tmp_path / "state.db") as conn:
            assert conn.execute(
                "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
                (dispatched["delegation_id"],),
            ).fetchone() == ("dropped",)
        assert asyncio.run(runner._dispatch_recovery_continuation(event)) is True
        delegate_task.assert_not_called()
    db.close()


def test_idle_ended_opaque_parent_retargets_current_route(tmp_path):
    runner, db, session_key, source = _runner(tmp_path)
    db.end_session("session-original", "idle_timeout")
    db.create_session("session-new", source="feishu")
    newer = SessionEntry(
        session_key=session_key, session_id="session-new", created_at=datetime.now(),
        updated_at=datetime.now(), origin=source, platform=Platform.FEISHU,
    )
    with runner.session_store._lock:
        runner.session_store._entries[session_key] = newer

    state, identity = asyncio.run(
        runner._resolve_recovery_parent_session_id(
            {"parent_session_id": "session-original"}, session_key,
        )
    )
    assert (state, identity) == ("deliver", "session-new")
    assert asyncio.run(runner._classify_completion_target("session-original")) == "deliver"
    db.close()
