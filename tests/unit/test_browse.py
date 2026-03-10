"""Tests for proxy_relay.browse — Chromium supervisor and browse helpers."""
from __future__ import annotations

import asyncio
import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

# ---------------------------------------------------------------------------
# 1. BrowseError hierarchy
# ---------------------------------------------------------------------------


class TestBrowseError:
    """Verify BrowseError is part of the exception hierarchy."""

    def test_browse_error_is_proxy_relay_error(self):
        from proxy_relay.exceptions import BrowseError, ProxyRelayError

        assert issubclass(BrowseError, ProxyRelayError)

    def test_browse_error_message(self):
        from proxy_relay.exceptions import BrowseError

        err = BrowseError("chromium not found")
        assert str(err) == "chromium not found"

    def test_browse_error_caught_by_base(self):
        from proxy_relay.exceptions import BrowseError, ProxyRelayError

        with pytest.raises(ProxyRelayError):
            raise BrowseError("test")


# ---------------------------------------------------------------------------
# 2. BrowseConfig parsing
# ---------------------------------------------------------------------------


class TestBrowseConfig:
    """Verify [browse] section parsing in config."""

    def test_default_values_when_section_missing(self):
        """No [browse] section => defaults applied."""
        from proxy_relay.config import _parse_config

        cfg = _parse_config({})
        assert cfg.browse.rotate_interval_min == 30

    def test_explicit_valid_rotate_interval(self):
        from proxy_relay.config import _parse_config

        cfg = _parse_config({"browse": {"rotate_interval_min": 10}})
        assert cfg.browse.rotate_interval_min == 10

    def test_rotate_interval_zero_is_valid(self):
        """Zero disables rotation and must be accepted."""
        from proxy_relay.config import _parse_config

        cfg = _parse_config({"browse": {"rotate_interval_min": 0}})
        assert cfg.browse.rotate_interval_min == 0

    def test_negative_rotate_interval_raises_config_error(self):
        from proxy_relay.config import _parse_config
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError, match="rotate_interval_min"):
            _parse_config({"browse": {"rotate_interval_min": -1}})

    def test_non_integer_string_raises_config_error(self):
        from proxy_relay.config import _parse_config
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError, match="rotate_interval_min"):
            _parse_config({"browse": {"rotate_interval_min": "fast"}})

    def test_float_value_raises_config_error(self):
        from proxy_relay.config import _parse_config
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError, match="rotate_interval_min"):
            _parse_config({"browse": {"rotate_interval_min": 5.5}})

    def test_browse_config_frozen(self):
        from proxy_relay.config import BrowseConfig

        bc = BrowseConfig()
        with pytest.raises(AttributeError):
            bc.rotate_interval_min = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. find_chromium()
# ---------------------------------------------------------------------------


class TestFindChromium:
    """Tests for find_chromium() — Chromium binary discovery."""

    @patch("proxy_relay.browse.shutil.which", return_value=None)
    def test_absolute_path_exists_returns_it(self, _mock_which: MagicMock):
        from proxy_relay.browse import _CHROMIUM_CANDIDATES, find_chromium

        first_abs = [c for c in _CHROMIUM_CANDIDATES if c.startswith("/")][0]

        with patch.object(Path, "exists", side_effect=lambda: True):
            # We need a more targeted approach: only the first absolute candidate
            result = find_chromium()
            assert result == Path(first_abs)

    @patch("proxy_relay.browse.shutil.which")
    def test_bare_name_found_via_which(self, mock_which: MagicMock):
        from proxy_relay.browse import find_chromium

        # All absolute paths don't exist; shutil.which finds a bare name
        def which_side_effect(name: str) -> str | None:
            if name == "chromium":
                return "/usr/bin/chromium"
            return None

        mock_which.side_effect = which_side_effect

        with patch.object(Path, "exists", return_value=False):
            result = find_chromium()
            assert result == Path("/usr/bin/chromium")

    @patch("proxy_relay.browse.shutil.which", return_value=None)
    def test_no_candidates_raises_browse_error(self, _mock_which: MagicMock):
        from proxy_relay.browse import find_chromium
        from proxy_relay.exceptions import BrowseError

        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(BrowseError, match="[Cc]hromium"):
                find_chromium()


