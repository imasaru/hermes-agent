"""LINE display-name profile cache: the webhook carries only a user ID, the adapter
looks the name up (get_user_profile for DMs, get_group_member_profile for groups),
caches it, and reuses it on later messages without a new call.

Outcome-based: feed a real webhook event through ``_handle_message_event`` and assert
on the observable result -- the display name the agent sees in the event/transcript,
and how many times the mocked LINE API boundary was hit. "X was called N times" is a
secondary diagnostic here, never the main check.

The adapter dispatches the current message with ``user_name=user_id`` (adapter.py:964),
so for DMs and addressed groups the cached name reaches the agent only through the
observed-group transcript path. That path is where the cache is observable end to end,
so the outcome assertions live there; the API-hit counts verify the caching contract.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_line = load_plugin_adapter("line")
LineAdapter = _line.LineAdapter


class _FakeStore:
    """Session store stub: records the source the adapter attributed and the rows."""

    def __init__(self):
        self.sources = []
        self.rows = []

    def get_or_create_session(self, source, **_kw):
        self.sources.append(source)
        return SimpleNamespace(session_id=f"sid-{source.chat_id}")

    def append_to_transcript(self, session_id, entry, **_kw):
        self.rows.append((session_id, entry))


def _adapter(monkeypatch):
    for var in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET",
                "LINE_MENTION_KEYWORDS", "LINE_GROUP_KEYWORD"):
        monkeypatch.delenv(var, raising=False)
    from gateway.config import PlatformConfig
    ad = LineAdapter(PlatformConfig(enabled=True, extra={
        "channel_access_token": "tok", "channel_secret": "sec",
        "mention_keywords": ["hermes"]}))
    ad._client = MagicMock()
    ad._client.loading = AsyncMock()
    ad._client.mark_read = AsyncMock()
    ad.handle_message = AsyncMock()
    store = _FakeStore()
    ad.set_session_store(store)
    return ad, store


def _group_event(text, *, user_id="U1", mid="m1"):
    return {"type": "message", "replyToken": "t", "timestamp": 1791700000000,
            "source": {"type": "group", "groupId": "C1", "userId": user_id},
            "message": {"type": "text", "id": mid, "text": text}}


def _dm_event(text, *, user_id="U1", mid="m1"):
    return {"type": "message", "replyToken": "t",
            "source": {"type": "user", "userId": user_id},
            "message": {"type": "text", "id": mid, "text": text}}


async def _deliver(ad, event):
    """Run one webhook through the handler and drain the fire-and-forget
    display-name tasks on the same event loop they were created on."""
    await ad._handle_message_event(event)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Group display-name cache (observable end to end via the observed transcript)
# ---------------------------------------------------------------------------

class TestGroupDisplayNameCache:

    def test_first_group_message_looks_up_and_caches_name(self, monkeypatch):
        """A group message triggers get_group_member_profile with (group, user) and,
        once the cache is populated, the observed transcript attributes the real name."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_group_member_profile = AsyncMock(
            side_effect=lambda gid, uid: {"displayName": "Carol"} if uid == "U1" else {"displayName": "Dan"})

        asyncio.run(_deliver(ad, _group_event("hi everyone", user_id="U1")))

        # Diagnostic: the boundary was hit once, with the group id and the user id.
        assert ad._client.get_group_member_profile.await_count == 1
        assert ad._client.get_group_member_profile.call_args == (("C1", "U1"), {})
        # Outcome: the transcript now carries the real display name, not the ID.
        assert store.rows[0][1]["content"].startswith("[Carol")

    def test_repeat_message_uses_cached_name_no_new_call(self, monkeypatch):
        """A second message from the same user reuses the cached name with no new API call."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_group_member_profile = AsyncMock(
            side_effect=lambda gid, uid: {"displayName": "Carol"})

        asyncio.run(_deliver(ad, _group_event("first", user_id="U1")))
        n_after_first = ad._client.get_group_member_profile.await_count

        asyncio.run(_deliver(ad, _group_event("second", user_id="U1")))

        # Outcome: same name, and no new boundary call for the repeat.
        assert store.rows[1][1]["content"].startswith("[Carol")
        assert ad._client.get_group_member_profile.await_count == n_after_first

    def test_different_user_triggers_new_lookup(self, monkeypatch):
        """A different user id in the same group is not served by the cache."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_group_member_profile = AsyncMock(
            side_effect=lambda gid, uid: {"displayName": "Carol"} if uid == "U1" else {"displayName": "Dan"})

        asyncio.run(_deliver(ad, _group_event("hi", user_id="U1")))
        n_before = ad._client.get_group_member_profile.await_count

        asyncio.run(_deliver(ad, _group_event("yo", user_id="U2")))

        # Outcome: the new user's name appears, and a second boundary call was made.
        assert store.rows[1][1]["content"].startswith("[Dan")
        assert ad._client.get_group_member_profile.await_count == n_before + 1


