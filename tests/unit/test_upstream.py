"""Tests for proxy_relay.upstream — UpstreamManager proxy-st integration."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from proxy_relay.exceptions import UpstreamError
from proxy_relay.upstream import UpstreamInfo, UpstreamManager


class TestUpstreamInfo:
    """Test UpstreamInfo dataclass."""

    def test_frozen(self):
        info = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://user:pass@proxy.example.com:12322",
            masked_url="socks5://***@proxy.example.com:12322", country="us",
        )
        assert info.host == "proxy.example.com"
        assert info.port == 12322
        with pytest.raises(AttributeError):
            info.host = "other"  # type: ignore[misc]


class TestUpstreamManager:
    """Test UpstreamManager with mocked proxy-st."""

    def test_profile_name_stored(self):
        mgr = UpstreamManager("browse")
        assert mgr.profile_name == "browse"

    def test_current_is_none_before_resolve(self):
        mgr = UpstreamManager("browse")
        assert mgr.current is None

    @patch.object(UpstreamManager, "_build_url")
    @patch.object(UpstreamManager, "_ensure_loaded")
    def test_get_upstream_parses_url(self, mock_loaded, mock_build):
        """get_upstream() parses a SOCKS5 URL into UpstreamInfo."""
        mock_build.return_value = "socks5://testuser:testpass@proxy.example.com:12322"
        # Mock the config to provide a profile with country
        mgr = UpstreamManager("browse")
        mgr._config = MagicMock()
        mgr._config.profiles = {"browse": MagicMock(country="us")}

        info = mgr.get_upstream()

        assert info.host == "proxy.example.com"
        assert info.port == 12322
        assert info.username == "testuser"
        assert info.password == "testpass"
        assert info.country == "us"

    @patch.object(UpstreamManager, "_build_url")
    @patch.object(UpstreamManager, "_ensure_loaded")
    def test_get_upstream_updates_current(self, mock_loaded, mock_build):
        """get_upstream() updates the current property."""
        mock_build.return_value = "socks5://user:pass@host.com:1080"
        mgr = UpstreamManager("browse")
        mgr._config = MagicMock()
        mgr._config.profiles = {"browse": MagicMock(country="co")}

        info = mgr.get_upstream()

        assert mgr.current is info

    @patch.object(UpstreamManager, "_build_url")
    @patch.object(UpstreamManager, "_ensure_loaded")
    def test_rotate_clears_and_rebuilds(self, mock_loaded, mock_build):
        """rotate() clears current and calls get_upstream()."""
        mock_build.return_value = "socks5://user:pass@host.com:1080"
        mgr = UpstreamManager("browse")
        mgr._config = MagicMock()
        mgr._config.profiles = {"browse": MagicMock(country="us")}
        mgr._session_store = MagicMock()

        info = mgr.rotate()

        mgr._session_store.rotate.assert_called_once_with("browse")
        assert info is not None
        assert info.host == "host.com"


class TestEnsureLoadedDoubleCheckedLocking:
    """Test F-RL2: _ensure_loaded uses double-checked locking (no TOCTOU)."""

    def test_ensure_loaded_fast_path_when_config_set(self):
        """`_ensure_loaded` returns immediately (fast path) when _config is already set."""
        mgr = UpstreamManager("browse")
        mock_config = MagicMock()
        mock_config.profiles = {"browse": MagicMock(country="co")}
        mgr._config = mock_config
        mgr._session_store = MagicMock()

        # Should return immediately without attempting any import
        mgr._ensure_loaded()
        assert mgr._config is mock_config

    def test_ensure_loaded_concurrent_calls_load_exactly_once(self):
        """Concurrent `_ensure_loaded` calls do not double-load the config."""
        import sys

        load_call_count = {"n": 0}
        barrier = threading.Barrier(8)

        real_app_config = MagicMock()
        real_app_config.profiles = {"browse": MagicMock(country="co")}

        def slow_load():
            load_call_count["n"] += 1
            return real_app_config

        mock_app_config_cls = MagicMock()
        mock_app_config_cls.load.side_effect = slow_load

        mock_session_store_cls = MagicMock()
        mock_session_store_cls.return_value = MagicMock()

        fake_proxy_st_config = MagicMock()
        fake_proxy_st_config.AppConfig = mock_app_config_cls

        fake_proxy_st_session_store = MagicMock()
        fake_proxy_st_session_store.SessionStore = mock_session_store_cls

        errors: list[Exception] = []

        def run_ensure_loaded(mgr: UpstreamManager) -> None:
            barrier.wait()
            try:
                mgr._ensure_loaded()
            except Exception as exc:
                errors.append(exc)

        mgr = UpstreamManager("browse")

        with patch.dict(sys.modules, {
            "proxy_st.config": fake_proxy_st_config,
            "proxy_st.session_store": fake_proxy_st_session_store,
            "proxy_st": MagicMock(),
        }):
            threads = [threading.Thread(target=run_ensure_loaded, args=(mgr,)) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors
        # Config loaded exactly once despite 8 concurrent callers
        assert load_call_count["n"] == 1
        assert mgr._config is real_app_config
