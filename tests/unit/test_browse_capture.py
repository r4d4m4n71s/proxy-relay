"""Tests for browse.py capture integration — _chrome_args cdp_port and BrowseSupervisor."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chromium_path() -> Path:
    return Path("/usr/bin/chromium")


def _make_profile_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles" / "test"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 1. _chrome_args — cdp_port parameter
# ---------------------------------------------------------------------------


class TestChromeArgsCdpPort:
    """Verify _chrome_args behaves correctly with and without cdp_port."""

    def test_chrome_args_without_cdp_port(self, tmp_path):
        """Without cdp_port, --remote-debugging-port must NOT appear in the command."""
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            _make_chromium_path(),
            _make_profile_dir(tmp_path),
        )
        assert not any("--remote-debugging-port" in arg for arg in cmd), (
            f"--remote-debugging-port must be absent without cdp_port. Got: {cmd}"
        )

    def test_chrome_args_with_cdp_port_adds_flag(self, tmp_path):
        """With cdp_port=12345, --remote-debugging-port=12345 must appear in the command."""
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            _make_chromium_path(),
            _make_profile_dir(tmp_path),
            cdp_port=12345,
        )
        assert any("--remote-debugging-port=12345" in arg for arg in cmd), (
            f"--remote-debugging-port=12345 must be present. Got: {cmd}"
        )

    def test_chrome_args_with_cdp_port_zero_not_added(self, tmp_path):
        """cdp_port=None must not add the flag (regression: avoid port 0)."""
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            _make_chromium_path(),
            _make_profile_dir(tmp_path),
            cdp_port=None,
        )
        assert not any("--remote-debugging-port" in arg for arg in cmd)

    def test_chrome_args_with_cdp_port_preserves_existing_flags(self, tmp_path):
        """Adding cdp_port must not remove existing anti-leak flags."""
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            _make_chromium_path(),
            _make_profile_dir(tmp_path),
            proxy_port=8080,
            cdp_port=9222,
        )
        assert any("--disable-webrtc-stun-origin" in arg for arg in cmd)
        assert any("--proxy-server=" in arg for arg in cmd)
        assert any("--remote-debugging-port=9222" in arg for arg in cmd)

    def test_chrome_args_cdp_port_binds_localhost(self, tmp_path):
        """CDP port flag must not expose to the network — no IP prefix (browser binds localhost)."""
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            _make_chromium_path(),
            _make_profile_dir(tmp_path),
            cdp_port=9222,
        )
        # The flag must be just --remote-debugging-port=<port>, no host prefix
        rdp_flags = [arg for arg in cmd if "--remote-debugging-port" in arg]
        assert len(rdp_flags) == 1
        assert rdp_flags[0] == "--remote-debugging-port=9222"


# ---------------------------------------------------------------------------
# 2. BrowseSupervisor — capture_session parameter
# ---------------------------------------------------------------------------


class TestBrowseSupervisorCapture:
    """Verify BrowseSupervisor correctly stores the capture_session."""

    def _make_supervisor(self, tmp_path, capture_session=None):
        from proxy_relay.browse import BrowseSupervisor

        return BrowseSupervisor(
            chromium_path=_make_chromium_path(),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=_make_profile_dir(tmp_path),
            relay_pid=12345,
            capture_session=capture_session,
        )

    def test_supervisor_init_without_capture_session_no_crash(self, tmp_path):
        """BrowseSupervisor with capture_session=None must construct without error."""
        supervisor = self._make_supervisor(tmp_path, capture_session=None)
        assert supervisor is not None

    def test_supervisor_init_without_capture_stores_none(self, tmp_path):
        """capture_session=None is stored as None (not some default mock)."""
        supervisor = self._make_supervisor(tmp_path, capture_session=None)
        assert supervisor._capture is None

    def test_supervisor_init_with_capture_stores_session(self, tmp_path):
        """capture_session argument is stored on the supervisor instance."""
        mock_session = MagicMock()
        supervisor = self._make_supervisor(tmp_path, capture_session=mock_session)
        assert supervisor._capture is mock_session

    def test_supervisor_capture_session_default_is_none(self, tmp_path):
        """When capture_session is not passed, default is None (backward compat)."""
        from proxy_relay.browse import BrowseSupervisor

        # Construct without the capture_session keyword arg at all
        supervisor = BrowseSupervisor(
            chromium_path=_make_chromium_path(),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=_make_profile_dir(tmp_path),
            relay_pid=99999,
        )
        assert supervisor._capture is None
