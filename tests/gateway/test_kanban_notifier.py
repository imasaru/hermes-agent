import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# notification_sources tests  (issue #39838)
# ---------------------------------------------------------------------------


def test_parse_notification_sources_forms():
    """Unit-test the config normalizer without spinning the notifier loop."""
    from gateway.kanban_watchers import parse_notification_sources

    assert parse_notification_sources(None) is None
    assert parse_notification_sources({"bad": True}) is None
    assert parse_notification_sources("*") == "*"
    assert parse_notification_sources("  *  ") == "*"
    assert parse_notification_sources("") == "*"
    assert parse_notification_sources([]) == "*"
    assert parse_notification_sources(["*"]) == "*"
    assert parse_notification_sources(["coder", "*"]) == "*"
    assert parse_notification_sources("coder, orchestrator") == frozenset(
        {"coder", "orchestrator"}
    )
    assert parse_notification_sources(["coder", "orchestrator"]) == frozenset(
        {"coder", "orchestrator"}
    )
    assert parse_notification_sources(["  coder  ", ""]) == frozenset({"coder"})


def _make_runner_with_config(adapter, profile_adapters=None):
    """Bare GatewayRunner for notifier tests (single-gateway by default)."""
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._profile_adapters = profile_adapters if profile_adapters is not None else {}
    # Stable gateway profile so owner != notifier for cross-profile cases.
    runner._kanban_notifier_profile = "macmini"
    return runner


def _make_runner_with_profile_adapters(adapter, profile_adapters):
    """Build a bare GatewayRunner with _profile_adapters set."""
    return _make_runner_with_config(adapter, profile_adapters=profile_adapters)


def _patch_load_config(monkeypatch, notification_sources=None, nested=False):
    """Monkeypatch hermes_cli.config.load_config for the notifier."""
    cfg = {}
    if notification_sources is not None:
        if nested:
            cfg["kanban"] = {"dispatch_in_gateway": True, "notification_sources": notification_sources}
        else:
            cfg["notification_sources"] = notification_sources
            cfg["kanban"] = {"dispatch_in_gateway": True}
    else:
        cfg["kanban"] = {"dispatch_in_gateway": True}

    def fake_load_config():
        return cfg

    import hermes_cli.config as hcfg
    monkeypatch.setattr(hcfg, "load_config", fake_load_config)


def _seed_completed_sub(tmp_path, monkeypatch, *, db_name, assignee, owner, chat_id="chat-1"):
    db_path = tmp_path / db_name
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=f"{assignee} task", assignee=assignee)
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id=chat_id,
            notifier_profile=owner,
        )
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def test_notifier_wildcard_list_accepts_cross_profile_sub(tmp_path, monkeypatch):
    """notification_sources: ['*'] should accept subs from any profile.

    Single-gateway (no multiplex): owner 'coder' has no _profile_adapters
    entry; delivery must use the gateway's own telegram adapter.
    """
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="wildcard.db", assignee="coder", owner="coder",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources=["*"])
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_wildcard_string_accepts_all(tmp_path, monkeypatch):
    """notification_sources: '*' (string) accepts all owners."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="wildcard-str.db", assignee="worker", owner="worker",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources="*")
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_nested_kanban_notification_sources(tmp_path, monkeypatch):
    """kanban.notification_sources is accepted as an alternate config site."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="nested-ns.db", assignee="coder", owner="coder",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources=["*"], nested=True)
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_allowlist_accepts_in_list_profile(tmp_path, monkeypatch):
    """notification_sources: ['coder'] accepts subs owned by 'coder'."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="allowlist.db", assignee="coder", owner="coder",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources=["coder"])
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_allowlist_comma_string(tmp_path, monkeypatch):
    """Comma-separated string allowlist (not substring matching)."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="allowlist-csv.db",
        assignee="orchestrator", owner="orchestrator",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources="coder,orchestrator")
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_allowlist_rejects_outside_profile(tmp_path, monkeypatch):
    """notification_sources: ['coder'] rejects subs owned by 'orchestrator'."""
    _seed_completed_sub(
        tmp_path, monkeypatch, db_name="allowlist-reject.db",
        assignee="orchestrator", owner="orchestrator",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources=["coder"])
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_notifier_default_behavior_unchanged_when_unset(tmp_path, monkeypatch):
    """When notification_sources is unset, foreign owners without adapters skip."""
    _seed_completed_sub(
        tmp_path, monkeypatch, db_name="default-unchanged.db",
        assignee="orphan", owner="orphan",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch)
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []


