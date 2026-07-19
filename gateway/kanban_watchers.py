"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agent.i18n import t

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


def parse_notification_sources(
    raw: Any,
) -> Union[None, str, frozenset[str]]:
    """Normalize ``notification_sources`` from config for the kanban notifier.

    Documented (kanban-worker SKILL / issue #39838):

    - ``['*']`` or ``\"*\"`` (or a list/string containing ``*``) → accept
      subscriptions owned by any profile.
    - ``['coder', 'orchestrator']`` or ``\"coder,orchestrator\"`` → allowlist.
    - unset / missing / unparseable → ``None`` (default isolation: only the
      gateway's own profile, or owners that already have multiplex adapters).

    Empty string / empty list is treated as wildcard so ``notification_sources:
    '*'`` and ``notification_sources: []`` both "open the gate" when the
    operator is intentionally clearing the default isolation.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        return None
    if not parts or any(p == "*" for p in parts):
        return "*"
    return frozenset(parts)


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """Resolve the live (enabled, per_tick) auto-decompose settings.

    Read fresh from config on every dispatcher tick (#49638) so that flipping
    ``kanban.auto_decompose: false`` to STOP runaway fan-out takes effect on the
    next tick instead of requiring a gateway restart. Auto-decompose is a
    safety toggle — a user who sees it create and launch tasks they didn't
    intend reaches for this flag to halt it, and a stale boot-captured value
    silently ignoring that change is the bug reported in #49638.

    Fails **safe**: if the config read raises, return ``(False, 3)`` — a
    transient read error must never re-enable a feature the user turned off,
    nor fall back to the burst-prone default-on behaviour. ``per_tick`` is
    clamped to ``>= 1``.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take an exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway process machine-wide may run the embedded kanban
    dispatcher: concurrent dispatchers double the reclaim frequency (each
    runs its own ``release_stale_claims`` → promote → dispatch loop), double
    claim-attempt events in the event log, and — with ``wal_autocheckpoint=0`` —
    concurrent manual WAL checkpoints can corrupt index pages. The
    ``dispatch_in_gateway`` config flag is the primary control; this lock is the
    backstop that survives config drift and same-profile restart races.

    Delegates to :func:`gateway.status._try_acquire_file_lock` (``fcntl`` on
    POSIX, ``msvcrt`` on Windows) so the guard is cross-platform.

    Returns ``(handle, "held")`` on success — the caller keeps the file handle
    for the process lifetime and **must** release it via
    :func:`_release_singleton_lock` when done. ``(None, "contended")`` when
    another process holds the lock (caller must NOT dispatch). ``(None,
    "unavailable")`` when locking cannot be performed (non-POSIX filesystem
    without flock, or the status.py helpers are unimportable) — caller falls
    back to config-only control.
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a dispatcher singleton lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass



def _zulip_approval_topic(task_id: str, title: Optional[str] = None) -> str:
    """Build a Zulip topic name: ``t_xxx — Title`` capped at 60 chars.

    Zulip's topic limit is 60. Always keep the full task id prefix so bare
    ``/approve`` in the topic can recover the id even when the title is
    truncated.
    """
    tid = (task_id or "").strip()
    if not tid:
        return "hermes-approval"
    title = (title or "").strip().replace("\n", " ")
    while "  " in title:
        title = title.replace("  ", " ")
    if not title:
        return tid[:60]
    sep = " — "
    budget = 60 - len(tid) - len(sep)
    if budget < 1:
        return tid[:60]
    if len(title) > budget:
        if budget <= 1:
            title = title[:budget]
        else:
            title = title[: max(1, budget - 1)].rstrip() + "…"
    return f"{tid}{sep}{title}"


def _chunk_text_for_platform(text: str, *, limit: int = 9000) -> list[str]:
    """Split long approval pings so Zulip/Telegram stay under message caps.

    Prefers paragraph boundaries; falls back to hard cuts. Each chunk is at
    most ``limit`` characters.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            parts.append(rest)
            break
        cut = rest[:limit]
        # Prefer double newline, then single, then space
        br = cut.rfind("\n\n")
        if br < limit // 3:
            br = cut.rfind("\n")
        if br < limit // 3:
            br = cut.rfind(" ")
        if br < limit // 3:
            br = limit
        chunk = rest[:br].rstrip()
        parts.append(chunk)
        rest = rest[br:].lstrip()
    # Annotate multi-part
    if len(parts) > 1:
        n = len(parts)
        parts = [f"{p}\n\n_({i+1}/{n})_" for i, p in enumerate(parts)]
    return parts


def _format_human_review_block_message(
    *,
    board_tag: str,
    tag: str,
    task_id: str,
    block_kind: Optional[str],
    reason: str,
    task,
    recent_comments: Optional[list] = None,
    platform: str = "",
) -> list[str]:
    """Rich approval ping(s) for needs_input / capability / review-required.

    Returns one or more message strings. Includes the **full** task body so
    reviewers can decide entirely from chat (Zulip topic / Telegram).
    Long payloads are split to respect platform message limits.
    """
    kind_label = block_kind or "human-review"
    task_title = ""
    task_workspace = ""
    task_body = ""
    if task is not None:
        task_title = task.title or ""
        task_workspace = task.workspace_path or ""
        task_body = (getattr(task, "body", None) or "") or ""

    reason_full = ""
    if reason:
        r = reason[2:] if reason.startswith(": ") else reason
        reason_full = r.strip()

    header: list[str] = [
        f"⏸ {board_tag}{tag}Kanban `{task_id}` blocked ({kind_label})",
    ]
    if task_title:
        header.append(f"**{task_title}**")
    if reason_full:
        header.append("")
        header.append(f"**Why blocked:** {reason_full}")

    body_section: list[str] = []
    if task_body.strip():
        body_section.append("")
        body_section.append("**Full task body:**")
        body_section.append(task_body.strip())

    comments_section: list[str] = []
    if recent_comments:
        comments_section.append("")
        comments_section.append("**Comments:**")
        for c in recent_comments:
            author = getattr(c, "author", None) or (
                c.get("author") if isinstance(c, dict) else "?"
            )
            cbody = getattr(c, "body", None) or (
                c.get("body") if isinstance(c, dict) else ""
            )
            cbody = (cbody or "").strip()
            if not cbody:
                continue
            comments_section.append(f"- **{author}:** {cbody}")

    footer: list[str] = []
    if task_workspace:
        footer.append("")
        footer.append(f"_Workspace:_ `{task_workspace}`")
    footer.append("")
    if block_kind == "review-required":
        footer.append(
            "Review required — approve promotes to ready and can respawn the worker."
        )
    elif block_kind == "needs_input":
        footer.append(
            "Needs your input — reply with a decision (or approve/deny)."
        )
    footer.append("")
    footer.append("**Reply in this topic (natural language OK):**")
    footer.append("- `lgtm! <notes for worker>` or `yes` / `approve`")
    footer.append("- `deny <reason>` or `no, <reason>`")
    footer.append("")
    footer.append("**Commands:**")
    footer.append(f"- `/kanban approve {task_id}`")
    footer.append(f'- `/kanban deny {task_id} "reason"`')
    footer.append(f"- `/kanban show {task_id}`")

    # Assemble: if everything fits one message, single part; else
    # part1 = header + start of body, continue body, end with comments+footer.
    full = "\n".join(header + body_section + comments_section + footer)
    plat = (platform or "").lower()
    # Zulip ~10k; Telegram ~4096. Stay under with margin.
    limit = 3500 if plat == "telegram" else 9000
    if len(full) <= limit:
        return [full]

    # Multi-part: (1) header + body chunk(s) (2) comments+footer if needed
    parts_out: list[str] = []
    head = "\n".join(header) + "\n\n**Full task body:**\n"
    body = task_body.strip() if task_body.strip() else "_(no body)_"
    tail = "\n".join(comments_section + footer)

    # Budget for body in first chunk after head
    # Put body in its own chunks, then tail
    body_chunks = _chunk_text_for_platform(body, limit=max(500, limit - len(head) - 80))
    for i, bc in enumerate(body_chunks):
        if i == 0:
            parts_out.append(head + bc)
        else:
            parts_out.append(
                f"⏸ `{task_id}` body continued:\n\n{bc}"
            )
    if tail.strip():
        tail_chunks = _chunk_text_for_platform(tail.strip(), limit=limit)
        parts_out.extend(tail_chunks)

    # Re-number parts
    n = len(parts_out)
    if n > 1:
        parts_out = [
            (p if p.endswith(f"({i+1}/{n})_") else f"{p}\n\n_({i+1}/{n})_")
            for i, p in enumerate(parts_out)
        ]
    return parts_out


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        # Notifier can run independently of the embedded dispatcher so cron-driven
        # dispatch (dispatch_in_gateway=false) still delivers approval pings to
        # Telegram/Zulip. Precedence:
        #   1. HERMES_KANBAN_NOTIFIER_IN_GATEWAY env (on/off)
        #   2. kanban.notifier_in_gateway config bool
        #   3. fall back to kanban.dispatch_in_gateway (legacy coupling)
        #   4. HERMES_KANBAN_DISPATCH_IN_GATEWAY env as legacy off-switch
        env_notifier = os.environ.get("HERMES_KANBAN_NOTIFIER_IN_GATEWAY", "").strip().lower()
        env_dispatch = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_notifier in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_NOTIFIER_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if env_notifier in {"1", "true", "yes", "on"}:
            notifier_enabled = True
        elif isinstance(kanban_cfg, dict) and "notifier_in_gateway" in kanban_cfg:
            notifier_enabled = bool(kanban_cfg.get("notifier_in_gateway"))
        elif env_dispatch in {"0", "false", "no", "off"}:
            notifier_enabled = False
        else:
            notifier_enabled = bool(kanban_cfg.get("dispatch_in_gateway", True))
        if not notifier_enabled:
            logger.info(
                "kanban notifier: disabled (set kanban.notifier_in_gateway=true "
                "to deliver approvals while dispatch_in_gateway=false)"
            )
            return
        logger.info(
            "kanban notifier: enabled (dispatch_in_gateway=%s notifier_in_gateway=%s)",
            kanban_cfg.get("dispatch_in_gateway", True) if isinstance(kanban_cfg, dict) else True,
            kanban_cfg.get("notifier_in_gateway") if isinstance(kanban_cfg, dict) else None,
        )

        # Load notification_sources allowlist (or wildcard) so the notifier
        # can accept cross-profile Kanban subscriptions.  See kanban-worker
        # SKILL.md and issue #39838. Prefer top-level key (documented); also
        # accept nested ``kanban.notification_sources`` for discoverability.
        ns_raw = None
        if isinstance(cfg, dict):
            ns_raw = cfg.get("notification_sources")
            if ns_raw is None and isinstance(kanban_cfg, dict):
                ns_raw = kanban_cfg.get("notification_sources")
        _notification_sources = parse_notification_sources(ns_raw)

        # Read kanban.approvals.auto_subscribe config option (default True)
        # When enabled, tasks that block for human-review kinds (needs_input,
        # capability, review-required) are auto-subscribed so the creator
        # receives approval notifications without manual /kanban subscribe.
        auto_subscribe_enabled = True  # default on
        if isinstance(kanban_cfg, dict):
            approvals_cfg = kanban_cfg.get("approvals", {})
            if isinstance(approvals_cfg, dict):
                auto_subscribe_enabled = bool(approvals_cfg.get("auto_subscribe", True))
            else:
                # Backward compat: approvals_cfg might be a bool directly
                auto_subscribe_enabled = bool(approvals_cfg)
        logger.debug(
            "kanban notifier: auto_subscribe=%s", auto_subscribe_enabled,
        )

        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        # "status" covers dashboard drag-drop and `_set_status_direct()`
        # writes — surface those transitions to subscribers too.
        TERMINAL_KINDS = (
            "completed", "blocked", "gave_up", "crashed", "timed_out",
            "status", "archived", "unblocked", "approved", "denied",
        )
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    # Apply notification_sources when configured
                                    # (wildcard or allowlist). When unset, keep
                                    # default isolation: only deliver if this
                                    # gateway already has multiplex adapters for
                                    # the owner profile.
                                    if _notification_sources is None:
                                        _owner_adapters = getattr(
                                            self, "_profile_adapters", {}
                                        ).get(owner_profile)
                                        if not _owner_adapters:
                                            logger.debug(
                                                "kanban notifier: subscription for %s owned by profile %s; current profile %s has no adapter for it, skipping",
                                                sub.get("task_id"), owner_profile, notifier_profile,
                                            )
                                            continue
                                    elif _notification_sources == "*":
                                        pass  # accept all profiles
                                    elif owner_profile not in _notification_sources:
                                        logger.debug(
                                            "kanban notifier: subscription for %s owned by profile %s; not in notification_sources allowlist, skipping",
                                            sub.get("task_id"), owner_profile,
                                        )
                                        continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=TERMINAL_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "board": slug,
                                })
                        finally:
                            conn.close()
                    return deliveries

                # Home-channel auto-subscribe FIRST (before claim) so
                # unsubscribed human-review blocks get a sub and are
                # included in this tick's claim/delivery pass. Zulip
                # subs use thread_id=task_id (dedicated topic).
                if auto_subscribe_enabled:
                    try:
                        await asyncio.to_thread(
                            self._kanban_home_auto_sub_unsubscribed_blocks,
                            auto_subscribe_enabled,
                        )
                    except Exception as _hs_exc:
                        logger.warning(
                            "kanban notifier: home auto-sub pass failed: %s",
                            _hs_exc,
                        )

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = self._authorization_adapter(plat, sub_profile or None)
                    # Single-gateway + notification_sources: subs stamped by a
                    # worker profile (coder/orchestrator/…) typically have no
                    # multiplex registry entry. When the config explicitly
                    # allows that owner AND the owner has no multiplex map at
                    # all, deliver via this gateway's connected adapters so
                    # multi-profile board alerts reach the user (#39838).
                    # If the owner IS multiplexed but missing this platform,
                    # fail closed (do not borrow the default bot) — same
                    # contract as _authorization_adapter.
                    #
                    # Same-profile case: when sub_profile == notifier_profile
                    # the sub was stamped by this gateway's own worker, so
                    # always deliver via self.adapters[plat] regardless of
                    # _notification_sources (preserves default behaviour).
                    if adapter is None and sub_profile:
                        profile_adapters = getattr(self, "_profile_adapters", None) or {}
                        if sub_profile == notifier_profile:
                            # Same-profile: always deliver via this gateway's
                            # own adapters (default isolation preserved).
                            adapters = getattr(self, "adapters", None) or {}
                            adapter = adapters.get(plat)
                        elif (
                            _notification_sources is not None
                            and (
                                _notification_sources == "*"
                                or sub_profile in _notification_sources
                            )
                            and sub_profile not in profile_adapters
                        ):
                            # notification_sources allows this owner and the
                            # owner has no multiplex entry → deliver via this
                            # gateway's adapter.
                            adapters = getattr(self, "adapters", None) or {}
                            adapter = adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    board_tag = f"[{board_slug}] " if board_slug else ""
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        # Default False for every event kind. Only the
                        # blocked branch may set this True; the auto-sub
                        # check below reads it for all kinds, so leaving
                        # it unbound crashes the whole tick (UnboundLocalError)
                        # and silently drops every notification.
                        is_approval_block = False
                        extra_msgs: list[str] = []
                        if kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                            msg = (
                                f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            block_kind = None
                            if ev.payload:
                                if ev.payload.get("reason"):
                                    reason = f": {str(ev.payload['reason'])[:160]}"
                                block_kind = ev.payload.get("kind")
                            # Check if this is a human-review block (needs_input,
                            # capability, review-required). For those kinds, render
                            # richer formatting with actionable approval commands.
                            is_approval_block = block_kind in {
                                "needs_input", "capability", "review-required",
                            }
                            if is_approval_block:
                                # Full reason text for the formatter (not the
                                # ultra-short ": …" used in one-liners).
                                reason_raw = ""
                                if ev.payload and ev.payload.get("reason"):
                                    reason_raw = str(ev.payload["reason"])
                                recent_comments = []
                                if task is not None:
                                    try:
                                        from hermes_cli import kanban_db as _kb_c
                                        # Delivery phase has no open conn; open briefly.
                                        _bc = _kb_c.connect(board=board_slug)
                                        try:
                                            recent_comments = _kb_c.list_comments(
                                                _bc, sub["task_id"],
                                            )
                                        finally:
                                            _bc.close()
                                    except Exception:
                                        recent_comments = []
                                msg_parts = _format_human_review_block_message(
                                    board_tag=board_tag,
                                    tag=tag,
                                    task_id=sub["task_id"],
                                    block_kind=block_kind,
                                    reason=reason_raw,
                                    task=task,
                                    recent_comments=recent_comments,
                                    platform=platform_str,
                                )
                                # First part is the primary msg for failure/
                                # logging paths below; extras sent after.
                                msg = msg_parts[0] if msg_parts else ""
                                extra_msgs = msg_parts[1:] if len(msg_parts) > 1 else []
                            else:
                                msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "approved":
                            actor = ""
                            reason_txt = ""
                            if ev.payload:
                                if ev.payload.get("actor"):
                                    actor = f" by {ev.payload['actor']}"
                                if ev.payload.get("reason"):
                                    reason_txt = f": {str(ev.payload['reason'])[:160]}"
                            msg = (
                                f"✅ {board_tag}{tag}Kanban {sub['task_id']} approved{actor}"
                                f"{reason_txt}"
                            )
                        elif kind == "denied":
                            actor = ""
                            reason_txt = ""
                            if ev.payload:
                                if ev.payload.get("actor"):
                                    actor = f" by {ev.payload['actor']}"
                                if ev.payload.get("reason"):
                                    reason_txt = f": {str(ev.payload['reason'])[:160]}"
                            msg = (
                                f"❌ {board_tag}{tag}Kanban {sub['task_id']} denied{actor}"
                                f"{reason_txt} (still blocked)"
                            )
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                f"✖ {board_tag}{tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {board_tag}{tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        elif kind == "status":
                            new_status = ""
                            if ev.payload and ev.payload.get("status"):
                                new_status = str(ev.payload["status"])
                            msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
                        else:
                            # archived / approval_requested / unblocked are
                            # claimed by TERMINAL_KINDS (so the cursor advances
                            # past them and they can't wedge a later
                            # completed/blocked event behind an unclaimed row)
                            # but are intentionally SILENT: an archive needs
                            # no user ping, approval_requested is redundant
                            # with the enriched blocked message above, and
                            # unblocked is an internal transition. They are
                            # also excluded from _WAKE_KINDS below, so they
                            # never wake the creator.
                            continue
                        # Auto-subscribe on human-review blocks if enabled.
                        # Delivery runs after the collector closes the board
                        # connection, so open a short-lived conn here.
                        if is_approval_block and kind == "blocked":
                            try:
                                _task_id = sub["task_id"]
                                _plat = platform_str
                                _chat = sub["chat_id"]
                                _thread = sub.get("thread_id")
                                _board = board_slug
                                _auto_cfg = auto_subscribe_enabled

                                def _auto_sub(
                                    task_id=_task_id,
                                    platform=_plat,
                                    chat_id=_chat,
                                    thread_id=_thread,
                                    board=_board,
                                    auto_cfg=_auto_cfg,
                                ):
                                    from hermes_cli import kanban_db as _kb
                                    c = _kb.connect(board=board)
                                    try:
                                        return self._kanban_auto_subscribe(
                                            c,
                                            task_id=task_id,
                                            platform=platform,
                                            chat_id=chat_id,
                                            thread_id=thread_id,
                                            board=board,
                                            auto_subscribe_cfg=auto_cfg,
                                        )
                                    finally:
                                        c.close()
                                await asyncio.to_thread(_auto_sub)
                            except Exception as _as_exc:
                                logger.warning(
                                    "kanban notifier: auto-subscribe check failed for %s: %s",
                                    sub["task_id"], _as_exc,
                                )
                        metadata: dict[str, Any] = {}
                        # Zulip: human-review approval pings go to a
                        # dedicated topic ``t_xxx — Title`` so context is
                        # obvious and bare /approve can recover the id.
                        if (
                            platform_str == "zulip"
                            and is_approval_block
                            and kind == "blocked"
                        ):
                            title = ""
                            if task is not None:
                                title = task.title or ""
                            ztopic = _zulip_approval_topic(sub["task_id"], title)
                            metadata["thread_id"] = ztopic
                            metadata["topic"] = ztopic
                        elif sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            result = await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            # Check SendResult.success to catch soft failures
                            # (e.g. wrong chat_id, platform soft-fail). Without
                            # this check, success=False was treated as delivered
                            # and the cursor advanced, permanently losing the
                            # event — the bug reported in #31901.
                            # Handle None for backward compatibility with
                            # adapters that don't return SendResult.
                            if result is not None and not result.success:
                                raise RuntimeError(
                                    f"send returned success=False: {result.error or 'unknown'}"
                                )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            for _em_i, _em in enumerate(extra_msgs):
                                try:
                                    _er = await adapter.send(
                                        sub["chat_id"], _em, metadata=metadata,
                                    )
                                    if _er is not None and not _er.success:
                                        logger.warning(
                                            "kanban notifier: extra part %s/%s failed for %s: %s",
                                            _em_i + 2, len(extra_msgs) + 1,
                                            sub["task_id"], getattr(_er, "error", None),
                                        )
                                except Exception as _em_exc:
                                    logger.warning(
                                        "kanban notifier: extra part send error for %s: %s",
                                        sub["task_id"], _em_exc,
                                    )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                        if _wake_kinds:
                            try:
                                _session_key = getattr(task, "session_id", None) or ""
                                if _session_key:
                                    _title = (task.title if task else sub["task_id"])[:120]
                                    _assignee = task.assignee if task else ""
                                    _parts = []
                                    if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                                    if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                                    if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                                    if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                                    if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                                    _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                                    _synth = t(
                                        "gateway.kanban.wake.message",
                                        task_id=sub["task_id"],
                                        status=_status,
                                        title=_title,
                                        assignee=_assignee,
                                        board=board_slug,
                                    )
                                    from gateway.session import SessionSource
                                    from gateway.platforms.base import MessageEvent, MessageType
                                    # KNOWN LIMITATION (tracked follow-up): the
                                    # subscription row does not persist the
                                    # creator's chat_type, and it is not carried
                                    # on the session-context bridge, so we cannot
                                    # faithfully reconstruct the creator's real
                                    # session key here. build_session_key() keys
                                    # DMs (":dm:<chat_id>") on a wholly different
                                    # shape from group/thread, so any hardcoded
                                    # value mis-routes some creators. "group" is
                                    # the least-surprising default for the
                                    # dashboard/group flows this wake primarily
                                    # serves; DM-originated creators are handled
                                    # by the follow-up that stamps + persists
                                    # chat_type end-to-end. handle_message()
                                    # get_or_create_session's the target, so a
                                    # mismatch degrades to "wake lands in a fresh
                                    # group session" — never an exception.
                                    _source = SessionSource(
                                        platform=plat,
                                        chat_id=sub["chat_id"],
                                        chat_type="group",
                                        thread_id=sub.get("thread_id") or None,
                                        user_id=sub.get("user_id"),
                                        profile=sub_profile or None,
                                    )
                                    _synth_event = MessageEvent(
                                        text=_synth,
                                        message_type=MessageType.TEXT,
                                        source=_source,
                                        internal=True,
                                    )
                                    await adapter.handle_message(_synth_event)
                                    logger.info(
                                        "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                        sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                    )
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)


    def _kanban_home_auto_sub_unsubscribed_blocks(
        self, auto_subscribe_cfg: bool,
    ) -> int:
        """Subscribe home channels to human-review blocks with no subscribers.

        For Zulip, uses ``thread_id = task_id`` so the notification opens a
        dedicated topic named after the ticket. Returns number of new subs.
        """
        if not auto_subscribe_cfg:
            return 0
        from hermes_cli import kanban_db as _kb
        from gateway.config import Platform as _Platform
        import time

        # Resolve home channels from connected adapters / config
        homes: list[tuple[str, str, str]] = []  # platform, chat_id, default_thread
        try:
            cfg = getattr(self, "config", None)
            platforms = getattr(cfg, "platforms", None) or {}
            for plat, pcfg in platforms.items():
                pstr = plat.value if hasattr(plat, "value") else str(plat)
                pstr = pstr.lower()
                hc = getattr(pcfg, "home_channel", None) if pcfg else None
                if hc is None and isinstance(pcfg, dict):
                    hc = pcfg.get("home_channel")
                chat_id = None
                thread = ""
                if hc is not None:
                    chat_id = getattr(hc, "chat_id", None) or (
                        hc.get("chat_id") if isinstance(hc, dict) else None
                    )
                    # Zulip topic default is empty here — we override per task
                    thread = getattr(hc, "thread_id", None) or (
                        hc.get("thread_id") if isinstance(hc, dict) else None
                    ) or ""
                if not chat_id:
                    # env fallbacks commonly used by plugins
                    import os
                    if pstr == "zulip":
                        chat_id = os.environ.get("ZULIP_HOME_CHANNEL") or ""
                    elif pstr == "telegram":
                        chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL") or ""
                if chat_id:
                    homes.append((pstr, str(chat_id), str(thread or "")))
        except Exception as exc:
            logger.debug("kanban notifier: home channel resolve failed: %s", exc)
            return 0

        if not homes:
            return 0

        now = int(time.time())
        # Only consider blocks from the last hour to avoid flooding old cards
        window = now - 3600
        created = 0
        try:
            boards = _kb.list_boards(include_archived=False)
        except Exception:
            boards = [{"slug": _kb.DEFAULT_BOARD}]

        for board_meta in boards:
            slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
            try:
                conn = _kb.connect(board=slug)
            except Exception:
                continue
            try:
                # Recent blocked events
                rows = conn.execute(
                    "SELECT e.task_id, e.payload, e.created_at FROM task_events e "
                    "WHERE e.kind = 'blocked' AND e.created_at >= ? "
                    "ORDER BY e.id DESC LIMIT 50",
                    (window,),
                ).fetchall()
                seen_tasks: set[str] = set()
                for r in rows:
                    tid = r["task_id"]
                    if tid in seen_tasks:
                        continue
                    seen_tasks.add(tid)
                    task = _kb.get_task(conn, tid)
                    if task is None or task.status != "blocked":
                        continue
                    kind = getattr(task, "block_kind", None)
                    if kind and kind not in _kb.HUMAN_REVIEW_KINDS:
                        continue
                    # payload kind fallback
                    if not kind:
                        try:
                            import json as _json
                            pl = _json.loads(r["payload"]) if r["payload"] else {}
                            kind = (pl or {}).get("kind")
                        except Exception:
                            kind = None
                        if kind and kind not in _kb.HUMAN_REVIEW_KINDS:
                            continue
                    existing = _kb.list_notify_subs(conn, task_id=tid)
                    if existing:
                        continue
                    for pstr, chat_id, thread in homes:
                        # Skip if adapter not connected
                        try:
                            plat = _Platform(pstr)
                            if not self.adapters.get(plat):
                                continue
                        except Exception:
                            continue
                        if pstr == "zulip":
                            t_obj = task  # already loaded
                            sub_thread = _zulip_approval_topic(
                                tid, getattr(t_obj, "title", None),
                            )
                        else:
                            sub_thread = thread
                        try:
                            _kb.add_notify_sub(
                                conn,
                                task_id=tid,
                                platform=pstr,
                                chat_id=chat_id,
                                thread_id=sub_thread or "",
                                notifier_profile=self._active_profile_name(),
                            )
                            created += 1
                            logger.info(
                                "kanban notifier: home auto-sub %s/%s topic=%s "
                                "task %s board %s",
                                pstr, chat_id, sub_thread or "-", tid, slug,
                            )
                        except Exception as exc:
                            logger.warning(
                                "kanban notifier: home auto-sub failed for %s: %s",
                                tid, exc,
                            )
            finally:
                conn.close()
        return created

    def _kanban_advance(

        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    def _kanban_auto_subscribe(
        self,
        conn,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: Optional[str],
        board: Optional[str],
        auto_subscribe_cfg: bool,
    ) -> bool:
        """Auto-subscribe the creator's home channel when a task blocks for human review.

        Called when a blocked event has a human-review kind (needs_input, capability,
        review-required). If the task has no existing subscriptions for this platform/chat,
        and auto_subscribe is enabled in config, creates a subscription so the user
        receives the approval notification without manual /kanban subscribe.

        Returns True if a new subscription was created, False otherwise.
        """
        from hermes_cli import kanban_db as _kb

        if not auto_subscribe_cfg:
            return False

        # Check if there are any existing subscriptions for this task
        existing_subs = _kb.list_notify_subs(conn, task_id=task_id)
        if not existing_subs:
            # No subscriptions at all - auto-subscribe
            try:
                _kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id or "",
                    notifier_profile=self._active_profile_name(),
                )
                logger.info(
                    "kanban notifier: auto-subscribed %s/%s to task %s on board %s",
                    platform, chat_id, task_id, board,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "kanban notifier: auto-subscribe failed for %s: %s",
                    task_id, exc,
                )
                return False
        else:
            # Check if any existing subscription matches this platform/chat
            for sub in existing_subs:
                if (sub.get("platform") == platform and
                    sub.get("chat_id") == chat_id and
                    (sub.get("thread_id") or "") == (thread_id or "")):
                    # Already subscribed - no action needed
                    return False
            # Different subscription exists - auto-subscribe this one too
            try:
                _kb.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id or "",
                    notifier_profile=self._active_profile_name(),
                )
                logger.info(
                    "kanban notifier: auto-subscribed %s/%s to task %s on board %s",
                    platform, chat_id, task_id, board,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "kanban notifier: auto-subscribe failed for %s: %s",
                    task_id, exc,
                )
                return False

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Re-read the auto-decompose toggle live each tick so a user
                # flipping kanban.auto_decompose=false to STOP runaway fan-out
                # takes effect on the next tick, not on gateway restart (#49638).
                _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                if _ad_enabled:
                    await asyncio.to_thread(_auto_decompose_tick, _ad_per_tick)
                results = await asyncio.to_thread(_tick_once)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                _release_singleton_lock(self._kanban_dispatcher_lock_handle)
                self._kanban_dispatcher_lock_handle = None
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        _release_singleton_lock(self._kanban_dispatcher_lock_handle)
        self._kanban_dispatcher_lock_handle = None
