"""Zulip stream topic rename on session title change."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, SessionStore, build_session_key


def _zulip_platform():
    """Build a Platform-like member for 'zulip' even when the plugin isn't installed."""
    try:
        plat = Platform("zulip")
        if plat is not None:
            return plat
    except ValueError:
        pass
    # Mirror Platform._missing_ pseudo-member creation for tests.
    value = "zulip"
    if value in Platform._value2member_map_:
        return Platform._value2member_map_[value]
    pseudo = object.__new__(Platform)
    pseudo._value_ = value
    pseudo._name_ = "ZULIP"
    Platform._value2member_map_[value] = pseudo
    Platform._member_map_["ZULIP"] = pseudo
    return pseudo


def _make_source(
    *,
    thread_id: str = "old-topic",
    chat_id: str = "12345",
    chat_type: str = "thread",
    user_id: str = "user@example.com",
) -> SessionSource:
    return SessionSource(
        platform=_zulip_platform(),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        user_name="User",
        thread_id=thread_id,
        chat_topic=thread_id,
    )


def _make_runner(*, session_store=None, adapters=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = adapters or {}
    runner.session_store = session_store
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    return runner


def test_sanitize_zulip_topic_title_truncates_and_keeps_prefix():
    runner = _make_runner()
    long_title = "A" * 80
    out = runner._sanitize_zulip_topic_title(long_title)
    assert len(out) <= 60
    assert out.endswith("...")

    prefixed = runner._sanitize_zulip_topic_title(
        "New semantic title that is quite long for a topic",
        old_topic="t_2ce2781d — Wire something",
    )
    assert prefixed.startswith("t_2ce2781d — ")
    assert len(prefixed) <= 60
    assert "New semantic" in prefixed


def test_is_zulip_topic_lane_excludes_dms():
    runner = _make_runner()
    stream = _make_source()
    assert runner._is_zulip_topic_lane(stream) is True

    dm = _make_source(chat_id="dm:99", chat_type="dm", thread_id=None)
    assert runner._is_zulip_topic_lane(dm) is False

    dm_topic = _make_source(chat_id="dm:99", chat_type="dm", thread_id="ignored")
    assert runner._is_zulip_topic_lane(dm_topic) is False


def test_session_store_rekey_for_new_thread(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = _make_source(thread_id="handoff-abc")
    entry = store.get_or_create_session(source)
    old_key = entry.session_key
    assert "handoff-abc" in old_key

    result = store.rekey_session_for_new_thread(old_key, "Renamed Topic")
    assert result is not None
    new_entry, new_key = result
    assert new_entry.session_id == entry.session_id
    assert new_key != old_key
    assert "Renamed Topic" in new_key
    assert store.peek_session_id(old_key) is None
    assert store.peek_session_id(new_key) == entry.session_id
    assert new_entry.origin.thread_id == "Renamed Topic"

    again = store.get_or_create_session(_make_source(thread_id="Renamed Topic"))
    assert again.session_id == entry.session_id


def test_session_store_rekey_refuses_collision(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    a = store.get_or_create_session(_make_source(thread_id="topic-a"))
    b = store.get_or_create_session(_make_source(thread_id="topic-b"))
    assert a.session_id != b.session_id

    assert store.rekey_session_for_new_thread(a.session_key, "topic-b") is None
    assert store.peek_session_id(a.session_key) == a.session_id
    assert store.peek_session_id(b.session_key) == b.session_id


@pytest.mark.asyncio
async def test_rename_zulip_topic_calls_adapter_and_rekeys(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = _make_source(thread_id="old-topic")
    entry = store.get_or_create_session(source)
    old_key = entry.session_key

    platform = _zulip_platform()
    adapter = MagicMock()
    adapter.rename_topic = AsyncMock(return_value=True)
    runner = _make_runner(session_store=store, adapters={platform: adapter})
    runner._running_agents[old_key] = "agent-obj"
    runner._agent_cache[old_key] = ("cached",)

    await runner._rename_zulip_topic_for_session_title(
        source,
        entry.session_id,
        "Fresh Session Title",
    )

    adapter.rename_topic.assert_awaited_once_with(
        chat_id="12345",
        old_topic="old-topic",
        new_topic="Fresh Session Title",
    )
    new_key = build_session_key(
        _make_source(thread_id="Fresh Session Title"),
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    assert store.peek_session_id(old_key) is None
    assert store.peek_session_id(new_key) == entry.session_id
    assert runner._running_agents.get(new_key) == "agent-obj"
    assert old_key not in runner._running_agents
    assert runner._agent_cache.get(new_key) == ("cached",)
    assert source.thread_id == "Fresh Session Title"


@pytest.mark.asyncio
async def test_rename_zulip_topic_dm_noop(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    platform = _zulip_platform()
    adapter = MagicMock()
    adapter.rename_topic = AsyncMock(return_value=True)
    runner = _make_runner(session_store=store, adapters={platform: adapter})
    source = _make_source(chat_id="dm:1", chat_type="dm", thread_id=None)

    await runner._rename_zulip_topic_for_session_title(source, "sess", "Title")
    adapter.rename_topic.assert_not_called()


@pytest.mark.asyncio
async def test_rename_zulip_topic_adapter_failure_is_soft(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = _make_source(thread_id="old-topic")
    entry = store.get_or_create_session(source)
    old_key = entry.session_key

    platform = _zulip_platform()
    adapter = MagicMock()
    adapter.rename_topic = AsyncMock(side_effect=RuntimeError("network"))
    runner = _make_runner(session_store=store, adapters={platform: adapter})

    await runner._rename_zulip_topic_for_session_title(
        source, entry.session_id, "Title"
    )
    assert store.peek_session_id(old_key) == entry.session_id


@pytest.mark.asyncio
async def test_title_command_schedules_zulip_rename(tmp_path: Path):
    from hermes_state import AsyncSessionDB, SessionDB
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("test_session_123", "zulip")

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = AsyncSessionDB(db)
    mock_entry = MagicMock()
    mock_entry.session_id = "test_session_123"
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_entry
    runner.session_store = mock_store
    runner._schedule_telegram_topic_title_rename = MagicMock()
    runner._schedule_zulip_topic_title_rename = MagicMock()

    source = _make_source(thread_id="handoff")
    event = MessageEvent(text="/title My Zulip Title", source=source)
    result = await runner._handle_title_command(event)

    assert "My Zulip Title" in result
    runner._schedule_zulip_topic_title_rename.assert_called_once_with(
        source, "test_session_123", "My Zulip Title"
    )
    db.close()


@pytest.mark.asyncio
async def test_title_show_does_not_schedule_zulip_rename(tmp_path: Path):
    from hermes_state import AsyncSessionDB, SessionDB
    from gateway.run import GatewayRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("test_session_123", "zulip")
    db.set_session_title("test_session_123", "Existing")

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = AsyncSessionDB(db)
    mock_entry = MagicMock()
    mock_entry.session_id = "test_session_123"
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = mock_entry
    runner.session_store = mock_store
    runner._schedule_zulip_topic_title_rename = MagicMock()

    event = MessageEvent(text="/title", source=_make_source())
    await runner._handle_title_command(event)
    runner._schedule_zulip_topic_title_rename.assert_not_called()
    db.close()
