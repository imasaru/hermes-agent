"""
Tests for subagent progress relay (issue #169).

Verifies that:
- KawaiiSpinner.print_above() works with and without active spinner
- _build_child_progress_callback handles CLI/gateway/no-display paths
- Thinking events are relayed correctly
- Parallel callbacks don't share state
"""

import io
import sys
import pytest
from unittest.mock import MagicMock

from agent.display import KawaiiSpinner
from tools.delegate_tool import _build_child_progress_callback


# =========================================================================
# KawaiiSpinner.print_above tests
# =========================================================================

class TestPrintAbove:
    """Tests for KawaiiSpinner.print_above method."""

    def test_print_above_without_spinner_running(self):
        """print_above should write to stdout even when spinner is not running."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf  # Redirect to buffer
        
        spinner.print_above("hello world")
        output = buf.getvalue()
        assert "hello world" in output

    def test_print_above_with_spinner_running(self):
        """print_above should clear spinner line and print text."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True  # Pretend spinner is running (don't start thread)
        
        spinner.print_above("tool line")
        output = buf.getvalue()
        assert "tool line" in output
        assert "\r" in output  # Should start with carriage return to clear spinner line

    def test_print_above_uses_captured_stdout(self):
        """print_above should use self._out, not sys.stdout.
        This ensures it works inside redirect_stdout(devnull)."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        
        # Simulate redirect_stdout(devnull)
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            spinner.print_above("should go to buf")
        finally:
            sys.stdout = old_stdout
        
        assert "should go to buf" in buf.getvalue()


# =========================================================================
# _build_child_progress_callback tests
# =========================================================================

class TestBuildChildProgressCallback:
    """Tests for child progress callback builder."""

    def test_returns_none_when_no_display(self):
        """Should return None when parent has no spinner or callback."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        assert cb is None

    def test_cli_spinner_tool_event(self):
        """Should print tool line above spinner for CLI path."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        assert cb is not None
        
        cb("tool.started", "web_search", "quantum computing", {})
        output = buf.getvalue()
        assert "web_search" in output
        assert "quantum computing" in output
        assert "├─" in output

    def test_cli_spinner_thinking_event(self):
        """Should print thinking line above spinner for CLI path."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("_thinking", "I'll search for papers first")
        
        output = buf.getvalue()
        assert "💭" in output
        assert "search for papers" in output

    def test_gateway_progress_streams_per_tool(self):
        """Gateway path: each tool.started relays a subagent.tool event AND a
        live subagent.progress summary, so a subagent window streams in step
        with the child's work instead of only flushing at end-of-run.

        Invariant under test: every tool start produces a paired
        (subagent.tool, subagent.progress) relay — no batching delay. This is
        the regression guard for "subagent output just appears all at once",
        which happened when the progress summary was buffered 5-deep and short
        subagents (0-4 tools) never reached the flush threshold mid-run.
        """
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Each tool.started relays subagent.tool immediately, then a live
        # subagent.progress summary right behind it (no accumulation delay).
        for i in range(3):
            cb("tool.started", f"tool_{i}", f"arg_{i}", {})

        events = [c.args[0] for c in parent_cb.call_args_list]
        # Live streaming: tool/progress pairs in order, one pair per tool.
        assert events == ["subagent.tool", "subagent.progress"] * 3

        # The most recent progress summary reflects the tool that just ran.
        summary_call = parent_cb.call_args_list[-1]
        summary_text = summary_call.kwargs.get("preview") or summary_call.args[2]
        assert "tool_2" in summary_text

    def test_thinking_relayed_to_gateway(self):
        """Thinking events are relayed as subagent.thinking events."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("_thinking", "some reasoning text")

        parent_cb.assert_called_once()
        assert parent_cb.call_args.args[0] == "subagent.thinking"
        assert parent_cb.call_args.args[2] == "some reasoning text"

    def test_parallel_callbacks_independent(self):
        """Each child's callback streams its own tool/progress relays
        independently — no cross-child interleaving in the per-child batch."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb0 = _build_child_progress_callback(0, "goal a", parent)
        cb1 = _build_child_progress_callback(1, "goal b", parent)

        # 3 tool.started per child. Each tool relays a subagent.tool plus a
        # live subagent.progress summary (batch size = 1, counted per-child).
        for i in range(3):
            cb0("tool.started", f"tool_{i}", f"a_{i}", {})
            cb1("tool.started", f"other_{i}", f"b_{i}", {})

        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events.count("subagent.tool") == 6
        # Each child's progress summary stays scoped to its own tools — child 0
        # never names child 1's tools and vice versa.
        progress_calls = [
            (c.kwargs.get("preview") or c.args[2])
            for c in parent_cb.call_args_list
            if c.args[0] == "subagent.progress"
        ]
        assert progress_calls, "expected live progress summaries to stream"
        assert all("other_" not in p for p in progress_calls if "tool_" in p)

    def test_task_index_prefix_in_batch_mode(self):
        """Batch mode (task_count > 1) should show 1-indexed prefix for all tasks."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        # task_index=0 in a batch of 3 → prefix "[1]"
        cb0 = _build_child_progress_callback(0, "test goal", parent, task_count=3)
        cb0("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[1]" in output

        # task_index=2 in a batch of 3 → prefix "[3]"
        buf.truncate(0)
        buf.seek(0)
        cb2 = _build_child_progress_callback(2, "test goal", parent, task_count=3)
        cb2("tool.started", "web_search", "test", {})
        output = buf.getvalue()
        assert "[3]" in output

    def test_single_task_no_prefix(self):
        """Single task (task_count=1) should not show index prefix."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("delegating")
        spinner._out = buf
        spinner.running = True
        
        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None
        
        cb = _build_child_progress_callback(0, "test goal", parent, task_count=1)
        cb("tool.started", "web_search", "test", {})
        
        output = buf.getvalue()
        assert "[" not in output


