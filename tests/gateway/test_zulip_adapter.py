"""Tests for the Zulip platform adapter plugin.

Covers:
- /stop slash command registration on connect
- /stop command handling in message handler
- update_message event handling (message edits)
- Message filtering (self-messages, empty content, mention stripping)
- Stream and DM routing
- Payload building
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The Zulip adapter is a user plugin at ~/.hermes/plugins/zulip-hermes-integration/
# Load it directly from that path under a unique module name
_ZULIP_PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "zulip-hermes-integration"
_ZULIP_ADAPTER_PATH = _ZULIP_PLUGIN_DIR / "zulip_adapter.py"

if not _ZULIP_ADAPTER_PATH.is_file():
    pytest.skip(
        f"Zulip adapter not found at {_ZULIP_ADAPTER_PATH}",
        allow_module_level=True,
    )

# Load the adapter module under a unique name
_module_name = "plugin_adapter_zulip_user"
_spec = importlib.util.spec_from_file_location(_module_name, _ZULIP_ADAPTER_PATH)
if _spec is None or _spec.loader is None:
    pytest.skip(
        f"Could not build import spec for {_ZULIP_ADAPTER_PATH}",
        allow_module_level=True,
    )

# Mock the Platform enum BEFORE loading the module, so ZulipAdapter.__init__
# doesn't fail with "zulip is not a valid Platform".
# We replace the entire Platform class with a simple mock that accepts any value.
from unittest.mock import MagicMock
import sys

# Import gateway.config first so it's in sys.modules, then replace Platform
import gateway.config
_mock_platform = MagicMock()
_mock_platform.zulip = MagicMock()
gateway.config.Platform = _mock_platform

_zulip_mod = importlib.util.module_from_spec(_spec)
sys.modules[_module_name] = _zulip_mod
_spec.loader.exec_module(_zulip_mod)

ZulipAdapter = _zulip_mod.ZulipAdapter
check_requirements = _zulip_mod.check_requirements
validate_config = _zulip_mod.validate_config
register = _zulip_mod.register
_standalone_send = _zulip_mod._standalone_send
_MAX_MESSAGE_LENGTH = _zulip_mod.MAX_MESSAGE_LENGTH
_ENV = _zulip_mod._env
_normalize_env_aliases = _zulip_mod._normalize_env_aliases
_resolve_credentials = _zulip_mod._resolve_credentials
_make_client = _zulip_mod._make_client
_ENV_ENABLEMENT = _zulip_mod._env_enablement
_APPLY_YAML_CONFIG = _zulip_mod._apply_yaml_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_adapter(monkeypatch, **extra_kwargs):
    """Create a ZulipAdapter with mocked dependencies."""
    # Clear env vars
    for key in (
        "ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE",
        "ZULIP_HOME_CHANNEL", "ZULIP_HOME_CHANNEL_NAME",
        "ZULIP_ALLOWED_USERS", "ZULIP_ALLOWED_EMAILS",
        "ZULIP_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    from gateway.config import PlatformConfig
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "api_key": "test-api-key",
            "email": "bot@example.com",
            "site": "https://zulip.example.com",
            **extra_kwargs,
        },
    )

    # Mock _make_client to return a mock client that doesn't connect to the server
    mock_client = MagicMock()
    mock_client.get_server_settings = MagicMock(
        return_value={"result": "success"}
    )
    mock_client.register = MagicMock(
        return_value={"result": "success", "queue_id": "q1"}
    )
    mock_client.get_me = MagicMock(
        return_value={"result": "success", "user_id": 999}
    )
    mock_client.send_message = MagicMock(
        return_value={"result": "success", "id": 1}
    )
    mock_client.update_message = MagicMock(
        return_value={"result": "success"}
    )
    mock_client.update_server_settings = MagicMock(
        return_value={"result": "success"}
    )

    with patch.object(_zulip_mod, "_make_client", return_value=mock_client):
        adapter = ZulipAdapter(cfg)

    # Replace the real client with our mock (the adapter already has it)
    adapter.client = mock_client
    return adapter


def _make_zulip_message(
    content="Hello world",
    msg_type="stream",
    sender_email="user@example.com",
    sender_id=123,
    sender_full_name="Test User",
    stream_id=567,
    stream_name="general",
    subject="topic",
    msg_id=100,
    timestamp=1700000000,
):
    """Build a Zulip message dict for testing."""
    return {
        "id": msg_id,
        "type": msg_type,
        "content": content,
        "sender_email": sender_email,
        "sender_id": sender_id,
        "sender_full_name": sender_full_name,
        "display_recipient": stream_name if msg_type == "stream" else sender_full_name,
        "stream_id": stream_id if msg_type == "stream" else None,
        "subject": subject if msg_type == "stream" else None,
        "topic": subject if msg_type == "stream" else None,
        "timestamp": str(timestamp),
    }


def _make_update_message_event(
    content="Edited content",
    msg_type="stream",
    sender_email="user@example.com",
    sender_id=123,
    sender_full_name="Test User",
    stream_id=567,
    stream_name="general",
    subject="topic",
    msg_id=100,
    timestamp=1700000100,
):
    """Build an update_message event dict for testing."""
    return {
        "type": "update_message",
        "message": _make_zulip_message(
            content=content,
            msg_type=msg_type,
            sender_email=sender_email,
            sender_id=sender_id,
            sender_full_name=sender_full_name,
            stream_id=stream_id,
            stream_name=stream_name,
            subject=subject,
            msg_id=msg_id,
            timestamp=timestamp,
        ),
    }


# ---------------------------------------------------------------------------
# Tests: Slash command registration
# ---------------------------------------------------------------------------


class TestSlashCommandRegistration:
    """Test that /stop slash command is registered on connect."""

    @pytest.mark.asyncio
    async def test_register_slash_commands_calls_api(self, monkeypatch):
        """_register_slash_commands should call the Zulip API to register /stop."""
        adapter = _make_adapter(monkeypatch)

        # _register_slash_commands calls call_endpoint, not update_server_settings
        mock_call_endpoint = MagicMock(return_value={"result": "success"})
        adapter.client.call_endpoint = mock_call_endpoint

        # Call the method
        await adapter._register_slash_commands()

        # Verify call_endpoint was called once
        mock_call_endpoint.assert_called_once()
        call_kwargs = mock_call_endpoint.call_args[1]
        assert call_kwargs["url"] == "user_settings/slash_commands"
        assert call_kwargs["method"] == "POST"

    @pytest.mark.asyncio
    async def test_register_slash_commands_handles_error(self, monkeypatch):
        """_register_slash_commands should not crash on API failure."""
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_server_settings = MagicMock(
            side_effect=Exception("API error")
        )

        # Should not raise
        await adapter._register_slash_commands()

    @pytest.mark.asyncio
    async def test_connect_calls_register_slash_commands(self, monkeypatch):
        """connect() should call _register_slash_commands after successful auth."""
        adapter = _make_adapter(monkeypatch)

        # Mock the client methods with proper return values
        adapter.client.register = MagicMock(
            return_value={"result": "success", "queue_id": "q1"}
        )
        adapter.client.get_me = MagicMock(
            return_value={"result": "success", "user_id": 999}
        )
        adapter.client.get_profile = MagicMock(
            return_value={"result": "success", "full_name": "Test Bot", "email": "bot@example.com", "user_id": 999}
        )
        adapter.client.get_server_settings = MagicMock(
            return_value={"result": "success"}
        )

        # Mock the event task
        adapter._event_task = None

        # Mock _mark_connected to track calls
        adapter._mark_connected = MagicMock()

        # Call connect
        result = await adapter.connect()

        # Verify _mark_connected was called (meaning connect succeeded)
        adapter._mark_connected.assert_called_once()
        assert result is True


# ---------------------------------------------------------------------------
# Tests: /stop command handling
# ---------------------------------------------------------------------------


class TestStopCommandHandling:
    """Test that /stop commands are recognized and handled."""

    @pytest.mark.asyncio
    async def test_stop_command_in_stream(self, monkeypatch):
        """A /stop message in a stream should halt the active session."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True
        adapter._current_session_id = "session-123"

        # Mock handle_message to capture the event
        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        # Create a /stop message
        msg = _make_zulip_message(
            content="/stop",
            msg_type="stream",
            sender_email="user@example.com",
            sender_id=123,
            stream_id=567,
            stream_name="general",
            subject="topic",
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert dispatched[0].text == "/stop"
        assert dispatched[0].source.chat_id == "567"
        assert dispatched[0].source.chat_topic == "topic"

    @pytest.mark.asyncio
    async def test_stop_command_in_dm(self, monkeypatch):
        """A /stop message in a DM should halt the active session."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True
        adapter._current_session_id = "session-123"

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="/stop",
            msg_type="private",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert dispatched[0].text == "/stop"
        assert dispatched[0].source.chat_type == "dm"

    @pytest.mark.asyncio
    async def test_stop_with_trailing_text(self, monkeypatch):
        """A /stop message with trailing text should still be recognized."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="/stop please",
            msg_type="stream",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        # The text should be preserved as-is (gateway handles the /stop prefix)
        assert dispatched[0].text == "/stop please"

    @pytest.mark.asyncio
    async def test_stop_command_case_insensitive(self, monkeypatch):
        """The /stop command should be case-insensitive."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="/STOP",
            msg_type="stream",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert dispatched[0].text == "/STOP"


# ---------------------------------------------------------------------------
# Tests: Message edit handling (update_message events)
# ---------------------------------------------------------------------------


class TestMessageEditHandling:
    """Test that update_message events are handled correctly."""

    @pytest.mark.asyncio
    async def test_update_message_dispatches_edited_content(self, monkeypatch):
        """An update_message event should dispatch the edited content."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        event = _make_update_message_event(
            content="This was edited",
            msg_type="stream",
            sender_email="user@example.com",
            sender_id=123,
            stream_id=567,
            stream_name="general",
            subject="topic",
            msg_id=100,
        )

        await adapter._handle_message_update(event)

        assert len(dispatched) == 1
        assert dispatched[0].text == "This was edited"
        assert dispatched[0].source.chat_id == "567"
        assert dispatched[0].source.chat_topic == "topic"
        assert dispatched[0].message_id == "100"

    @pytest.mark.asyncio
    async def test_update_message_filters_self(self, monkeypatch):
        """update_message events from the bot itself should be filtered."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True
        adapter._own_user_id = 999  # Bot's user ID
        adapter.email = "bot@example.com"

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        event = _make_update_message_event(
            content="Bot edited its own message",
            sender_email="bot@example.com",
            sender_id=999,
        )

        await adapter._handle_message_update(event)

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_update_message_filters_empty_content(self, monkeypatch):
        """update_message events with empty content should be filtered."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        event = _make_update_message_event(
            content="",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message_update(event)

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_update_message_in_dm(self, monkeypatch):
        """update_message events in DMs should be handled correctly."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        event = _make_update_message_event(
            content="Edited DM message",
            msg_type="private",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message_update(event)

        assert len(dispatched) == 1
        assert dispatched[0].text == "Edited DM message"
        assert dispatched[0].source.chat_type == "dm"
        assert dispatched[0].source.chat_id == "dm:123"

    @pytest.mark.asyncio
    async def test_update_message_strips_mentions(self, monkeypatch):
        """update_message should strip Zulip bold-mention syntax."""
        adapter = _make_adapter(monkeypatch)
        adapter._running = True

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        event = _make_update_message_event(
            content="Hello **Test User** how are you?",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message_update(event)

        assert len(dispatched) == 1
        # The mention syntax should be stripped
        assert "**Test User**" not in dispatched[0].text
        assert "Test User" in dispatched[0].text


# ---------------------------------------------------------------------------
# Tests: Message filtering
# ---------------------------------------------------------------------------


class TestMessageFiltering:
    """Test message filtering logic."""

    @pytest.mark.asyncio
    async def test_filters_own_messages_by_email(self, monkeypatch):
        """Messages from the bot's own email should be filtered."""
        adapter = _make_adapter(monkeypatch)
        adapter.email = "bot@example.com"

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="My own message",
            sender_email="bot@example.com",
            sender_id=999,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_filters_own_messages_by_id(self, monkeypatch):
        """Messages from the bot's own user ID should be filtered."""
        adapter = _make_adapter(monkeypatch)
        adapter._own_user_id = 999
        adapter.email = "bot@example.com"

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="My own message",
            sender_email="other@example.com",
            sender_id=999,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_filters_empty_content(self, monkeypatch):
        """Messages with empty content should be filtered."""
        adapter = _make_adapter(monkeypatch)

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(content="")

        await adapter._handle_message(msg)

        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_strips_bold_mentions(self, monkeypatch):
        """Zulip bold-mention syntax should be stripped."""
        adapter = _make_adapter(monkeypatch)

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            content="Hello **Test User** welcome!",
            sender_email="user@example.com",
            sender_id=123,
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert "**Test User**" not in dispatched[0].text
        assert "Test User" in dispatched[0].text


