"""LINE group observation: unaddressed chatter is stored as observed context, never dispatched,
and the gateway keeps observed LINE rows out of replayed user turns."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_line = load_plugin_adapter("line")
LineAdapter = _line.LineAdapter


class _FakeStore:
    def __init__(self):
        self.sources, self.rows = [], []

    def get_or_create_session(self, source, **_kw):
        self.sources.append(source)
        return SimpleNamespace(session_id=f"sid-{source.chat_id}")

    def append_to_transcript(self, session_id, entry, **_kw):
        self.rows.append((session_id, entry))


def _adapter(monkeypatch):
    for var in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET", "LINE_MENTION_KEYWORDS", "LINE_GROUP_KEYWORD"):
        monkeypatch.delenv(var, raising=False)
    from gateway.config import PlatformConfig
    ad = LineAdapter(PlatformConfig(enabled=True, extra={
        "channel_access_token": "tok", "channel_secret": "sec", "mention_keywords": ["hermes"]}))
    ad._client = None  # no network: skip display-name lookups
    ad.handle_message = AsyncMock()
    store = _FakeStore()
    ad.set_session_store(store)
    return ad, store


def _event(text, *, source, mid="m1"):
    return {"type": "message", "replyToken": "t", "timestamp": 1791700000000,
            "source": source, "message": {"type": "text", "id": mid, "text": text}}


GROUP = {"type": "group", "groupId": "C1", "userId": "Ua"}


def test_unaddressed_group_message_is_observed_not_dispatched(monkeypatch):
    ad, store = _adapter(monkeypatch)
    asyncio.run(ad._handle_message_event(_event("dinner at 7 on friday", source=GROUP)))
    ad.handle_message.assert_not_awaited()
    assert len(store.rows) == 1
    sid, row = store.rows[0]
    assert sid == "sid-C1"
    assert row["role"] == "user" and row["observed"] is True
    assert "dinner at 7 on friday" in row["content"]
    assert row.get("message_id") == "m1"
    src = store.sources[0]
    assert src.chat_id == "C1" and src.chat_type == "group"


def test_observed_row_targets_the_shared_group_session(monkeypatch):
    from gateway.session import build_session_key
    ad, store = _adapter(monkeypatch)
    asyncio.run(ad._handle_message_event(_event("hi", source=GROUP)))
    asyncio.run(ad._handle_message_event(_event("hermes what time?", source={**GROUP, "userId": "Ub"}, mid="m2")))
    dispatched = ad.handle_message.await_args.args[0].source
    observed = store.sources[0]
    assert build_session_key(observed, group_sessions_per_user=True) == build_session_key(
        dispatched, group_sessions_per_user=True)


def test_addressed_group_message_is_dispatched_without_observed_row(monkeypatch):
    ad, store = _adapter(monkeypatch)
    asyncio.run(ad._handle_message_event(_event("hermes, what's the plan?", source=GROUP)))
    ad.handle_message.assert_awaited_once()
    assert store.rows == []


def test_dm_is_dispatched_and_never_observed(monkeypatch):
    ad, store = _adapter(monkeypatch)
    asyncio.run(ad._handle_message_event(_event("hello", source={"type": "user", "userId": "Ua"})))
    ad.handle_message.assert_awaited_once()
    assert store.rows == []


def test_observe_failure_is_non_fatal(monkeypatch):
    ad, store = _adapter(monkeypatch)
    store.append_to_transcript = MagicMock(side_effect=RuntimeError("db locked"))
    asyncio.run(ad._handle_message_event(_event("chatter", source=GROUP)))
    ad.handle_message.assert_not_awaited()


_HISTORY = [
    {"role": "user", "content": "[Alice]\ndinner at 7 on friday", "observed": True},
    {"role": "user", "content": "[Bob]\nI'll book it", "observed": True},
    {"role": "user", "content": "hermes when is dinner?"},
    {"role": "assistant", "content": "Friday at 7."},
]


def test_line_observed_rows_become_context_not_user_turns():
    from gateway.config import Platform
    from gateway.run import _build_gateway_agent_history, _wrap_current_message_with_observed_context
    history, observed = _build_gateway_agent_history(_HISTORY, platform=Platform("line"))
    assert [m["content"] for m in history] == ["hermes when is dinner?", "Friday at 7."]
    assert "dinner at 7 on friday" in observed and "I'll book it" in observed
    wrapped = _wrap_current_message_with_observed_context("hermes and dessert?", observed, Platform("line"))
    assert wrapped.startswith("[Observed LINE group context - context only, not requests]")
    assert wrapped.rstrip().endswith("hermes and dessert?")


def test_other_platforms_keep_previous_behavior():
    from gateway.run import _build_gateway_agent_history
    history, observed = _build_gateway_agent_history(_HISTORY)
    assert observed is None
    assert any("dinner at 7" in m["content"] for m in history)
    history, observed = _build_gateway_agent_history(
        _HISTORY, channel_prompt="uses observed Telegram group context", platform="telegram")
    assert "dinner at 7" in observed
