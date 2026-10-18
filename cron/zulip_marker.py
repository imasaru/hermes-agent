"""Cron-side Zulip topic-ingest marker.

One grammar. Prefer the plugin implementation when ``zulip.topic_ingest`` is
importable (gateway tests / a process that put the plugin on ``sys.path``).
A standalone ``hermes cron`` process often cannot import the plugin package
(loaded live as ``hermes_plugins.zulip*``), so this module keeps a matching
fallback. ``safe_append_cron_marker_for_delivery`` never raises: any failure
returns the original content and logs a warning.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("cron.scheduler")

CRON_MARKER_PREFIX = "-# 🕒"
_MARKER_LINE_RE = re.compile(
    r"^[ \t]*-#[ \t]*(?:🕒[ \t]*)?cron:(?P<job_id>[A-Za-z0-9._-]+)[ \t]+"
    r"run:(?P<run_id>[A-Za-z0-9._-]+)[ \t]*$",
    re.MULTILINE,
)
_MARKER_INLINE_RE = re.compile(
    r"-#[ \t]*(?:🕒[ \t]*)?cron:(?P<job_id>[A-Za-z0-9._-]+)[ \t]+"
    r"run:(?P<run_id>[A-Za-z0-9._-]+)"
)


@dataclass(frozen=True)
class CronMarker:
    job_id: str
    run_id: str

    def line(self) -> str:
        return f"{CRON_MARKER_PREFIX} cron:{self.job_id} run:{self.run_id}"


def _sanitize_marker_id(value: str) -> str:
    text = (value or "").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")


def format_cron_marker(job_id: str, run_id: str) -> str:
    job = _sanitize_marker_id(job_id)
    run = _sanitize_marker_id(run_id)
    if not job or not run:
        raise ValueError("job_id and run_id must be non-empty [A-Za-z0-9._-]+")
    return CronMarker(job, run).line()


def parse_cron_marker(content: str) -> Optional[CronMarker]:
    if not content:
        return None
    matches = list(_MARKER_LINE_RE.finditer(content))
    if not matches:
        matches = list(_MARKER_INLINE_RE.finditer(content))
    if not matches:
        return None
    match = matches[-1]
    return CronMarker(job_id=match.group("job_id"), run_id=match.group("run_id"))


def append_cron_marker(content: str, job_id: str, run_id: str) -> str:
    marker = format_cron_marker(job_id, run_id)
    body = content if content is not None else ""
    existing = parse_cron_marker(body)
    if (
        existing
        and existing.job_id == _sanitize_marker_id(job_id)
        and existing.run_id == _sanitize_marker_id(run_id)
    ):
        return body
    if body and not body.endswith("\n"):
        body += "\n"
    return body + "\n" + marker + "\n"


def _load_plugin_marker() -> None:
    """Rebind the public functions to the plugin copy when it is importable."""
    global CronMarker, format_cron_marker, parse_cron_marker, append_cron_marker
    from zulip.topic_ingest import (  # type: ignore
        CronMarker as _CM,
        append_cron_marker as _append,
        format_cron_marker as _format,
        parse_cron_marker as _parse,
    )

    CronMarker = _CM
    format_cron_marker = _format
    parse_cron_marker = _parse
    append_cron_marker = _append


try:
    _load_plugin_marker()
except Exception:
    pass


def safe_append_cron_marker_for_delivery(
    delivery_content: str,
    targets: Any,
    job: Any,
    *,
    append_fn: Optional[Callable[[str, str, str], str]] = None,
    import_append: Optional[Callable[[], Callable[[str, str, str], str]]] = None,
) -> str:
    """Append the topic-ingest footer for Zulip targets. Never raises.

    ``import_append`` / ``append_fn`` are test hooks. Forcing the import to
    fail (ImportError, plugin not importable, or any exception) returns
    ``delivery_content`` unchanged and logs a warning.
    """
    original = delivery_content
    try:
        rows = targets or []
        if not any(str((t or {}).get("platform") or "").lower() == "zulip" for t in rows):
            return delivery_content
        if import_append is not None:
            fn = import_append()
        elif append_fn is not None:
            fn = append_fn
        else:
            fn = append_cron_marker
        job = job or {}
        job_id = str(job.get("id") or "unknown")
        run_id = str(job.get("execution_id") or job_id)
        return fn(delivery_content, job_id, run_id)
    except Exception:
        logger.warning("zulip cron marker append failed", exc_info=True)
        return original


__all__ = [
    "CronMarker",
    "append_cron_marker",
    "format_cron_marker",
    "parse_cron_marker",
    "safe_append_cron_marker_for_delivery",
]