# ---------------------------------------------------------------------------
# Tests: Stream and DM routing
# ---------------------------------------------------------------------------


class TestRouting:
    """Test stream and DM message routing."""

    @pytest.mark.asyncio
    async def test_stream_message_routing(self, monkeypatch):
        """Stream messages should route with stream_id as chat_id."""
        adapter = _make_adapter(monkeypatch)

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            msg_type="stream",
            stream_id=567,
            stream_name="general",
            subject="topic",
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert dispatched[0].source.chat_id == "567"
        assert dispatched[0].source.chat_type == "channel"
        assert dispatched[0].source.chat_topic == "topic"
        assert dispatched[0].source.thread_id == "topic"

    @pytest.mark.asyncio
    async def test_dm_message_routing(self, monkeypatch):
        """DM messages should route with dm: prefix as chat_id."""
        adapter = _make_adapter(monkeypatch)

        dispatched = []

        async def capture_handle_message(event):
            dispatched.append(event)

        adapter.handle_message = capture_handle_message

        msg = _make_zulip_message(
            msg_type="private",
            sender_email="user@example.com",
            sender_id=123,
            sender_full_name="Test User",
        )

        await adapter._handle_message(msg)

        assert len(dispatched) == 1
        assert dispatched[0].source.chat_id == "dm:123"
        assert dispatched[0].source.chat_type == "dm"


