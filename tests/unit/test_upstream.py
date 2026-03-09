"""Tests for proxy_relay.upstream — UpstreamManager proxy-st integration."""
from __future__ import annotations

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
            url="socks5://***@proxy.example.com:12322", country="us",
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
