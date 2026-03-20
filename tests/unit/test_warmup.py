"""Tests for proxy_relay.warmup — DataDome trust warm-up."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestWarmupSessionDefaults:
    """WarmupSession constructor defaults."""

    def test_defaults_are_correct(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin")
        assert s.profile_name == "medellin"
        assert s.timeout == 120.0
        assert s.no_verify is False
        assert s.lang is None
        assert s.timezone is None

    def test_custom_values_stored(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(
            profile_name="test",
            timeout=60.0,
            lang="es-419,es",
            timezone="America/Bogota",
            no_verify=True,
        )
        assert s.timeout == 60.0
        assert s.lang == "es-419,es"
        assert s.timezone == "America/Bogota"
        assert s.no_verify is True

    def test_account_email_default_none(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="test")
        assert s.account_email is None

    def test_account_email_stored(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="test", account_email="test@example.com")
        assert s.account_email == "test@example.com"


class TestRunWarmup:
    """Tests for run_warmup() entry point."""

    def test_missing_browser_returns_1(self, capsys):
        from proxy_relay.warmup import run_warmup
        from proxy_relay.exceptions import BrowseError

        with patch("proxy_relay.warmup._browse.resolve_browser",
                   side_effect=BrowseError("not found")):
            result = run_warmup("medellin", browser="nonexistent")
        assert result == 1

    def test_no_browser_override_uses_find_chromium(self):
        """run_warmup with browser=None delegates to find_chromium."""
        from proxy_relay.warmup import run_warmup

        with patch("proxy_relay.warmup.WarmupSession.run", return_value=0) as mock_run:
            result = run_warmup("medellin")
        assert result == 0
        mock_run.assert_called_once()


class TestWarmupSessionEnsureServer:
    """Tests for _ensure_server() — server lifecycle."""

    def test_uses_existing_server_when_running(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin")
        with (
            patch("proxy_relay.warmup.read_pid", return_value=1234),
            patch("proxy_relay.warmup.is_process_running", return_value=True),
            patch("proxy_relay.warmup.read_status", return_value={"host": "127.0.0.1", "port": 9090}),
        ):
            host, port = s._ensure_server()
        assert port == 9090
        assert s._auto_started is False

    def test_auto_starts_when_no_server(self):
        from proxy_relay.warmup import WarmupSession

        mock_proc = MagicMock()
        mock_proc.pid = 5678

        s = WarmupSession(profile_name="medellin")
        with (
            patch("proxy_relay.warmup.read_pid", return_value=None),
            patch("proxy_relay.warmup._browse.auto_start_server", return_value=mock_proc),
            patch("proxy_relay.warmup._browse.wait_for_server_ready", return_value=("127.0.0.1", 8000)),
        ):
            host, port = s._ensure_server()
        assert port == 8000
        assert s._auto_started is True
        assert s._server_proc is mock_proc


def _make_cookies_db(path: Path, *, with_datadome: bool = True) -> None:
    """Create a minimal Chromium Cookies SQLite DB at path/Default/Cookies."""
    cookies_dir = path / "Default"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cookies_dir / "Cookies")
    conn.execute(
        "CREATE TABLE cookies ("
        "host_key TEXT, name TEXT, value TEXT, "
        "expires_utc INTEGER, is_secure INTEGER)"
    )
    if with_datadome:
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
            (".tidal.com", "datadome", "abc123", 0, 1),
        )
    conn.commit()
    conn.close()


class TestPollForDatadome:
    """Tests for WarmupSession._poll_for_datadome()."""

    def test_cookie_found_returns_0(self, tmp_path):
        from proxy_relay.warmup import WarmupSession

        _make_cookies_db(tmp_path, with_datadome=True)
        s = WarmupSession(profile_name="medellin", timeout=10.0)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        s._browser_handle = MagicMock()
        s._browser_handle.process.poll.return_value = None

        with patch.object(s, "_write_meta"):
            result = s._poll_for_datadome(tmp_path)
        assert result == 0

    def test_no_cookie_timeout_returns_1(self, tmp_path, capsys):
        from proxy_relay.warmup import WarmupSession

        _make_cookies_db(tmp_path, with_datadome=False)
        s = WarmupSession(profile_name="medellin", timeout=0.05, no_verify=True)
        s._browser_handle = MagicMock()
        s._browser_handle.process.poll.return_value = None

        with patch("proxy_relay.warmup._DATADOME_POLL_INTERVAL", 0.01):
            result = s._poll_for_datadome(tmp_path)
        assert result == 1
        assert "timeout" in capsys.readouterr().err.lower()

    def test_browser_exit_returns_1(self, tmp_path, capsys):
        from proxy_relay.warmup import WarmupSession

        # No cookies DB — browser "exits" before it's created
        s = WarmupSession(profile_name="medellin", timeout=10.0)
        s._browser_handle = MagicMock()
        s._browser_handle.process.poll.return_value = 1  # browser exited

        result = s._poll_for_datadome(tmp_path)
        assert result == 1
        assert "exited" in capsys.readouterr().err.lower()

    def test_cookie_found_writes_warmup_meta(self, tmp_path):
        from proxy_relay.warmup import WarmupSession

        _make_cookies_db(tmp_path, with_datadome=True)
        s = WarmupSession(profile_name="medellin", timeout=10.0, account_email="a@b.com")
        s._browser_handle = MagicMock()
        s._browser_handle.process.poll.return_value = None

        with patch("proxy_relay.warmup.WarmupSession._write_meta") as mock_write:
            s._poll_for_datadome(tmp_path)

        mock_write.assert_called_once_with(tmp_path, exit_ip="")

    def test_missing_db_does_not_crash(self, tmp_path, capsys):
        """No Cookies DB yet — should keep polling until timeout."""
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin", timeout=0.05, no_verify=True)
        s._browser_handle = MagicMock()
        s._browser_handle.process.poll.return_value = None

        with patch("proxy_relay.warmup._DATADOME_POLL_INTERVAL", 0.01):
            result = s._poll_for_datadome(tmp_path)
        assert result == 1  # timeout, not crash


class TestWriteMeta:
    """Tests for WarmupSession._write_meta()."""

    def test_calls_write_warmup_meta(self, tmp_path):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin", account_email="user@example.com")
        s.host = "127.0.0.1"
        s.country = "co"

        with patch("proxy_relay.warmup.write_warmup_meta") as mock_write:
            s._write_meta(tmp_path)

        mock_write.assert_called_once_with(tmp_path, "127.0.0.1", "co", "user@example.com")

    def test_write_failure_does_not_raise(self, tmp_path):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin")
        s.host = "127.0.0.1"
        s.country = "co"

        with patch("proxy_relay.warmup.write_warmup_meta", side_effect=OSError("disk full")):
            s._write_meta(tmp_path)  # must not raise


class TestWarmupCleanup:
    """Tests for cleanup in WarmupSession._cleanup()."""

    def test_cleanup_kills_browser_if_running(self):
        from proxy_relay.warmup import WarmupSession
        from proxy_relay.browse import BrowserHandle

        s = WarmupSession(profile_name="medellin")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        s._browser_handle = BrowserHandle(
            process=mock_proc, profile_dir=Path("/tmp/p"), chromium_path=Path("/usr/bin/chromium")
        )

        with patch("proxy_relay.warmup._browse.close_browser") as mock_close:
            s._cleanup()
        mock_close.assert_called_once()

    def test_cleanup_skips_browser_if_already_exited(self):
        from proxy_relay.warmup import WarmupSession
        from proxy_relay.browse import BrowserHandle

        s = WarmupSession(profile_name="medellin")
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        s._browser_handle = BrowserHandle(
            process=mock_proc, profile_dir=Path("/tmp/p"), chromium_path=Path("/usr/bin/chromium")
        )

        with patch("proxy_relay.warmup._browse.close_browser") as mock_close:
            s._cleanup()
        mock_close.assert_not_called()

    def test_cleanup_stops_auto_started_server(self):
        from proxy_relay.warmup import WarmupSession

        s = WarmupSession(profile_name="medellin")
        s._auto_started = True
        mock_proc = MagicMock()
        s._server_proc = mock_proc
        s._browser_handle = None

        with patch("proxy_relay.warmup._browse.auto_stop_server") as mock_stop:
            s._cleanup()
        mock_stop.assert_called_once_with(mock_proc, "medellin")
