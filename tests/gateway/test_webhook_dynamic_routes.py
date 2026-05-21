"""Tests for webhook adapter dynamic route loading."""

import json
import os
import pytest
from pathlib import Path

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _DYNAMIC_ROUTES_FILENAME


def _make_adapter(routes=None, extra=None):
    _extra = extra or {}
    if routes:
        _extra["routes"] = routes
    _extra.setdefault("secret", "test-global-secret")
    config = PlatformConfig(enabled=True, extra=_extra)
    return WebhookAdapter(config)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


class TestDynamicRouteLoading:
    def test_no_dynamic_file(self):
        adapter = _make_adapter(routes={"static": {"secret": "s"}})
        adapter._reload_dynamic_routes()
        assert "static" in adapter._routes
        assert len(adapter._dynamic_routes) == 0

    def test_loads_dynamic_routes(self, tmp_path):
        subs = {"my-hook": {"secret": "dynamic-secret", "prompt": "test", "events": []}}
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(json.dumps(subs))

        adapter = _make_adapter(routes={"static": {"secret": "s"}})
        adapter._reload_dynamic_routes()
        assert "my-hook" in adapter._routes
        assert "static" in adapter._routes

    def test_static_takes_precedence(self, tmp_path):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"conflict": {"secret": "dynamic", "prompt": "dyn"}})
        )
        adapter = _make_adapter(routes={"conflict": {"secret": "static", "prompt": "stat"}})
        adapter._reload_dynamic_routes()
        assert adapter._routes["conflict"]["secret"] == "static"

    def test_mtime_gated(self, tmp_path):
        import time
        path = tmp_path / _DYNAMIC_ROUTES_FILENAME
        path.write_text(json.dumps({"v1": {"secret": "s"}}))

        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "v1" in adapter._dynamic_routes

        # Same mtime — no reload
        adapter._dynamic_routes["injected"] = True
        adapter._reload_dynamic_routes()
        assert "injected" in adapter._dynamic_routes

        # New write — reloads
        time.sleep(0.05)
        path.write_text(json.dumps({"v2": {"secret": "s"}}))
        adapter._reload_dynamic_routes()
        assert "v2" in adapter._dynamic_routes
        assert "v1" not in adapter._dynamic_routes

    def test_file_removal_clears(self, tmp_path):
        path = tmp_path / _DYNAMIC_ROUTES_FILENAME
        path.write_text(json.dumps({"temp": {"secret": "s"}}))
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "temp" in adapter._dynamic_routes

        path.unlink()
        adapter._reload_dynamic_routes()
        assert len(adapter._dynamic_routes) == 0

    def test_corrupted_file(self, tmp_path):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text("not json")
        adapter = _make_adapter(routes={"static": {"secret": "s"}})
        adapter._reload_dynamic_routes()
        assert "static" in adapter._routes
        assert len(adapter._dynamic_routes) == 0


class TestDynamicRouteSecretValidation:
    """Regression tests for #8306 — hot-reloaded dynamic routes with empty
    or missing secrets must be rejected so the per-request HMAC check in
    ``_handle_webhook`` can never see a falsy secret and skip validation."""

    def test_empty_secret_string_rejected(self, tmp_path, caplog):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"monitor": {"secret": "", "prompt": "test"}})
        )
        # No global secret: _global_secret falls back to None, so the route
        # has no effective secret at all.
        config = PlatformConfig(
            enabled=True,
            extra={"routes": {"static": {"secret": "s"}}},
        )
        adapter = WebhookAdapter(config)
        adapter._reload_dynamic_routes()
        assert "monitor" not in adapter._routes
        assert "monitor" not in adapter._dynamic_routes

    def test_missing_secret_key_rejected_without_global(self, tmp_path):
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"hook-a": {"prompt": "test"}})
        )
        config = PlatformConfig(
            enabled=True,
            extra={"routes": {"static": {"secret": "s"}}},
        )
        adapter = WebhookAdapter(config)
        adapter._reload_dynamic_routes()
        assert "hook-a" not in adapter._routes
        assert "hook-a" not in adapter._dynamic_routes

    def test_missing_secret_falls_back_to_global(self, tmp_path):
        # When 'secret' key is absent, dict.get() returns the global default,
        # so a route with no per-route secret is still admitted when a global
        # secret is configured.
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"hook-b": {"prompt": "test"}})
        )
        adapter = _make_adapter()  # _make_adapter sets a global secret
        adapter._reload_dynamic_routes()
        assert "hook-b" in adapter._dynamic_routes

    def test_empty_per_route_secret_rejected_even_with_global(self, tmp_path):
        # Critical case: dict.get('secret', default) returns '' when the key
        # is present and empty, NOT the default. So even with a global secret
        # set, an empty per-route secret must still be skipped.
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"sneaky": {"secret": "", "prompt": "test"}})
        )
        adapter = _make_adapter()  # has a global secret
        adapter._reload_dynamic_routes()
        assert "sneaky" not in adapter._dynamic_routes

    def test_insecure_no_auth_secret_still_admitted(self, tmp_path):
        # INSECURE_NO_AUTH is the explicit opt-in for unauthenticated routes
        # (loopback-only enforcement happens at startup). Hot-reload must keep
        # it working — it's a non-empty string so the validator accepts it.
        (tmp_path / _DYNAMIC_ROUTES_FILENAME).write_text(
            json.dumps({"local-test": {"secret": "INSECURE_NO_AUTH", "prompt": "t"}})
        )
        adapter = _make_adapter()
        adapter._reload_dynamic_routes()
        assert "local-test" in adapter._dynamic_routes