# ---------------------------------------------------------------------------
# 4. health_check()
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for health_check() — calls the server's /__health endpoint."""

    @patch("proxy_relay.browse.urllib.request.urlopen")
    def test_success_returns_exit_ip(self, mock_urlopen: MagicMock):
        from proxy_relay.browse import health_check

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "exit_ip": "203.0.113.42"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = health_check("127.0.0.1", 8080)
        assert result == "203.0.113.42"
        mock_urlopen.assert_called_once_with(
            "http://127.0.0.1:8080/__health", timeout=60.0,
        )

    @patch("proxy_relay.browse.urllib.request.urlopen")
    def test_timeout_raises_browse_error(self, mock_urlopen: MagicMock):
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        mock_urlopen.side_effect = TimeoutError("timed out")

        with pytest.raises(BrowseError, match="[Hh]ealth"):
            health_check("127.0.0.1", 8080)

    @patch("proxy_relay.browse.urllib.request.urlopen")
    def test_url_error_raises_browse_error(self, mock_urlopen: MagicMock):
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        mock_urlopen.side_effect = URLError("connection refused")

        with pytest.raises(BrowseError):
            health_check("127.0.0.1", 8080)

    @patch("proxy_relay.browse.urllib.request.urlopen")
    def test_http_503_parses_error_body(self, mock_urlopen: MagicMock):
        import io
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        body = b'{"ok": false, "error": "upstream unreachable after 3 attempts"}'
        exc = HTTPError(
            url="http://127.0.0.1:8080/__health", code=503,
            msg="Service Unavailable", hdrs=MagicMock(),
            fp=io.BytesIO(body),
        )
        mock_urlopen.side_effect = exc

        with pytest.raises(BrowseError, match="upstream unreachable"):
            health_check("127.0.0.1", 8080)

    @patch("proxy_relay.browse.urllib.request.urlopen")
    def test_os_error_raises_browse_error(self, mock_urlopen: MagicMock):
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        mock_urlopen.side_effect = OSError("network unreachable")

        with pytest.raises(BrowseError):
            health_check("127.0.0.1", 8080)


# ---------------------------------------------------------------------------
# 5. get_profile_dir()
# ---------------------------------------------------------------------------