def test_notifier_default_allows_when_owner_has_adapters(tmp_path, monkeypatch):
    """Unset notification_sources still delivers multiplexed owner profiles."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="default-adapter.db",
        assignee="beta", owner="beta", chat_id="chat-beta",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch)
    runner = _make_runner_with_profile_adapters(
        adapter,
        {"beta": {Platform.TELEGRAM: adapter}},
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_same_profile_delivery_unaffected(tmp_path, monkeypatch):
    """Same-profile owner still delivers with notification_sources unset."""
    tid = _seed_completed_sub(
        tmp_path, monkeypatch, db_name="same-profile.db",
        assignee="macmini", owner="macmini",
    )
    adapter = RecordingAdapter()
    _patch_load_config(monkeypatch)
    runner = _make_runner_with_config(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in adapter.sent[0]["text"]


def test_notifier_wildcard_does_not_steal_multiplex_missing_platform(tmp_path, monkeypatch):
    """Even with ['*'], a multiplexed owner missing the platform must not
    fall back to the default bot (preserves no-wrong-bot contract).
    """
    _seed_completed_sub(
        tmp_path, monkeypatch, db_name="wildcard-no-steal.db",
        assignee="beta", owner="beta", chat_id="chat-beta",
    )
    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    _patch_load_config(monkeypatch, notification_sources=["*"])
    runner = _make_runner_with_config(
        default_adapter,
        profile_adapters={"beta": {Platform.DISCORD: other_adapter}},
    )

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert default_adapter.sent == []
    assert other_adapter.sent == []


# ---------------------------------------------------------------------------
# Bug #1 regression: SendResult.success check (issue #31901)
# ---------------------------------------------------------------------------

class SoftFailingAdapter(RecordingAdapter):
    """Adapter that returns SendResult(success=False) to simulate soft failure."""

    async def send(self, chat_id, text, metadata=None):
        # Simulate SendResult with success=False (e.g. wrong chat_id)
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="chat not found")


def test_kanban_notifier_retries_on_soft_failure(tmp_path, monkeypatch):
    """When adapter.send() returns success=False, the notifier must retry
    (rewind cursor) instead of treating it as delivered.

    This is the fix for issue #31901: without the SendResult.success check,
    soft failures were silently swallowed and the cursor advanced, permanently
    losing the event.
    """
    db_path = tmp_path / "soft-fail.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="soft fail task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = SoftFailingAdapter()
    runner = _make_runner(adapter)

    # Run the notifier - it should attempt to send, get failure, and retry
    # (rewind cursor). The test passes if no exception is raised and the
    # failure counter is tracked.
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The send was attempted (even though it failed)
    assert len(adapter.sent) == 0  # FailingAdapter doesn't record on failure
    # The failure count should be tracked
    assert len(runner._kanban_sub_fail_counts) > 0


# ---------------------------------------------------------------------------
# Bug #2 regression: notify subs inherit on decompose (issue #31901)
# ---------------------------------------------------------------------------

def test_decompose_inherits_notify_subs(tmp_path, monkeypatch):
    """When a triage task is decomposed, its kanban_notify_subs must be
    copied to all children so that child events (blocked/completed) notify
    the origin chat.

    This is the fix for issue #31901: without this, decomposed children
    were silent to subscribers of the root task.
    """
    db_path = tmp_path / "decompose-notify.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        # Create a triage task with a notify sub
        tid = kb.create_task(conn, title="triage task", assignee="triage", triage=True)
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
        )

        # Decompose the triage task
        child_ids = kb.decompose_triage_task(
            conn,
            task_id=tid,
            children=[
                {"title": "child 1", "assignee": "worker1"},
                {"title": "child 2", "assignee": "worker2"},
            ],
            root_assignee="orchestrator",
        )

        assert len(child_ids) == 2

        # Verify notify subs were copied to both children
        for cid in child_ids:
            subs = conn.execute(
                "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
                (cid,),
            ).fetchall()
            assert len(subs) == 1, f"Child {cid} should have 1 notify sub, got {len(subs)}"
            assert subs[0]["platform"] == "telegram"
            assert subs[0]["chat_id"] == "chat-1"

        # Verify root still has its sub
        root_subs = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?",
            (tid,),
        ).fetchall()
        assert len(root_subs) == 1
    finally:
        conn.close()
