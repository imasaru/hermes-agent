"""Regression: _await_gateway_decision must never return None (t_640d3683).

Commit 846e678c2 inserted kanban permission helpers immediately after the
post_approval_response hook and accidentally dropped the function's return
statement. Callers then crashed with:

    AttributeError: 'NoneType' object has no attribute 'get'

on decision.get("notify_failed") inside check_all_command_guards /
check_execute_code_guard / _run_approval_gate.
"""

from __future__ import annotations

import os
import threading
import time


class TestAwaitGatewayDecisionNeverReturnsNone:
    """_await_gateway_decision must always return a decision dict."""

    SESSION_KEY = "test-await-gateway-decision-none"

    def setup_method(self):
        from tools import approval as mod

        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()
        mod._pending.clear()
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "HERMES_GATEWAY_SESSION",
                "HERMES_CRON_SESSION",
                "HERMES_YOLO_MODE",
                "HERMES_SESSION_KEY",
                "HERMES_INTERACTIVE",
                "HERMES_KANBAN_TASK",
            )
        }
        os.environ.pop("HERMES_YOLO_MODE", None)
        os.environ.pop("HERMES_INTERACTIVE", None)
        os.environ.pop("HERMES_CRON_SESSION", None)
        os.environ.pop("HERMES_KANBAN_TASK", None)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_SESSION_KEY"] = self.SESSION_KEY

    def teardown_method(self):
        from tools import approval as mod

        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _force_short_timeout(self, monkeypatch, seconds=1):
        from tools import approval as mod

        monkeypatch.setattr(
            mod,
            "_get_approval_config",
            lambda: {
                "mode": "manual",
                "gateway_timeout": seconds,
                "timeout": seconds,
            },
        )

    def test_await_gateway_decision_returns_dict_on_timeout(self, monkeypatch):
        """Timeout path returns a dict, never None."""
        from tools import approval as mod

        self._force_short_timeout(monkeypatch, seconds=1)
        decision = mod._await_gateway_decision(
            self.SESSION_KEY,
            lambda data: None,
            {
                "command": "echo hello",
                "pattern_key": "test_pattern",
                "pattern_keys": ["test_pattern"],
                "description": "test warning",
            },
            surface="gateway",
        )
        assert decision is not None, (
            "_await_gateway_decision returned None — callers will AttributeError "
            "on decision.get('notify_failed') (t_640d3683)"
        )
        assert isinstance(decision, dict)
        assert "resolved" in decision
        assert "choice" in decision
        assert decision.get("resolved") is False
        assert decision.get("notify_failed") is not True

    def test_await_gateway_decision_returns_dict_on_approve(self, monkeypatch):
        """Approve path also returns a non-None decision dict."""
        from tools import approval as mod

        self._force_short_timeout(monkeypatch, seconds=5)
        result_holder = {}

        def _run():
            result_holder["d"] = mod._await_gateway_decision(
                self.SESSION_KEY,
                lambda data: None,
                {
                    "command": "echo hello",
                    "pattern_key": "test_pattern",
                    "pattern_keys": ["test_pattern"],
                    "description": "test warning",
                },
                surface="gateway",
            )

        t = threading.Thread(target=_run)
        t.start()
        for _ in range(50):
            if mod._gateway_queues.get(self.SESSION_KEY):
                break
            time.sleep(0.02)
        mod.resolve_gateway_approval(self.SESSION_KEY, "once")
        t.join(timeout=5)
        assert "d" in result_holder, "await did not return after approve"
        decision = result_holder["d"]
        assert decision is not None
        assert isinstance(decision, dict)
        assert decision.get("resolved") is True
        assert decision.get("choice") == "once"

    def test_await_gateway_decision_notify_failed_is_dict(self):
        """Notify failure must return {notify_failed: True}, not raise/None."""
        from tools import approval as mod

        def _boom(_data):
            raise RuntimeError("notify transport down")

        decision = mod._await_gateway_decision(
            self.SESSION_KEY,
            _boom,
            {
                "command": "echo hello",
                "pattern_key": "test_pattern",
                "pattern_keys": ["test_pattern"],
                "description": "test warning",
            },
            surface="gateway",
        )
        assert decision is not None
        assert decision.get("notify_failed") is True
        assert decision.get("resolved") is False

    def test_check_all_command_guards_no_attributeerror_on_gateway_path(
        self, monkeypatch
    ):
        """Gateway notify + flagged cmd must not AttributeError on decision.get."""
        from tools import approval as mod

        self._force_short_timeout(monkeypatch, seconds=1)
        mod.register_gateway_notify(self.SESSION_KEY, lambda data: None)

        # Force a dangerous hit without embedding a real destructive string.
        monkeypatch.setattr(
            mod,
            "detect_dangerous_command",
            lambda command: (True, "test_pattern", "flagged for test"),
        )
        monkeypatch.setattr(
            mod,
            "detect_hardline_command",
            lambda command: (False, None),
        )

        # Pre-fix: if decision is None, this is the production AttributeError.
        result = mod.check_all_command_guards("echo safe-looking", "local")
        assert result is not None
        assert isinstance(result, dict)
        assert "approved" in result
        assert result["approved"] is False
