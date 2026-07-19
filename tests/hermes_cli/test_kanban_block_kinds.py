"""Unit tests for parse_block_kind prefix auto-detection (CLI + tools UX)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_parse_block_kind_with_prefix():
    """Prefix in reason (no explicit kind) sets kind and strips it from reason."""
    assert kb.parse_block_kind("review-required: please review the ACL") == (
        "review-required",
        "please review the ACL",
    )
    assert kb.parse_block_kind("needs_input: which key?") == ("needs_input", "which key?")
    assert kb.parse_block_kind("dependency: wait for X") == ("dependency", "wait for X")


def test_parse_block_kind_case_insensitive_prefix():
    assert kb.parse_block_kind("Review-Required: foo") == ("review-required", "foo")
    assert kb.parse_block_kind("NEEDS_INPUT:Bar") == ("needs_input", "Bar")


def test_parse_block_kind_empty_remainder_after_prefix():
    """kind: or kind:   (after strip) -> kind set, reason=None."""
    assert kb.parse_block_kind("review-required:") == ("review-required", None)
    assert kb.parse_block_kind("review-required:   ") == ("review-required", None)
    assert kb.parse_block_kind("transient: \t\n") == ("transient", None)


def test_parse_block_kind_unprefixed_reason():
    """No prefix and no kind -> (None, stripped_reason)."""
    assert kb.parse_block_kind("plain reason here") == (None, "plain reason here")
    assert kb.parse_block_kind("  spaced  ") == (None, "spaced")
    assert kb.parse_block_kind("") == (None, None)
    assert kb.parse_block_kind(None) == (None, None)
    assert kb.parse_block_kind("   ") == (None, None)


def test_parse_block_kind_explicit_kind_wins():
    """Explicit kind always returned as-is; reason untouched even if it looks prefixed."""
    k, r = kb.parse_block_kind("needs_input: foo", kind="review-required")
    assert k == "review-required"
    assert r == "needs_input: foo"

    k, r = kb.parse_block_kind("review-required: bar", kind="dependency")
    assert k == "dependency"
    assert r == "review-required: bar"

    # kind=None (explicit or default) means "no kind provided" -> auto-detect
    k, r = kb.parse_block_kind("review-required: x", kind=None)
    assert k == "review-required"
    assert r == "x"


def test_parse_block_kind_whitespace_handling():
    """Outer whitespace stripped; space after ':' is trimmed from rest; space before ':' prevents prefix match."""
    # space after : is ok (rest.strip)
    assert kb.parse_block_kind("needs_input:  foo bar  ") == ("needs_input", "foo bar")
    # no space between kind and :
    assert kb.parse_block_kind("capability:foo") == ("capability", "foo")
    # space before : -> treated as unprefixed (no match)
    res = kb.parse_block_kind("needs_input : bar")
    assert res == (None, "needs_input : bar")
    # leading/trailing on whole
    assert kb.parse_block_kind("  dependency: wait  ") == ("dependency", "wait")


def test_parse_block_kind_explicit_kind_with_whitespace_reason():
    # explicit kind leaves reason exactly, including odd whitespace
    k, r = kb.parse_block_kind("  weird: thing ", kind="transient")
    assert k == "transient"
    assert r == "  weird: thing "  # note: no outer strip when kind explicit


@pytest.mark.parametrize(
    "status,kind,expected",
    [
        ("blocked", "review-required", True),
        ("scheduled", "needs_input", True),
        ("triage", "capability", True),
        ("todo", "dependency", True),
        ("todo", "review-required", False),  # residue only
        ("ready", "review-required", False),
        ("running", "review-required", False),
        ("done", "review-required", False),
        ("blocked", None, False),
        ("ready", None, False),
    ],
)
def test_task_block_kind_is_active(status, kind, expected):
    assert kb.task_block_kind_is_active(status, kind) is expected


def test_loop_detection_survives_approve_then_reblock(kanban_home):
    """Option A: approve keeps kind/recurrences; same-kind re-block still climbs."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="loop", assignee="a")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="need eyes", kind="review-required")
        t1 = kb.get_task(conn, tid)
        assert t1.status == "blocked"
        assert t1.block_kind == "review-required"
        assert t1.block_recurrences == 1
        assert t1.is_active_block() is True

        ok, _ = kb.approve_task(conn, tid, actor="Evan", reason="ship it")
        assert ok
        t2 = kb.get_task(conn, tid)
        assert t2.status == "ready"
        assert t2.block_kind == "review-required"
        assert t2.block_recurrences == 1
        assert t2.is_active_block() is False

        kb.claim_task(conn, tid)
        # Second same-kind block should hit BLOCK_RECURRENCE_LIMIT (default 2)
        # and route to triage.
        assert kb.block_task(conn, tid, reason="still need eyes", kind="review-required")
        t3 = kb.get_task(conn, tid)
        assert t3.block_kind == "review-required"
        assert t3.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
        assert t3.status == "triage"
        assert t3.is_active_block() is True