# =========================================================================
# Integration: thinking callback in run_agent.py
# =========================================================================

class TestThinkingCallback:
    """Tests for the _thinking callback in AIAgent conversation loop."""

    def _simulate_thinking_callback(self, content, callback, delegate_depth=1):
        """Simulate the exact code path from run_agent.py for the thinking callback.
        
        delegate_depth: simulates self._delegate_depth.
            0 = main agent (should NOT fire), >=1 = subagent (should fire).
        """
        import re
        if (content and callback and delegate_depth > 0):
            _think_text = content.strip()
            _think_text = re.sub(
                r'</?(?:REASONING_SCRATCHPAD|think|reasoning)>', '', _think_text
            ).strip()
            first_line = _think_text.split('\n')[0][:80] if _think_text else ""
            if first_line:
                try:
                    callback("_thinking", first_line)
                except Exception:
                    pass

    def test_thinking_callback_fires_on_content(self):
        """tool_progress_callback should receive _thinking event
        when assistant message has content."""
        calls = []
        self._simulate_thinking_callback(
            "I'll research quantum computing first, then summarize.",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert calls[0][0] == "_thinking"
        assert "quantum computing" in calls[0][1]

    def test_thinking_callback_skipped_when_no_content(self):
        """Should not fire when assistant has no content."""
        calls = []
        self._simulate_thinking_callback(
            None,
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0

    def test_thinking_callback_truncates_long_content(self):
        """Should truncate long content to 80 chars."""
        calls = []
        self._simulate_thinking_callback(
            "A" * 200 + "\nSecond line should be ignored",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert len(calls[0][1]) == 80

    def test_thinking_callback_skipped_for_main_agent(self):
        """Main agent (delegate_depth=0) should NOT fire thinking events.
        This prevents gateway spam on Telegram/Discord."""
        calls = []
        self._simulate_thinking_callback(
            "I'll help you with that request.",
            lambda name, preview=None: calls.append((name, preview)),
            delegate_depth=0,
        )
        assert len(calls) == 0

    def test_thinking_callback_strips_reasoning_scratchpad(self):
        """REASONING_SCRATCHPAD tags should be stripped before display."""
        calls = []
        self._simulate_thinking_callback(
            "<REASONING_SCRATCHPAD>I need to analyze this carefully</REASONING_SCRATCHPAD>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert "<REASONING_SCRATCHPAD>" not in calls[0][1]
        assert "analyze this carefully" in calls[0][1]

    def test_thinking_callback_strips_think_tags(self):
        """<think> tags should be stripped before display."""
        calls = []
        self._simulate_thinking_callback(
            "<think>Let me think about this problem</think>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 1
        assert "<think>" not in calls[0][1]
        assert "think about this problem" in calls[0][1]

    def test_thinking_callback_empty_after_strip(self):
        """Should not fire when content is only XML tags."""
        calls = []
        self._simulate_thinking_callback(
            "<REASONING_SCRATCHPAD></REASONING_SCRATCHPAD>",
            lambda name, preview=None: calls.append((name, preview))
        )
        assert len(calls) == 0


# =========================================================================
# Gateway batch flush tests
# =========================================================================

class TestBatchFlush:
    """Tests for gateway batch flush on subagent completion."""

    def test_progress_flushes_live_per_tool(self):
        """With per-tool streaming, each tool.started immediately emits its own
        subagent.progress summary — nothing is left buffered for end-of-run.

        This is the inverse of the old buffer-until-flush behavior: a live
        subagent window must see progress as each tool runs, not in one burst
        when the child finishes.
        """
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)

        # Each tool relays a paired subagent.tool + subagent.progress, live.
        cb("tool.started", "web_search", "query1", {})
        cb("tool.started", "read_file", "file.txt", {})
        cb("tool.started", "write_file", "out.txt", {})
        events = [c.args[0] for c in parent_cb.call_args_list]
        assert events == ["subagent.tool", "subagent.progress"] * 3

        # Nothing left to flush — the batch drained on each tool, so _flush is
        # a no-op (no trailing end-of-run summary).
        before = len(parent_cb.call_args_list)
        cb._flush()
        assert len(parent_cb.call_args_list) == before

    def test_flush_noop_when_batch_empty(self):
        """_flush should not send anything when batch is empty."""
        parent = MagicMock()
        parent._delegate_spinner = None
        parent_cb = MagicMock()
        parent.tool_progress_callback = parent_cb

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb._flush()
        parent_cb.assert_not_called()

    def test_flush_noop_when_no_parent_callback(self):
        """_flush should not crash when there's no parent callback."""
        buf = io.StringIO()
        spinner = KawaiiSpinner("test")
        spinner._out = buf
        spinner.running = True

        parent = MagicMock()
        parent._delegate_spinner = spinner
        parent.tool_progress_callback = None

        cb = _build_child_progress_callback(0, "test goal", parent)
        cb("tool.started", "web_search", "test", {})
        cb._flush()  # Should not crash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

