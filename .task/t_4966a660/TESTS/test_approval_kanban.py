"""Unit tests for kanban permission approval flow in tools/approval.py.

Tests cover:
1. _await_kanban_permission_approval() timeout path (the bug fix)
2. _await_kanban_permission_approval() approve path
3. _await_kanban_permission_approval() deny path
4. _await_kanban_permission_approval() file corruption handling
5. _forward_kanban_permission_approval() file write
6. _resolve_kanban_permission_approval() file update
7. resolve_kanban_permission_approvals() batch resolution
8. _get_kanban_approval_dir() path construction
9. Integration: full worker -> file -> gateway -> worker flow
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, "/Users/taka/.hermes/hermes-agent/tools")

import approval as ap


class TestGetKanbanApprovalDir:
    """Test _get_kanban_approval_dir path construction."""

    def test_default_path(self):
        with patch.dict(os.environ, {}, clear=True):
            d = ap._get_kanban_approval_dir()
            assert d == Path.home() / ".hermes" / "kanban" / "pending_approvals"

    def test_custom_env_var(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": "/tmp/test_kanban"}):
            d = ap._get_kanban_approval_dir()
            assert d == Path("/tmp/test_kanban") / "pending_approvals"

    def test_creates_directory(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": tmpdir}):
                d = ap._get_kanban_approval_dir()
                assert d.exists()
                assert d.is_dir()


class TestAwaitKanbanPermissionApproval:
    """Test _await_kanban_permission_approval polling and timeout."""

    def test_timeout_returns_deny(self, tmp_path):
        """BUG FIX: Timeout path must return a dict, not None.

        Previously this function had no return on timeout, causing
        the caller to crash on .get("resolved", False) with NoneType.
        """
        task_id = "t_test_timeout"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=1)
        assert result is not None, "Timeout path returned None - caller will crash"
        assert result["resolved"] is False
        assert result["choice"] == "deny"
        assert "timeout" in result["reason"].lower()

    def test_approve_path(self, tmp_path):
        task_id = "t_test_approve"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        pending_file = approval_dir / f"{task_id}.json"
        pending_file.write_text(json.dumps({"status": "pending", "task_id": task_id, "timestamp": time.time()}))
        time.sleep(0.1)
        pending_file.write_text(json.dumps({"status": "resolved", "task_id": task_id, "choice": "once", "reason": "looks good", "resolved_at": time.time()}))
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=5)
        assert result["resolved"] is True
        assert result["choice"] == "once"
        assert result["reason"] == "looks good"

    def test_deny_path(self, tmp_path):
        task_id = "t_test_deny"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        pending_file = approval_dir / f"{task_id}.json"
        pending_file.write_text(json.dumps({"status": "pending", "task_id": task_id, "timestamp": time.time()}))
        time.sleep(0.1)
        pending_file.write_text(json.dumps({"status": "resolved", "task_id": task_id, "choice": "deny", "reason": "too dangerous", "resolved_at": time.time()}))
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=5)
        assert result["resolved"] is True
        assert result["choice"] == "deny"
        assert result["reason"] == "too dangerous"

    def test_file_corruption_handled(self, tmp_path):
        task_id = "t_test_corrupt"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        pending_file = approval_dir / f"{task_id}.json"
        pending_file.write_text("not valid json {{{")
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=1)
        assert result is not None
        assert result["resolved"] is False

    def test_file_not_exists_handled(self, tmp_path):
        task_id = "t_test_no_file"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=1)
        assert result is not None
        assert result["resolved"] is False


class TestForwardKanbanPermissionApproval:
    """Test _forward_kanban_permission_approval file write."""

    def test_writes_pending_file(self, tmp_path):
        task_id = "t_test_forward"
        approval_data = {"command": "rm -rf /important", "pattern_key": "destructive_file_ops", "description": "Potentially destructive file operation"}
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            # kanban_db logging is best-effort (wrapped in try/except),
            # so we just verify the file write works
            ap._forward_kanban_permission_approval(task_id, approval_data)
        pending_file = tmp_path / "pending_approvals" / f"{task_id}.json"
        assert pending_file.exists()
        data = json.loads(pending_file.read_text())
        assert data["status"] == "pending"
        assert data["task_id"] == task_id
        assert data["approval"]["command"] == "rm -rf /important"

    def test_includes_timestamp(self, tmp_path):
        task_id = "t_test_ts"
        approval_data = {"command": "ls", "pattern_key": "safe_cmd", "description": "Safe command"}
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            ap._forward_kanban_permission_approval(task_id, approval_data)
        pending_file = tmp_path / "pending_approvals" / f"{task_id}.json"
        data = json.loads(pending_file.read_text())
        assert "timestamp" in data
        assert isinstance(data["timestamp"], float)


class TestResolveKanbanPermissionApproval:
    """Test _resolve_kanban_permission_approval file update."""

    def test_resolves_pending_file(self, tmp_path):
        task_id = "t_test_resolve"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        pending_file = approval_dir / f"{task_id}.json"
        pending_file.write_text(json.dumps({"status": "pending", "task_id": task_id, "timestamp": time.time()}))
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            result = ap._resolve_kanban_permission_approval(task_id, "once", "approved")
        assert result is True
        data = json.loads(pending_file.read_text())
        assert data["status"] == "resolved"
        assert data["choice"] == "once"
        assert data["reason"] == "approved"
        assert "resolved_at" in data

    def test_returns_false_for_nonexistent(self, tmp_path):
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            result = ap._resolve_kanban_permission_approval("t_nonexistent", "once")
        assert result is False


class TestResolveKanbanPermissionApprovals:
    """Test resolve_kanban_permission_approvals batch resolution."""

    def test_resolves_all_pending(self, tmp_path):
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        for i in range(3):
            task_id = f"t_batch_{i}"
            (approval_dir / f"{task_id}.json").write_text(json.dumps({"status": "pending", "task_id": task_id, "timestamp": time.time()}))
        (approval_dir / "t_done.json").write_text(json.dumps({"status": "resolved", "task_id": "t_done"}))
        (approval_dir / "readme.txt").write_text("not a task")
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            count = ap.resolve_kanban_permission_approvals("once", "batch approve")
        assert count == 3
        for i in range(3):
            task_id = f"t_batch_{i}"
            data = json.loads((approval_dir / f"{task_id}.json").read_text())
            assert data["status"] == "resolved"


class TestIntegration:
    """Integration test: full worker -> file -> gateway -> worker flow."""

    def test_full_approval_flow(self, tmp_path):
        task_id = "t_integration"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        approval_data = {"command": "rm -rf /tmp/test_data", "pattern_key": "destructive_file_ops", "description": "Potentially destructive file operation"}
        # Step 1: Worker forwards approval request
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            ap._forward_kanban_permission_approval(task_id, approval_data)
        pending_file = approval_dir / f"{task_id}.json"
        assert pending_file.exists()
        data = json.loads(pending_file.read_text())
        assert data["status"] == "pending"
        # Step 2: Gateway resolves (user approves)
        time.sleep(0.1)
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            ap._resolve_kanban_permission_approval(task_id, "once", "user approved")
        # Step 3: Worker polls and sees resolution
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=5)
        assert result["resolved"] is True
        assert result["choice"] == "once"
        assert result["reason"] == "user approved"

    def test_full_deny_flow(self, tmp_path):
        task_id = "t_integration_deny"
        approval_dir = tmp_path / "pending_approvals"
        approval_dir.mkdir()
        approval_data = {"command": "rm -rf /important", "pattern_key": "destructive_file_ops", "description": "Dangerous"}
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            ap._forward_kanban_permission_approval(task_id, approval_data)
        time.sleep(0.1)
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            ap._resolve_kanban_permission_approval(task_id, "deny", "too risky")
        with patch.dict(os.environ, {"HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path)}):
            with patch("tools.environments.base.touch_activity_if_due"):
                result = ap._await_kanban_permission_approval(task_id, timeout_seconds=5)
        assert result["resolved"] is True
        assert result["choice"] == "deny"
        assert result["reason"] == "too risky"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
