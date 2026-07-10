from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _no_plugin_execution_middleware(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.middleware._get_middleware_callbacks", lambda _kind: []
    )


def test_policy_resolves_tool_name_before_toolset_and_uses_most_restrictive(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "file*": "allow",
                    "write_*": "ask",
                    "write_file": "deny",
                }
            }
        },
    )
    monkeypatch.setattr(
        "tools.registry.registry.get_entry",
        lambda _name: SimpleNamespace(toolset="files"),
    )

    assert middleware.resolve_tool_approval_policy("write_file") == "deny"


def test_policy_can_match_registered_toolset(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"approvals": {"tool_policies": {"browser*": "ask"}}},
    )
    monkeypatch.setattr(
        "tools.registry.registry.get_entry",
        lambda _name: SimpleNamespace(toolset="browser"),
    )

    assert middleware.resolve_tool_approval_policy("browser_click") == "ask"


def test_malformed_policy_entries_are_ignored(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "approvals": {
                "tool_policies": {
                    "terminal": "maybe",
                    42: "deny",
                    "*": None,
                }
            }
        },
    )

    assert middleware.resolve_tool_approval_policy("terminal") is None


def test_deny_policy_blocks_without_calling_tool(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "deny"
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "write_file", {"path": "notes.txt"}, lambda args: called.append(args)
    )

    assert called == []
    assert json.loads(result)["error"].startswith("BLOCKED: Tool 'write_file'")


def test_ask_policy_uses_shared_fail_closed_approval_gate(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "ask"
    )
    requested = []
    monkeypatch.setattr(
        "tools.approval.request_tool_approval",
        lambda tool_name, reason, **kwargs: requested.append(
            (tool_name, reason, kwargs)
        )
        or {"approved": False, "message": "BLOCKED by cron policy"},
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "terminal", {"command": "printf ok"}, lambda args: called.append(args)
    )

    assert json.loads(result)["error"] == "BLOCKED by cron policy"
    assert called == []
    assert requested[0][0] == "terminal"
    assert requested[0][2]["rule_key"] == "tool_policy:terminal"


def test_allow_policy_does_not_skip_downstream_execution_middleware(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    events = []

    def plugin_middleware(**kwargs):
        events.append("plugin")
        return kwargs["next_call"](kwargs["args"])

    monkeypatch.setattr(
        middleware, "_get_middleware_callbacks", lambda _kind: [plugin_middleware]
    )

    result = middleware.run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda _args: events.append("tool") or "ok"
    )

    assert result == "ok"
    assert events == ["plugin", "tool"]


def test_direct_registry_dispatch_is_policy_gated(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "deny"
    )
    dispatched = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kwargs: dispatched.append((name, args)) or "ran",
    )

    result = model_tools.handle_function_call("read_file", {"path": "README.md"})

    assert dispatched == []
    assert "Tool 'read_file' is denied" in json.loads(result)["error"]


def test_sequential_agent_level_dispatch_is_policy_gated(monkeypatch):
    from agent import tool_executor
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "deny"
    )
    called = []
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
    )

    result, _ = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="todo",
        function_args={"todos": []},
        effective_task_id="task",
        tool_call_id="call",
        execute=lambda args: called.append(args) or "ran",
    )

    assert called == []
    assert "Tool 'todo' is denied" in json.loads(result)["error"]


def test_concurrent_agent_level_dispatch_is_policy_gated(monkeypatch):
    from agent import agent_runtime_helpers
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "deny"
    )
    called = []
    agent = SimpleNamespace(
        session_id="s",
        _current_turn_id="t",
        _current_api_request_id="r",
        _todo_store=object(),
        _memory_manager=None,
        valid_tool_names=set(),
        enabled_toolsets=None,
        disabled_toolsets=None,
    )
    monkeypatch.setattr(
        "tools.todo_tool.todo_tool", lambda **kwargs: called.append(kwargs) or "ran"
    )

    result = agent_runtime_helpers.invoke_tool(
        agent, "todo", {"todos": []}, "task", tool_call_id="call",
        pre_tool_block_checked=True,
    )

    assert called == []
    assert "Tool 'todo' is denied" in json.loads(result)["error"]


def test_explicit_allow_does_not_bypass_plugin_escalation(monkeypatch):
    import model_tools
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.resolve_pre_tool_block",
        lambda *_args, **_kwargs: "plugin requires approval",
    )
    dispatched = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **kwargs: dispatched.append((name, args)) or "ran",
    )

    result = model_tools.handle_function_call("read_file", {"path": "README.md"})

    assert dispatched == []
    assert json.loads(result)["error"] == "plugin requires approval"


def test_explicit_allow_does_not_bypass_terminal_hardline(monkeypatch):
    from hermes_cli import middleware
    from tools.approval import check_all_command_guards

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    called = []

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "rm -rf /"},
        lambda args: called.append(args)
        or check_all_command_guards(args["command"], "local"),
    )

    assert called == [{"command": "rm -rf /"}]
    assert result["approved"] is False
    assert "hardline" in result["message"].lower()


def test_explicit_allow_bypasses_only_ordinary_terminal_prompt(monkeypatch):
    from hermes_cli import middleware
    from tools import approval

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    prompted = []

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "rm -rf /tmp/hermes-policy-test"},
        lambda args: approval.check_all_command_guards(
            args["command"],
            "local",
            approval_callback=lambda *_args, **_kwargs: prompted.append(True) or "deny",
        ),
    )

    assert result["approved"] is True
    assert prompted == []


def test_explicit_allow_does_not_bypass_terminal_user_deny(monkeypatch):
    from hermes_cli import middleware
    from tools import approval

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    monkeypatch.setattr(approval, "_match_user_deny_rule", lambda _command: "git push *")

    result = middleware.run_tool_execution_middleware(
        "terminal",
        {"command": "git push origin main"},
        lambda args: approval.check_all_command_guards(args["command"], "local"),
    )

    assert result["approved"] is False
    assert "user-defined deny rule" in result["message"].lower()


def test_explicit_allow_does_not_bypass_credential_path_guard(monkeypatch, tmp_path):
    from hermes_cli import middleware
    from tools.file_tools import write_file_tool

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "allow"
    )
    profile = tmp_path / ".hermes"
    profile.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile))

    result = middleware.run_tool_execution_middleware(
        "write_file",
        {"path": str(profile / ".env"), "content": "SECRET=value"},
        lambda args: write_file_tool(**args),
    )

    assert not (profile / ".env").exists()
    assert "write denied" in json.loads(result)["error"].lower()


def test_ask_policy_honors_cron_deny(monkeypatch):
    from hermes_cli import middleware

    monkeypatch.setattr(
        middleware, "resolve_tool_approval_policy", lambda _name: "ask"
    )
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr("tools.approval._get_cron_approval_mode", lambda: "deny")
    called = []

    result = middleware.run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda args: called.append(args)
    )

    assert called == []
    assert "cron jobs run without a user present" in json.loads(result)["error"]


def test_default_config_exposes_empty_profile_local_policy_mapping():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["approvals"]["tool_policies"] == {}