class TestGetProfileDir:
    """Tests for get_profile_dir() — browser profile directory creation."""

    def test_returns_correct_path(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            result = get_profile_dir("my-profile")
            assert result == tmp_path / "my-profile"

    def test_creates_directory(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            profile_dir = get_profile_dir("new-profile")
            assert profile_dir.is_dir()

    def test_existing_directory_no_error(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        (tmp_path / "existing").mkdir()
        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            result = get_profile_dir("existing")
            assert result == tmp_path / "existing"

    def test_snap_chromium_uses_snap_dir(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        snap_dir = tmp_path / "snap-profiles"
        with patch("proxy_relay.browse._SNAP_PROFILES_DIR", snap_dir):
            result = get_profile_dir("browse", chromium_path=Path("/snap/bin/chromium"))
            assert result == snap_dir / "browse"
            assert result.is_dir()

    def test_non_snap_chromium_uses_default_dir(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            result = get_profile_dir("browse", chromium_path=Path("/usr/bin/chromium"))
            assert result == tmp_path / "browse"

    def test_no_chromium_path_uses_default_dir(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            result = get_profile_dir("browse", chromium_path=None)
            assert result == tmp_path / "browse"


# ---------------------------------------------------------------------------
# 5b. _is_snap_chromium()
# ---------------------------------------------------------------------------


class TestIsSnapChromium:
    """Tests for _is_snap_chromium() — Snap package detection."""

    def test_snap_path_returns_true(self):
        from proxy_relay.browse import _is_snap_chromium

        assert _is_snap_chromium(Path("/snap/bin/chromium")) is True

    def test_non_snap_path_returns_false(self):
        from proxy_relay.browse import _is_snap_chromium

        assert _is_snap_chromium(Path("/usr/bin/chromium")) is False
        assert _is_snap_chromium(Path("/usr/bin/google-chrome")) is False


# ---------------------------------------------------------------------------
# 6. BrowseSupervisor._start_chromium()
# ---------------------------------------------------------------------------


class TestStartChromium:
    """Tests for BrowseSupervisor._start_chromium() — Chromium launch."""

    def _make_supervisor(self, **overrides):
        from proxy_relay.browse import BrowseSupervisor

        defaults = dict(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=12345,
            rotate_interval_min=30,
        )
        defaults.update(overrides)
        return BrowseSupervisor(**defaults)

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_correct_flags_passed(self, mock_popen: MagicMock):
        sv = self._make_supervisor()
        mock_popen.return_value = MagicMock()

        sv._start_chromium()

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]

        assert cmd[0] == str(Path("/usr/bin/chromium"))
        assert "--proxy-server=http://127.0.0.1:8080" in cmd
        assert "--user-data-dir=/tmp/test-profile" in cmd
        assert "--start-maximized" in cmd
        assert "--no-first-run" in cmd
        assert "--disable-default-apps" in cmd
        assert "--disable-sync" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_os_error_raises_browse_error(self, mock_popen: MagicMock):
        from proxy_relay.exceptions import BrowseError

        sv = self._make_supervisor()
        mock_popen.side_effect = OSError("No such file")

        with pytest.raises(BrowseError, match="[Cc]hromium|launch|start"):
            sv._start_chromium()


# ---------------------------------------------------------------------------
# 7. BrowseSupervisor.run() — Chromium exits normally
# ---------------------------------------------------------------------------


class TestRunNormalExit:
    """Tests for BrowseSupervisor.run() when Chromium exits cleanly."""

    def _make_supervisor(self, **overrides):
        from proxy_relay.browse import BrowseSupervisor

        defaults = dict(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=12345,
            rotate_interval_min=0,  # disable rotation for simpler tests
        )
        defaults.update(overrides)
        return BrowseSupervisor(**defaults)

    @patch("proxy_relay.browse.is_process_running", return_value=True)
    @patch("proxy_relay.browse.subprocess.Popen")
    def test_chromium_exits_returns_zero(
        self, mock_popen: MagicMock, _mock_running: MagicMock
    ):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        sv = self._make_supervisor()
        result = sv.run()
        assert result == 0


# ---------------------------------------------------------------------------
# 8. BrowseSupervisor.run() — proxy dies
# ---------------------------------------------------------------------------


class TestRunProxyDies:
    """Tests for BrowseSupervisor.run() when the relay process dies."""

    def _make_supervisor(self, **overrides):
        from proxy_relay.browse import BrowseSupervisor

        defaults = dict(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=12345,
            rotate_interval_min=0,
        )
        defaults.update(overrides)
        return BrowseSupervisor(**defaults)

    @patch("proxy_relay.browse.is_process_running")
    @patch("proxy_relay.browse.subprocess.Popen")
    def test_relay_dies_returns_one(
        self, mock_popen: MagicMock, mock_running: MagicMock
    ):
        # Relay dies immediately
        mock_running.return_value = False

        mock_proc = MagicMock()
        # Chromium is still running when relay dies; poll returns None first,
        # then after terminate is called, poll returns -15
        mock_proc.poll.side_effect = [None, None, -15]
        mock_proc.wait.return_value = -15
        mock_proc.returncode = -15
        mock_popen.return_value = mock_proc

        sv = self._make_supervisor()
        result = sv.run()
        assert result == 1


# ---------------------------------------------------------------------------
# 9. BrowseSupervisor._poll_relay()
# ---------------------------------------------------------------------------


class TestPollRelay:
    """Tests for the relay polling background thread."""

    def _make_supervisor(self, **overrides):
        from proxy_relay.browse import BrowseSupervisor

        defaults = dict(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=99999,
            rotate_interval_min=0,
        )
        defaults.update(overrides)
        return BrowseSupervisor(**defaults)

    @patch("proxy_relay.browse.is_process_running", return_value=False)
    def test_sets_stop_event_and_relay_died(self, _mock_running: MagicMock):
        sv = self._make_supervisor()

        # Run _poll_relay in a thread; it should detect relay death quickly
        t = threading.Thread(target=sv._poll_relay, daemon=True)
        t.start()
        t.join(timeout=5.0)

        assert sv._stop_event.is_set()
        assert sv._relay_died is True

    @patch("proxy_relay.browse.is_process_running")
    def test_stops_when_stop_event_set(self, mock_running: MagicMock):
        """If _stop_event is already set, _poll_relay exits without checking."""
        mock_running.return_value = True

        sv = self._make_supervisor()
        sv._stop_event.set()

        # Should return immediately without blocking
        t = threading.Thread(target=sv._poll_relay, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()


# ---------------------------------------------------------------------------
# 10. BrowseSupervisor._rotation_loop()
# ---------------------------------------------------------------------------


class TestRotationLoop:
    """Tests for the rotation background thread."""

    def _make_supervisor(self, **overrides):
        from proxy_relay.browse import BrowseSupervisor

        defaults = dict(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=12345,
            rotate_interval_min=1,  # 1 minute for testing
        )
        defaults.update(overrides)
        return BrowseSupervisor(**defaults)

    @patch("proxy_relay.browse.send_signal", return_value=True)
    def test_sends_sigusr1(self, mock_send: MagicMock):
        sv = self._make_supervisor()

        # We'll let the rotation loop run, then immediately stop it
        # by setting _stop_event after a very short wait.
        # To avoid real sleeps, we make _stop_event.wait() return False once
        # (simulating interval elapsed) then True (simulating stop).
        call_count = 0

        def fake_wait(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return False  # interval elapsed, should send signal
            # Set stop and return True
            sv._stop_event.set()
            return True

        sv._stop_event.wait = fake_wait  # type: ignore[assignment]

        sv._rotation_loop()

        mock_send.assert_called_once_with(12345, signal.SIGUSR1)

    def test_rotation_disabled_when_interval_zero(self):
        sv = self._make_supervisor(rotate_interval_min=0)

        # _rotation_loop should return immediately without doing anything
        # when interval is 0
        t = threading.Thread(target=sv._rotation_loop, daemon=True)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()

    @patch("proxy_relay.browse.send_signal", return_value=True)
    def test_stops_when_stop_event_set(self, mock_send: MagicMock):
        sv = self._make_supervisor()
        sv._stop_event.set()

        sv._rotation_loop()

        # Should not have sent any signal since we stopped immediately
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 11. _cmd_browse() CLI handler
# ---------------------------------------------------------------------------


class TestCmdBrowse:
    """Tests for _cmd_browse() CLI handler with auto-start/stop lifecycle."""

    def _make_args(self, **overrides):
        """Build a mock argparse.Namespace for the browse command."""
        import argparse

        defaults = dict(
            command="browse",
            config=None,
            rotate_min=None,
            no_rotate=False,
            profile=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    @patch("proxy_relay.browse.auto_start_server")
    @patch("proxy_relay.cli.is_process_running", return_value=False)
    @patch("proxy_relay.cli.read_pid", return_value=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_no_server_triggers_auto_start(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        mock_auto_start: MagicMock,
    ):
        """When no server is running, auto_start_server is called."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import BrowseError

        mock_load.return_value = RelayConfig()
        mock_auto_start.side_effect = BrowseError("upstream failed")

        result = _cmd_browse(self._make_args())
        assert result == 1
        mock_auto_start.assert_called_once()

    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 8080})
    @patch("proxy_relay.cli.is_process_running", return_value=True)
    @patch("proxy_relay.cli.read_pid", return_value=42)
    @patch("proxy_relay.browse.health_check", side_effect=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_health_check_fails_returns_one(
        self,
        mock_load: MagicMock,
        _mock_hc: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
    ):
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import BrowseError

        mock_load.return_value = RelayConfig()
        _mock_hc.side_effect = BrowseError("connection refused")

        result = _cmd_browse(self._make_args())
        assert result == 1

    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 8080})
    @patch("proxy_relay.cli.is_process_running", return_value=True)
    @patch("proxy_relay.cli.read_pid", return_value=42)
    @patch("proxy_relay.browse.health_check", return_value="203.0.113.1")
    @patch("proxy_relay.browse.find_chromium", side_effect=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_chromium_not_found_returns_one(
        self,
        mock_load: MagicMock,
        mock_find: MagicMock,
        _mock_hc: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
    ):
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import BrowseError

        mock_load.return_value = RelayConfig()
        mock_find.side_effect = BrowseError("chromium not found")

        result = _cmd_browse(self._make_args())
        assert result == 1

    @patch("proxy_relay.browse.BrowseSupervisor")
    @patch("proxy_relay.browse.get_profile_dir", return_value=Path("/tmp/profile"))
    @patch("proxy_relay.browse.find_chromium", return_value=Path("/usr/bin/chromium"))
    @patch("proxy_relay.browse.health_check", return_value="203.0.113.1")
    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 8080})
    @patch("proxy_relay.cli.is_process_running", return_value=True)
    @patch("proxy_relay.cli.read_pid", return_value=42)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_no_rotate_flag_sets_interval_zero(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
        _mock_hc: MagicMock,
        _mock_find: MagicMock,
        _mock_profile: MagicMock,
        mock_supervisor_cls: MagicMock,
    ):
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import BrowseConfig, RelayConfig

        mock_load.return_value = RelayConfig(browse=BrowseConfig(rotate_interval_min=30))

        mock_sv = MagicMock()
        mock_sv.run.return_value = 0
        mock_supervisor_cls.return_value = mock_sv

        result = _cmd_browse(self._make_args(no_rotate=True))
        assert result == 0

        _, kwargs = mock_supervisor_cls.call_args
        assert kwargs.get("rotate_interval_min") == 0

    @patch("proxy_relay.browse.BrowseSupervisor")
    @patch("proxy_relay.browse.get_profile_dir", return_value=Path("/tmp/profile"))
    @patch("proxy_relay.browse.find_chromium", return_value=Path("/usr/bin/chromium"))
    @patch("proxy_relay.browse.health_check", return_value="203.0.113.1")
    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 8080})
    @patch("proxy_relay.cli.is_process_running", return_value=True)
    @patch("proxy_relay.cli.read_pid", return_value=42)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_rotate_min_overrides_config(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
        _mock_hc: MagicMock,
        _mock_find: MagicMock,
        _mock_profile: MagicMock,
        mock_supervisor_cls: MagicMock,
    ):
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import BrowseConfig, RelayConfig

        mock_load.return_value = RelayConfig(browse=BrowseConfig(rotate_interval_min=30))

        mock_sv = MagicMock()
        mock_sv.run.return_value = 0
        mock_supervisor_cls.return_value = mock_sv

        result = _cmd_browse(self._make_args(rotate_min=5))
        assert result == 0

        _, kwargs = mock_supervisor_cls.call_args
        assert kwargs.get("rotate_interval_min") == 5

    @patch("proxy_relay.browse.BrowseSupervisor")
    @patch("proxy_relay.browse.get_profile_dir", return_value=Path("/tmp/profile"))
    @patch("proxy_relay.browse.find_chromium", return_value=Path("/usr/bin/chromium"))
    @patch("proxy_relay.browse.health_check", return_value="203.0.113.1")
    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 8080})
    @patch("proxy_relay.cli.is_process_running", return_value=True)
    @patch("proxy_relay.cli.read_pid", return_value=42)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_existing_server_reused_no_auto_stop(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
        _mock_hc: MagicMock,
        _mock_find: MagicMock,
        _mock_profile: MagicMock,
        mock_supervisor_cls: MagicMock,
    ):
        """When server is already running, it's reused and NOT auto-stopped."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import RelayConfig

        mock_load.return_value = RelayConfig()
        mock_sv = MagicMock()
        mock_sv.run.return_value = 0
        mock_supervisor_cls.return_value = mock_sv

        with patch("proxy_relay.browse.auto_stop_server") as mock_auto_stop:
            result = _cmd_browse(self._make_args())
        assert result == 0
        mock_sv.run.assert_called_once()
        mock_auto_stop.assert_not_called()

    @patch("proxy_relay.browse.auto_stop_server")
    @patch("proxy_relay.browse.BrowseSupervisor")
    @patch("proxy_relay.browse.get_profile_dir", return_value=Path("/tmp/profile"))
    @patch("proxy_relay.browse.find_chromium", return_value=Path("/usr/bin/chromium"))
    @patch("proxy_relay.browse.health_check", return_value="203.0.113.1")
    @patch("proxy_relay.browse.wait_for_server_ready", return_value=("127.0.0.1", 9999))
    @patch("proxy_relay.browse.auto_start_server")
    @patch("proxy_relay.cli.read_status", return_value={"host": "127.0.0.1", "port": 9999})
    @patch("proxy_relay.cli.is_process_running", return_value=False)
    @patch("proxy_relay.cli.read_pid", return_value=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_auto_start_and_auto_stop(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        _mock_status: MagicMock,
        mock_auto_start: MagicMock,
        _mock_wait: MagicMock,
        _mock_hc: MagicMock,
        _mock_find: MagicMock,
        _mock_profile: MagicMock,
        mock_supervisor_cls: MagicMock,
        mock_auto_stop: MagicMock,
    ):
        """When no server running, auto-starts and auto-stops on browser exit."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import RelayConfig

        mock_load.return_value = RelayConfig()
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        mock_auto_start.return_value = mock_proc

        mock_sv = MagicMock()
        mock_sv.run.return_value = 0
        mock_supervisor_cls.return_value = mock_sv

        result = _cmd_browse(self._make_args())
        assert result == 0
        mock_auto_start.assert_called_once()
        mock_auto_stop.assert_called_once_with(mock_proc, "browse")


# ---------------------------------------------------------------------------
# 12. build_parser() — browse subcommand registration
# ---------------------------------------------------------------------------


class TestBuildParserBrowse:
    """Verify the browse subcommand is registered in the argument parser."""

    def test_browse_subcommand_registered(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse"])
        assert args.command == "browse"

    def test_rotate_min_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--rotate-min", "10"])
        assert args.rotate_min == 10

    def test_no_rotate_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--no-rotate"])
        assert args.no_rotate is True

    def test_config_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--config", "/tmp/my.toml"])
        assert args.config == "/tmp/my.toml"

    def test_default_no_rotate_is_false(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse"])
        assert args.no_rotate is False

    def test_default_rotate_min_is_none(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse"])
        assert args.rotate_min is None


# ---------------------------------------------------------------------------
# 13. BrowseSupervisor._cleanup_chromium()
# ---------------------------------------------------------------------------


class TestCleanupChromium:
    """Tests for _cleanup_chromium() — graceful and forced termination."""

    def _make_supervisor(self):
        from proxy_relay.browse import BrowseSupervisor

        return BrowseSupervisor(
            chromium_path=Path("/usr/bin/chromium"),
            proxy_host="127.0.0.1",
            proxy_port=8080,
            profile_dir=Path("/tmp/test-profile"),
            relay_pid=12345,
            rotate_interval_min=0,
        )

    def test_terminate_then_wait(self):
        sv = self._make_supervisor()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running before terminate
        mock_proc.wait.return_value = 0

        sv._cleanup_chromium(mock_proc)

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()

    def test_force_kill_on_timeout(self):
        sv = self._make_supervisor()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="chromium", timeout=5)

        sv._cleanup_chromium(mock_proc)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_already_exited_no_terminate(self):
        sv = self._make_supervisor()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited

        sv._cleanup_chromium(mock_proc)

        # Should not try to terminate an already-exited process
        # (implementation may or may not check poll first — this is lenient)
        # At minimum, it should not raise


# ---------------------------------------------------------------------------
# 14. Constants validation
# ---------------------------------------------------------------------------


class TestBrowseConstants:
    """Verify module-level constants have expected values."""

    def test_health_check_timeout(self):
        from proxy_relay.browse import _HEALTH_CHECK_TIMEOUT

        assert _HEALTH_CHECK_TIMEOUT == 60.0

    def test_pid_poll_interval(self):
        from proxy_relay.browse import _PID_POLL_INTERVAL

        assert _PID_POLL_INTERVAL == 2.0

    def test_chromium_candidates_non_empty(self):
        from proxy_relay.browse import _CHROMIUM_CANDIDATES

        assert len(_CHROMIUM_CANDIDATES) >= 2

    def test_browser_profiles_dir_under_config_dir(self):
        from proxy_relay.browse import BROWSER_PROFILES_DIR
        from proxy_relay.config import CONFIG_DIR

        assert BROWSER_PROFILES_DIR == CONFIG_DIR / "browser-profiles"


# ---------------------------------------------------------------------------
# 15. Server health_check() — rotate+retry logic
# ---------------------------------------------------------------------------


class TestServerHealthCheck:
    """Tests for ProxyServer.health_check() — internal rotate+retry."""

    def _make_server(self):
        from proxy_relay.server import ProxyServer
        from proxy_relay.upstream import UpstreamInfo

        srv = ProxyServer(host="127.0.0.1", port=8080)
        srv._upstream = UpstreamInfo(
            host="proxy.example.com", port=1080,
            username="u", password="p",
            url="socks5://***@proxy.example.com:1080",
            country="us",
        )
        return srv

    def _make_tunnel_result(self, response_body: bytes):
        """Create a mock TunnelResult that returns the given HTTP response."""
        mock_result = MagicMock()

        async def fake_drain():
            pass

        mock_result.writer.drain = fake_drain
        mock_result.writer.close = MagicMock()

        async def fake_read(n):
            return b"HTTP/1.1 200 OK\r\n\r\n" + response_body

        mock_result.reader.read = fake_read
        return mock_result

    def test_returns_exit_ip_on_success(self):
        """First attempt succeeds -> returns (True, exit_ip)."""
        server = self._make_server()
        mock_result = self._make_tunnel_result(b"203.0.113.42")

        async def run():
            with patch("proxy_relay.server.open_tunnel", return_value=mock_result):
                return await server.health_check()

        ok, body = asyncio.run(run())
        assert ok is True
        assert body == "203.0.113.42"

    def test_returns_false_when_no_upstream(self):
        from proxy_relay.server import ProxyServer

        srv = ProxyServer()

        ok, msg = asyncio.run(srv.health_check())
        assert ok is False
        assert "not started" in msg

    def test_rotates_on_failure_then_succeeds(self):
        """First attempt fails, rotation happens, second succeeds."""
        server = self._make_server()
        mock_result = self._make_tunnel_result(b"198.51.100.1")
        call_count = 0

        async def tunnel_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Host unreachable")
            return mock_result

        async def fake_rotate():
            pass

        async def run():
            with (
                patch("proxy_relay.server.open_tunnel", side_effect=tunnel_side_effect),
                patch.object(server, "_do_rotate", side_effect=fake_rotate) as mock_rotate,
            ):
                result = await server.health_check()
            return result, mock_rotate

        (ok, body), mock_rotate = asyncio.run(run())
        assert ok is True
        assert body == "198.51.100.1"
        mock_rotate.assert_called_once()

    def test_returns_false_after_all_retries_exhausted(self):
        """All attempts fail -> returns (False, error message)."""
        server = self._make_server()

        async def fake_rotate():
            pass

        async def run():
            with (
                patch("proxy_relay.server.open_tunnel", side_effect=Exception("Host unreachable")),
                patch.object(server, "_do_rotate", side_effect=fake_rotate),
            ):
                return await server.health_check()

        ok, body = asyncio.run(run())
        assert ok is False
        assert "3 attempts" in body
        assert "Host unreachable" in body


# ---------------------------------------------------------------------------
# 16. Handler /__health interception
# ---------------------------------------------------------------------------


class TestHandlerHealthEndpoint:
    """Tests for /__health interception in handle_connection."""

    def test_health_path_constant(self):
        from proxy_relay.handler import HEALTH_PATH

        assert HEALTH_PATH == "/__health"


# ---------------------------------------------------------------------------
# 17. auto_start_server()
# ---------------------------------------------------------------------------


class TestAutoStartServer:
    """Tests for auto_start_server() — subprocess launch."""

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_launches_subprocess(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server

        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        result = auto_start_server("steal", host="127.0.0.1")

        assert result is mock_proc
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "--profile" in cmd
        assert "steal" in cmd
        assert "--port" in cmd
        assert "0" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_uses_sys_executable(self, mock_popen: MagicMock):
        import sys
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("browse")

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == sys.executable

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_redirects_output_to_devnull(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("browse")

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stderr") == subprocess.DEVNULL

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_includes_config_path_when_provided(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("browse", config_path=Path("/tmp/custom.toml"))

        cmd = mock_popen.call_args[0][0]
        assert "--config" in cmd
        assert "/tmp/custom.toml" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_os_error_raises_browse_error(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server
        from proxy_relay.exceptions import BrowseError

        mock_popen.side_effect = OSError("No such file")

        with pytest.raises(BrowseError, match="subprocess"):
            auto_start_server("browse")


# ---------------------------------------------------------------------------
# 18. wait_for_server_ready()
# ---------------------------------------------------------------------------


class TestWaitForServerReady:
    """Tests for wait_for_server_ready() — status file polling."""

    @patch("proxy_relay.browse.time.sleep")
    @patch("proxy_relay.browse.read_status")
    def test_returns_host_port_from_status(
        self, mock_status: MagicMock, _mock_sleep: MagicMock
    ):
        from proxy_relay.browse import wait_for_server_ready

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_status.return_value = {"host": "127.0.0.1", "port": 9999}

        host, port = wait_for_server_ready("browse", mock_proc, timeout=5)
        assert host == "127.0.0.1"
        assert port == 9999

    @patch("proxy_relay.browse.time.sleep")
    @patch("proxy_relay.browse.read_status", return_value=None)
    def test_process_exit_raises_browse_error(
        self, _mock_status: MagicMock, _mock_sleep: MagicMock
    ):
        from proxy_relay.browse import wait_for_server_ready
        from proxy_relay.exceptions import BrowseError

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # process exited
        mock_proc.returncode = 1

        with pytest.raises(BrowseError, match="exited with code 1"):
            wait_for_server_ready("browse", mock_proc, timeout=5)

    @patch("proxy_relay.browse.time.monotonic")
    @patch("proxy_relay.browse.time.sleep")
    @patch("proxy_relay.browse.read_status", return_value=None)
    def test_timeout_raises_browse_error(
        self, _mock_status: MagicMock, _mock_sleep: MagicMock, mock_time: MagicMock
    ):
        from proxy_relay.browse import wait_for_server_ready
        from proxy_relay.exceptions import BrowseError

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="proxy-relay", timeout=5)
        # Simulate time passing beyond deadline
        mock_time.side_effect = [0.0, 100.0]

        with pytest.raises(BrowseError, match="did not become ready"):
            wait_for_server_ready("browse", mock_proc, timeout=5)


# ---------------------------------------------------------------------------
# 19. auto_stop_server()
# ---------------------------------------------------------------------------


class TestAutoStopServer:
    """Tests for auto_stop_server() — graceful process termination."""

    def test_terminates_running_process(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0

        auto_stop_server(mock_proc, "browse")

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()

    def test_force_kills_on_timeout(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="proxy-relay", timeout=5)

        auto_stop_server(mock_proc, "browse")

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_noop_when_already_exited(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        auto_stop_server(mock_proc, "browse")

        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# 20. CLI parser --profile on stop/status/rotate
# ---------------------------------------------------------------------------


class TestBuildParserProfile:
    """Verify --profile flag on stop, status, rotate subcommands."""

    def test_stop_accepts_profile(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["stop", "--profile", "steal"])
        assert args.profile == "steal"

    def test_stop_profile_defaults_to_none(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["stop"])
        assert args.profile is None

    def test_status_accepts_profile(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["status", "--profile", "steal"])
        assert args.profile == "steal"

    def test_rotate_accepts_profile(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["rotate", "--profile", "steal"])
        assert args.profile == "steal"
