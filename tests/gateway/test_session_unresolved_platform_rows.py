"""Rows for a plugin platform that registers after the session store loads must survive full saves
and be restored by ``retry_unresolved_entries`` (zulip under the multiplexer)."""
import json

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore

PLAT = "zz-late-plugin-platform"
KEY = f"agent:p:{PLAT}:dm:dm:1"


def _row():
    return {"session_key": KEY, "session_id": "20260101_000000_abcdef",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
            "display_name": "T", "platform": PLAT, "chat_type": "dm", "metadata": {},
            "origin": {"platform": PLAT, "chat_id": "1", "chat_type": "dm"}}


def test_unresolved_row_preserved_and_restored(tmp_path):
    (tmp_path / "sessions.json").write_text(json.dumps({KEY: _row()}))
    store = SessionStore(tmp_path, GatewayConfig())
    with store._lock:
        store._ensure_loaded_locked()
        assert KEY not in store._entries
        assert KEY in store._entries_as_dicts()          # a full save keeps it
        store._save()
    assert KEY in json.loads((tmp_path / "sessions.json").read_text())
    assert store.retry_unresolved_entries() == 0          # still unknown: stays preserved
    Platform._add_pseudo_member(PLAT)                     # plugin registers later
    assert store.retry_unresolved_entries() == 1
    assert KEY in store._entries
    assert store.retry_unresolved_entries() == 0