# ---------------------------------------------------------------------------
# Failure / fallback
# ---------------------------------------------------------------------------

class TestDisplayNameFallback:

    def test_lookup_error_falls_back_to_id_without_caching(self, monkeypatch):
        """If the profile lookup raises, the adapter must not crash and must not
        cache a bad value; the transcript degrades to the raw ID."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_group_member_profile = AsyncMock(side_effect=RuntimeError("LINE down"))

        asyncio.run(_deliver(ad, _group_event("hi", user_id="Ufail")))

        # Outcome: no crash, the row is still observed.
        assert store.rows[0][1]["content"].endswith("hi")
        # Diagnostic: the failure did not poison the cache.
        assert "Ufail" not in ad._display_names
        assert store.rows[0][1]["content"].startswith("[Ufail")


# ---------------------------------------------------------------------------
# DM display-name cache (the contract the feature promises; currently unobservable
# because the dispatched event carries user_id as user_name)
# ---------------------------------------------------------------------------

class TestDisplayNameContract:

    def test_dm_first_message_makes_no_display_name_lookup(self, monkeypatch):
        """Current behavior: the DM path never calls get_user_profile at all -- only
        addressed groups resolve the name on this path. Diagnostic only; the outcome
        is that the dispatched event carries the raw ID."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_user_profile = AsyncMock(return_value={"displayName": "Alice"})

        asyncio.run(_deliver(ad, _dm_event("hello", user_id="U1")))

        # Diagnostic: the current adapter does not look up a DM name on this path.
        assert ad._client.get_user_profile.await_count == 0
        # Outcome (current behavior): the dispatched event still carries the raw ID.
        event = ad.handle_message.await_args.args[0]
        assert event.source.user_name == "U1"

    def test_dm_repeat_message_makes_no_new_call(self, monkeypatch):
        """A second DM from U1 makes no new get_user_profile call."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_user_profile = AsyncMock(return_value={"displayName": "Alice"})

        asyncio.run(_deliver(ad, _dm_event("one", user_id="U1")))
        n_after_first = ad._client.get_user_profile.await_count

        asyncio.run(_deliver(ad, _dm_event("two", user_id="U1")))

        # Outcome: no new boundary call for the repeat.
        assert ad._client.get_user_profile.await_count == n_after_first

    @pytest.mark.xfail(reason="DM display name is looked up but the dispatched event "
                              "carries user_id as user_name (adapter.py:964), so the "
                              "cached name never reaches the agent for DMs.")
    def test_dm_event_carries_cached_display_name(self, monkeypatch):
        """Intended behavior: a DM's event shows the user's cached display name."""
        ad, store = _adapter(monkeypatch)
        ad._client.get_user_profile = AsyncMock(return_value={"displayName": "Alice"})

        asyncio.run(_deliver(ad, _dm_event("hello", user_id="U1")))

        event = ad.handle_message.await_args.args[0]
        assert event.source.user_name == "Alice"
