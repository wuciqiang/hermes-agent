from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime

import pytest

from gateway.backlink_progress import (
    build_live_progress,
    format_live_progress,
    progress_due,
    relay_live_progress,
)
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.run_notifications import GatewayNotificationsMixin
from gateway.config import GatewayConfig
from gateway.session import SessionEntry, SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


def test_live_progress_uses_manifest_facts_and_throttles(monkeypatch):
    monkeypatch.setattr(
        "gateway.backlink_progress.get_manifest_tool_facts",
        lambda _delegation_id: [{
            "backlinkhub_advance_submission_round": {
                "run_id": "run-1", "site_id": "site-1",
                "site_progress": {"published": 2, "pending_review": 1, "submission_unconfirmed": 1, "remaining": 4},
            }
        }],
    )
    monkeypatch.setattr(
        "gateway.backlink_progress.get_manifest_checkpoints",
        lambda _delegation_id: [{"stage": "browser_started", "at": "2026-10-11 14:00:00"}],
    )
    snapshot = build_live_progress(
        {"status": "running", "tool_profile": "backlinkhub", "delegation_id": "d1", "dispatched_at": 100},
        now=160,
    )
    assert snapshot["site_id"] == "site-1"
    assert snapshot["published"] == 2
    assert snapshot["pending"] == 1
    assert snapshot["attempted_unconfirmed"] == 1
    assert "待审核 1" in format_live_progress(snapshot)
    assert "未确认 1" in format_live_progress(snapshot)
    assert progress_due(None, 160, 300)
    assert not progress_due(100, 160, 300)
    assert progress_due(100, 400, 300)


@pytest.mark.asyncio
async def test_live_progress_preserves_message_id_for_feishu_reply(monkeypatch):
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_tool_facts", lambda _id: [{
        "backlinkhub_advance_submission_round": {
            "site_id": "site-1", "site_progress": {"published": 1, "remaining": 2},
        }
    }])
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_checkpoints", lambda _id: [])
    record = {
        "status": "running", "tool_profile": "backlinkhub", "delegation_id": "d-route",
        "session_key": "agent:main:feishu:dm:c", "parent_session_id": "session-original",
        "platform": "feishu", "chat_id": "chat-1", "chat_type": "dm",
        "thread_id": "thread-1", "message_id": "om-original", "scope_id": "scope-1",
        "user_id": "user-1", "user_name": "虾哥", "dispatched_at": 100,
    }
    snapshot = build_live_progress(record, now=160)
    assert snapshot["message_id"] == "om-original"
    assert snapshot["thread_id"] == "thread-1"
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat-1", thread_id="thread-1", message_id="om-original")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    route_runner = object.__new__(GatewayRunner)

    async def valid_target(_event):
        return True

    sent = await relay_live_progress(
        snapshot, validate_target=valid_target,
        build_source=lambda event: source if event["message_id"] == "om-original" else None,
        resolve_adapter=lambda *_args: adapter,
        thread_metadata=route_runner._thread_metadata_for_source,
        logger=__import__("logging").getLogger(__name__),
    )
    assert sent
    metadata = adapter.send.await_args.kwargs["metadata"]
    assert metadata["thread_id"] == "thread-1"
    assert metadata["reply_to_message_id"] == "om-original"
    assert metadata["_interim_send"] is True
    assert metadata["idempotency_key"].startswith("backlinkhub:live_progress:")


@pytest.mark.asyncio
async def test_live_progress_retries_without_thread_on_feishu_field_validation():
    snapshot = {"site_id": "site-1", "run_id": "run-1", "thread_id": "thread-1"}
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat-1", thread_id="thread-1")
    adapter = SimpleNamespace(send=AsyncMock(side_effect=[
        SimpleNamespace(success=False, error="[99992402] field validation failed"),
        SimpleNamespace(success=True),
    ]))
    route_runner = object.__new__(GatewayRunner)

    async def valid_target(_event):
        return True

    assert await relay_live_progress(
        snapshot, validate_target=valid_target,
        build_source=lambda _event: source,
        resolve_adapter=lambda *_args: adapter,
        thread_metadata=route_runner._thread_metadata_for_source,
        logger=__import__("logging").getLogger(__name__),
    )
    assert adapter.send.await_count == 2
    assert adapter.send.await_args_list[0].kwargs["metadata"]["thread_id"] == "thread-1"
    assert "thread_id" not in adapter.send.await_args_list[1].kwargs["metadata"]