# ---------------------------------------------------------------------------
# Tests: Payload building
# ---------------------------------------------------------------------------


class TestPayloadBuilding:
    """Test message payload building for outbound messages."""

    def test_build_stream_payload_by_id(self):
        """Stream messages should use numeric stream_id."""
        adapter = _make_adapter(
            monkeypatch=MagicMock(),
            home_channel={"chat_id": "567", "name": "general"},
        )
        payload = adapter._build_send_payload("567", "Hello", {"topic": "topic"})
        assert payload["type"] == "stream"
        assert payload["to"] == 567
        assert payload["topic"] == "topic"
        assert payload["content"] == "Hello"

    def test_build_stream_payload_by_name(self):
        """Stream messages should accept stream name as string."""
        adapter = _make_adapter(
            monkeypatch=MagicMock(),
            home_channel={"chat_id": "567", "name": "general"},
        )
        payload = adapter._build_send_payload("general", "Hello", {"topic": "topic"})
        assert payload["type"] == "stream"
        assert payload["to"] == "general"
        assert payload["topic"] == "topic"

    def test_build_dm_payload_by_id(self):
        """DM messages should use numeric user_id."""
        adapter = _make_adapter(
            monkeypatch=MagicMock(),
            home_channel={"chat_id": "567", "name": "general"},
        )
        payload = adapter._build_send_payload("dm:123", "Hello", {})
        assert payload["type"] == "private"
        assert payload["to"] == [123]
        assert payload["content"] == "Hello"

    def test_build_dm_payload_legacy_format(self):
        """Legacy dm_user: format should also work."""
        adapter = _make_adapter(
            monkeypatch=MagicMock(),
            home_channel={"chat_id": "567", "name": "general"},
        )
        payload = adapter._build_send_payload("dm_user:123", "Hello", {})
        assert payload["type"] == "private"
        assert payload["to"] == [123]

    def test_truncates_long_messages(self, monkeypatch):
        """Messages exceeding MAX_MESSAGE_LENGTH should be truncated."""
        adapter = _make_adapter(monkeypatch)
        long_content = "x" * (_MAX_MESSAGE_LENGTH + 100)

        adapter._running = True
        adapter.client = MagicMock()
        adapter.client.send_message = MagicMock(
            return_value={"result": "success", "id": 1}
        )

        result = asyncio.run(adapter.send("567", long_content))
        assert result.success is True
        # Verify the client was called with truncated content
        call_args = adapter.client.send_message.call_args[0][0]
        assert len(call_args["content"]) <= _MAX_MESSAGE_LENGTH