@pytest.mark.asyncio
async def test_closed_origin_or_failed_send_does_not_consume_retry(monkeypatch):
    snapshot = {
        "delegation_id": "d1", "site_id": "site-1", "session_key": "agent:main:feishu:dm:c",
        "parent_session_id": "session-original",
        "elapsed_seconds": 300,
    }
    adapter = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("temporary")))
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat-1")
    async def valid_target(_event):
        return True
    assert not await relay_live_progress(
        snapshot,
        validate_target=valid_target,
        build_source=lambda _event: None,
        resolve_adapter=lambda *_args: adapter,
        thread_metadata=lambda _source: {},
        logger=__import__("logging").getLogger(__name__),
    )
    assert not await relay_live_progress(
        snapshot,
        validate_target=valid_target,
        build_source=lambda _event: source,
        resolve_adapter=lambda *_args: adapter,
        thread_metadata=lambda _source: {},
        logger=__import__("logging").getLogger(__name__),
    )
    assert adapter.send.await_count == 1
    assert progress_due(100, 500, 300)

    # The same path is driven by the idle watcher, with no foreground turn.
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat-1")
    adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    record = {
        "status": "running", "tool_profile": "backlinkhub", "delegation_id": "d1",
        "live_delegation_id": "d1", "session_key": "agent:main:feishu:dm:c",
        "dispatched_at": 100,
    }
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [record])
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_tool_facts", lambda _id: [{
        "backlinkhub_advance_submission_round": {
            "site_id": "site-1", "site_progress": {"published": 1, "remaining": 2}
        }
    }])
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_checkpoints", lambda _id: [{"stage": "advanced"}])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"delegation": {"profile_routes": {"backlinkhub": {"progress_interval_seconds": 300}}}})
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._backlinkhub_live_progress_sent = {}
    runner._build_process_event_source = lambda _event: source
    runner._resolve_injection_adapter = lambda *_args: adapter
    runner._thread_metadata_for_source = lambda _source: {}
    runner._validate_backlinkhub_progress_target = valid_target
    runner._drain_watch_notifications = AsyncMock()
    sleeps = 0

    async def bounded_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise StopAsyncIteration

    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", bounded_sleep)
    with pytest.raises(StopAsyncIteration):
        await runner._async_delegation_watcher(interval=0)
    adapter.send.assert_awaited_once()
    assert runner._backlinkhub_live_progress_sent["d1"] > 0


def _identity_runner(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("session-original", source="feishu")
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = db
    source = SessionSource(platform=Platform.FEISHU, chat_id="chat", user_id="user")
    entry = SessionEntry(
        session_key="agent:main:feishu:dm:chat:user", session_id="session-original",
        created_at=datetime.now(), updated_at=datetime.now(), origin=source,
        platform=Platform.FEISHU,
    )
    with store._lock:
        store._entries[entry.session_key] = entry
        store._loaded = True
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_db = AsyncSessionDB(db)
    return runner, db, entry.session_key, source


@pytest.mark.asyncio
async def test_reset_boundary_blocks_live_progress_before_route_lookup(tmp_path):
    runner, db, session_key, _source = _identity_runner(tmp_path)
    db.end_session("session-original", "session_reset")
    db.create_session("session-new", source="feishu")
    with runner.session_store._lock:
        runner.session_store._entries[session_key] = SessionEntry(
            session_key=session_key, session_id="session-new", created_at=datetime.now(),
            updated_at=datetime.now(), origin=SessionSource(platform=Platform.FEISHU, chat_id="chat"),
            platform=Platform.FEISHU,
        )
    send = AsyncMock()
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat")
    snapshot = {
        "delegation_id": "d-reset", "site_id": "site-1", "session_key": session_key,
        "parent_session_id": "session-original",
    }
    assert not await relay_live_progress(
        snapshot,
        validate_target=runner._validate_backlinkhub_progress_target,
        build_source=lambda _event: source,
        resolve_adapter=lambda *_args: SimpleNamespace(send=send),
        thread_metadata=lambda _source: {},
        logger=__import__("logging").getLogger(__name__),
    )
    send.assert_not_awaited()
    db.close()


@pytest.mark.asyncio
async def test_failed_live_progress_retries_and_succeeds_after_backoff(tmp_path):
    runner, db, session_key, _source = _identity_runner(tmp_path)
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat")
    adapter = SimpleNamespace(send=AsyncMock(side_effect=[RuntimeError("temporary"), SimpleNamespace(success=True)]))
    snapshot = {
        "delegation_id": "d-retry", "site_id": "site-1", "session_key": session_key,
        "parent_session_id": "session-original",
    }
    assert not await relay_live_progress(
        snapshot, validate_target=runner._validate_backlinkhub_progress_target,
        build_source=lambda _event: source, resolve_adapter=lambda *_args: adapter,
        thread_metadata=lambda _source: {}, logger=__import__("logging").getLogger(__name__),
    )
    assert await relay_live_progress(
        snapshot, validate_target=runner._validate_backlinkhub_progress_target,
        build_source=lambda _event: source, resolve_adapter=lambda *_args: adapter,
        thread_metadata=lambda _source: {}, logger=__import__("logging").getLogger(__name__),
    )
    assert adapter.send.await_count == 2
    db.close()


@pytest.mark.asyncio
async def test_watcher_does_not_retry_failed_send_every_tick(monkeypatch):
    source = SimpleNamespace(platform=Platform.FEISHU, chat_id="chat-1")
    adapter = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("temporary")))
    record = {
        "status": "running", "tool_profile": "backlinkhub", "delegation_id": "d-throttle",
        "live_delegation_id": "d-throttle", "session_key": "agent:main:feishu:dm:c",
        "parent_session_id": "session-original", "dispatched_at": 100,
    }
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [record])
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_tool_facts", lambda _id: [{
        "backlinkhub_advance_submission_round": {
            "site_id": "site-1", "site_progress": {"published": 1, "remaining": 2}
        }
    }])
    monkeypatch.setattr("gateway.backlink_progress.get_manifest_checkpoints", lambda _id: [])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"delegation": {"profile_routes": {"backlinkhub": {"progress_interval_seconds": 300}}}})
    monkeypatch.setattr("gateway.run_notifications.time.time", lambda: 1000)
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._backlinkhub_live_progress_sent = {}
    runner._backlinkhub_live_progress_attempted = {}
    runner._build_process_event_source = lambda _event: source
    runner._resolve_injection_adapter = lambda *_args: adapter
    runner._thread_metadata_for_source = lambda _source: {}
    async def always_valid(_event):
        return True

    runner._validate_backlinkhub_progress_target = always_valid
    runner._drain_watch_notifications = AsyncMock()
    sleeps = 0

    async def bounded_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2:
            raise StopAsyncIteration

    monkeypatch.setattr("gateway.run_notifications.asyncio.sleep", bounded_sleep)
    with pytest.raises(StopAsyncIteration):
        await runner._async_delegation_watcher(interval=0)
    adapter.send.assert_awaited_once()


@pytest.mark.parametrize("reason, expected", [
    ("user_control_or_permission_prompt", "浏览器权限或用户接管"),
    ("site_safety_pause", "站点安全预算已暂停"),
])
def test_pause_reasons_are_paused_and_keep_remaining(reason, expected):
    text = GatewayNotificationsMixin._backlinkhub_progress_text(
        {"stop_reason": reason}, {"site_id": "site-1", "remaining": 7}
    )
    assert "状态：已暂停" in text
    assert "剩余：7" in text
    assert expected in text