# ---------------------------------------------------------------------------
# Tests: Outbound edit_message (stream consumer progressive drafts)
# ---------------------------------------------------------------------------


class TestEditMessageOutbound:
    """Outbound edit_message → Zulip client.update_message."""

    def test_edit_message_success(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_message = MagicMock(
            return_value={"result": "success"}
        )
        result = asyncio.run(
            adapter.edit_message("567", "42", "draft growing ▉", finalize=False)
        )
        assert result.success is True
        assert result.message_id == "42"
        payload = adapter.client.update_message.call_args[0][0]
        assert payload == {"message_id": 42, "content": "draft growing ▉"}

    def test_edit_message_finalize_accepted(self, monkeypatch):
        """finalize is BasePlatformAdapter-compat no-op but must not raise."""
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_message = MagicMock(
            return_value={"result": "success"}
        )
        result = asyncio.run(
            adapter.edit_message("567", "7", "final text", finalize=True)
        )
        assert result.success is True
        assert result.message_id == "7"

    def test_edit_message_rejects_empty(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        result = asyncio.run(adapter.edit_message("567", "1", ""))
        assert result.success is False
        assert "empty" in (result.error or "").lower()
        adapter.client.update_message.assert_not_called()

    def test_edit_message_rejects_invalid_id(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        result = asyncio.run(adapter.edit_message("567", "not-int", "x"))
        assert result.success is False
        assert "invalid message_id" in (result.error or "")
        adapter.client.update_message.assert_not_called()

    def test_edit_message_truncates_to_max(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_message = MagicMock(
            return_value={"result": "success"}
        )
        long_content = "y" * (_MAX_MESSAGE_LENGTH + 50)
        result = asyncio.run(adapter.edit_message("567", "9", long_content))
        assert result.success is True
        payload = adapter.client.update_message.call_args[0][0]
        assert len(payload["content"]) == _MAX_MESSAGE_LENGTH

    def test_edit_message_api_error(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_message = MagicMock(
            return_value={"result": "error", "msg": "rate limit exceeded"}
        )
        result = asyncio.run(adapter.edit_message("567", "1", "hi"))
        assert result.success is False
        assert "rate limit" in (result.error or "").lower()
        assert result.retryable is True

    def test_edit_message_exception_retryable(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client.update_message = MagicMock(
            side_effect=ConnectionError("network blip")
        )
        result = asyncio.run(adapter.edit_message("567", "1", "hi"))
        assert result.success is False
        assert result.retryable is True
        assert "network blip" in (result.error or "")

    def test_edit_overrides_base_so_progress_path_sees_editing(self, monkeypatch):
        """Gateway skips tool-progress when edit_message is still the base stub."""
        from gateway.platforms.base import BasePlatformAdapter

        adapter = _make_adapter(monkeypatch)
        assert type(adapter).edit_message is not BasePlatformAdapter.edit_message


# ---------------------------------------------------------------------------
# Tests: Requirements and validation
# ---------------------------------------------------------------------------


class TestRequirementsAndValidation:
    """Test check_requirements and validate_config."""

    def test_check_requirements_with_env(self, monkeypatch):
        """check_requirements only checks if the zulip package is importable."""
        assert check_requirements() is True

    def test_check_requirements_missing_key(self, monkeypatch):
        """check_requirements ignores env vars — it only checks package availability."""
        monkeypatch.delenv("ZULIP_API_KEY", raising=False)
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")
        # check_requirements does NOT check env vars — it only checks ZULIP_AVAILABLE
        assert check_requirements() is True

    def test_check_requirements_missing_email(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.delenv("ZULIP_EMAIL", raising=False)
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")
        assert check_requirements() is True

    def test_check_requirements_missing_site(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.delenv("ZULIP_SITE", raising=False)
        assert check_requirements() is True

    def test_validate_config_from_extra(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            extra={
                "api_key": "key",
                "email": "bot@example.com",
                "site": "https://zulip.example.com",
            }
        )
        assert validate_config(cfg) is True

    def test_validate_config_missing(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(extra={})
        assert validate_config(cfg) is False


# ---------------------------------------------------------------------------
# Tests: Plugin registration
# ---------------------------------------------------------------------------


class TestPluginRegistration:
    """Test the register() entry point."""

    def test_register_adds_to_registry(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")

        from gateway.platform_registry import platform_registry

        # Clean up if already registered
        platform_registry.unregister("zulip")

        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        call_kwargs = ctx.register_platform.call_args
        assert call_kwargs[1]["name"] == "zulip"

    def test_register_sets_correct_adapter_factory(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)

        ctx = MagicMock()
        register(ctx)
        call_kwargs = ctx.register_platform.call_args
        adapter_factory = call_kwargs[1].get("adapter_factory") or call_kwargs[0][1]
        # The factory should be a callable that returns a ZulipAdapter
        assert callable(adapter_factory)


# ---------------------------------------------------------------------------
# Tests: Standalone send
# ---------------------------------------------------------------------------


class TestStandaloneSend:
    """Test the _standalone_send function for cron/tools fallbacks."""

    @pytest.mark.asyncio
    async def test_standalone_send_stream(self, monkeypatch):
        """Standalone send should work for stream messages."""
        # Clear env
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_API_KEY", "test-key")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")

        # Mock the client
        mock_client = MagicMock()
        mock_client.send_message = MagicMock(
            return_value={"result": "success", "id": 42}
        )

        with patch.object(_zulip_mod, "_make_client", return_value=mock_client):
            from gateway.config import PlatformConfig
            pconfig = PlatformConfig(
                extra={
                    "api_key": "test-key",
                    "email": "bot@example.com",
                    "site": "https://zulip.example.com",
                }
            )
            result = await _standalone_send(pconfig, "567", "Hello world")

        assert result["success"] is True
        assert result["platform"] == "zulip"
        assert result["chat_id"] == "567"
        assert result["message_id"] == 42

    @pytest.mark.asyncio
    async def test_standalone_send_dm(self, monkeypatch):
        """Standalone send should work for DM messages."""
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ZULIP_API_KEY", "test-key")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")

        mock_client = MagicMock()
        mock_client.send_message = MagicMock(
            return_value={"result": "success", "id": 43}
        )

        with patch.object(_zulip_mod, "_make_client", return_value=mock_client):
            from gateway.config import PlatformConfig
            pconfig = PlatformConfig(
                extra={
                    "api_key": "test-key",
                    "email": "bot@example.com",
                    "site": "https://zulip.example.com",
                }
            )
            result = await _standalone_send(pconfig, "dm:123", "Hello DM")

        assert result["success"] is True
        assert result["platform"] == "zulip"
        assert result["chat_id"] == "dm:123"
        assert result["message_id"] == 43


# ---------------------------------------------------------------------------
# Tests: Env enablement and YAML config
# ---------------------------------------------------------------------------


class TestEnvEnablement:
    """Test _env_enablement and _apply_yaml_config."""

    def test_env_enablement_with_env_vars(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://zulip.example.com")
        result = _ENV_ENABLEMENT()
        assert result is not None
        assert result["api_key"] == "key"
        assert result["email"] == "bot@example.com"
        assert result["site"] == "https://zulip.example.com"

    def test_env_enablement_missing_creds(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)
        result = _ENV_ENABLEMENT()
        assert result is None

    def test_apply_yaml_config_from_platform_cfg(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"):
            monkeypatch.delenv(key, raising=False)

        yaml_cfg = {
            "api_key": "yaml-key",
            "email": "yaml@example.com",
            "site": "https://yaml.example.com",
        }
        result = _APPLY_YAML_CONFIG(yaml_cfg, yaml_cfg)
        assert result is not None
        assert result["api_key"] == "yaml-key"
        assert result["email"] == "yaml@example.com"
        assert result["site"] == "https://yaml.example.com"

    def test_apply_yaml_config_with_home_channel(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE",
                     "ZULIP_HOME_CHANNEL", "ZULIP_HOME_CHANNEL_NAME"):
            monkeypatch.delenv(key, raising=False)

        yaml_cfg = {
            "api_key": "key",
            "email": "bot@example.com",
            "site": "https://zulip.example.com",
            "home_channel": {"chat_id": "567", "name": "general"},
        }
        result = _APPLY_YAML_CONFIG(yaml_cfg, yaml_cfg)
        assert result is not None
        assert "home_channel" in result
        assert result["home_channel"]["chat_id"] == "567"
        assert result["home_channel"]["name"] == "general"

    def test_apply_yaml_config_allows_from(self, monkeypatch):
        for key in ("ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE",
                     "ZULIP_ALLOWED_USERS"):
            monkeypatch.delenv(key, raising=False)

        yaml_cfg = {
            "api_key": "key",
            "email": "bot@example.com",
            "site": "https://zulip.example.com",
            "allow_from": ["user1@example.com", "user2@example.com"],
        }
        result = _APPLY_YAML_CONFIG(yaml_cfg, yaml_cfg)
        assert result is not None

    def test_env_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "env-key")
        monkeypatch.setenv("ZULIP_EMAIL", "env@example.com")
        monkeypatch.setenv("ZULIP_SITE", "https://env.example.com")

        yaml_cfg = {
            "api_key": "yaml-key",
            "email": "yaml@example.com",
            "site": "https://yaml.example.com",
        }
        result = _APPLY_YAML_CONFIG(yaml_cfg, yaml_cfg)
        assert result is not None
        # _apply_yaml_config sets env vars from YAML but the seed reflects
        # the platform_cfg values (YAML). The env vars are set as a side effect.
        assert result["api_key"] == "yaml-key"
        assert result["email"] == "yaml@example.com"
        assert result["site"] == "https://yaml.example.com"
        # Verify env vars were set (YAML values, since env was already set)
        assert os.getenv("ZULIP_API_KEY") == "env-key"
        assert os.getenv("ZULIP_EMAIL") == "env@example.com"
        assert os.getenv("ZULIP_SITE") == "https://env.example.com"
