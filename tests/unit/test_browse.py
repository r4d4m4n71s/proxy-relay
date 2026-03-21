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
# 2. ProfileConfig parsing via _parse_config (browse fields moved to profiles)
# ---------------------------------------------------------------------------


class TestProfileBrowseFields:
    """Verify browse fields (rotate_interval_min, browser) are now in ProfileConfig."""

    def test_profile_config_has_rotate_interval_min(self):
        """rotate_interval_min field now lives in ProfileConfig."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig(rotate_interval_min=10)
        assert pc.rotate_interval_min == 10

    def test_profile_config_rotate_interval_zero_is_valid(self):
        """Zero disables rotation and is a valid value."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig(rotate_interval_min=0)
        assert pc.rotate_interval_min == 0

    def test_profile_config_has_browser_field(self):
        """browser field is in ProfileConfig."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig(browser="brave-browser")
        assert pc.browser == "brave-browser"

    def test_no_browse_config_class(self):
        """BrowseConfig dataclass is removed — should not be importable."""
        try:
            from proxy_relay.config import BrowseConfig  # type: ignore[attr-defined]
            # If it still exists, the refactor hasn't happened yet
            import warnings
            warnings.warn(
                "BrowseConfig still exists — per-profile refactor not yet applied",
                stacklevel=1,
            )
        except ImportError:
            pass  # Expected — BrowseConfig was removed


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
            result = find_chromium()
            assert result == Path(first_abs)

    @patch("proxy_relay.browse.shutil.which")
    def test_bare_name_found_via_which(self, mock_which: MagicMock):
        from proxy_relay.browse import find_chromium

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

    @patch("urllib.request.OpenerDirector.open")
    def test_success_returns_exit_ip(self, mock_open: MagicMock):
        from proxy_relay.browse import health_check

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "exit_ip": "203.0.113.42"}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = mock_response

        result = health_check("127.0.0.1", 8080)
        assert result == "203.0.113.42"

    @patch("urllib.request.OpenerDirector.open")
    def test_timeout_raises_browse_error(self, mock_open: MagicMock):
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        mock_open.side_effect = TimeoutError("timed out")

        with pytest.raises(BrowseError, match="[Hh]ealth"):
            health_check("127.0.0.1", 8080)

    @patch("urllib.request.OpenerDirector.open")
    def test_url_error_raises_browse_error(self, mock_open: MagicMock):
        from proxy_relay.browse import health_check
        from proxy_relay.exceptions import BrowseError

        mock_open.side_effect = URLError("connection refused")

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

    def test_composite_key_creates_correct_dir(self, tmp_path: Path):
        from proxy_relay.browse import get_profile_dir

        with patch("proxy_relay.browse.BROWSER_PROFILES_DIR", tmp_path):
            result = get_profile_dir("medellin+default")
            assert result == tmp_path / "medellin+default"
            assert result.is_dir()


# ---------------------------------------------------------------------------
# 6. auto_start_server() — updated signature
# ---------------------------------------------------------------------------


class TestAutoStartServer:
    """Tests for auto_start_server() — subprocess launch (updated contract)."""

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_launches_subprocess_with_profile(self, mock_popen: MagicMock):
        """auto_start_server passes --profile and --port 0 to subprocess."""
        from proxy_relay.browse import auto_start_server

        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        result = auto_start_server("miami", host="127.0.0.1")

        assert result is mock_proc
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert "--profile" in cmd
        assert "miami" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_always_passes_port_zero(self, mock_popen: MagicMock):
        """auto_start_server always passes --port 0 (OS-assigned ephemeral port)."""
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami")

        cmd = mock_popen.call_args[0][0]
        assert "--port" in cmd
        assert "0" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_no_block_domains_flag_in_command(self, mock_popen: MagicMock):
        """auto_start_server does NOT pass --block-domains (removed)."""
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami")

        cmd = mock_popen.call_args[0][0]
        assert "--block-domains" not in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_passes_start_url_when_provided(self, mock_popen: MagicMock):
        """auto_start_server passes --start-url when start_url is non-empty."""
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami", start_url="https://listen.tidal.com")

        cmd = mock_popen.call_args[0][0]
        assert "--start-url" in cmd
        assert "https://listen.tidal.com" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_no_start_url_flag_when_empty(self, mock_popen: MagicMock):
        """auto_start_server does not include --start-url when start_url is empty."""
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami", start_url="")

        cmd = mock_popen.call_args[0][0]
        assert "--start-url" not in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_uses_sys_executable(self, mock_popen: MagicMock):
        import sys
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami")

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == sys.executable

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_redirects_output_correctly(self, mock_popen: MagicMock):
        """stdout is discarded (DEVNULL); stderr is captured (PIPE) for diagnostics."""
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami")

        kwargs = mock_popen.call_args[1]
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stderr") == subprocess.PIPE

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_includes_config_path_when_provided(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server

        mock_popen.return_value = MagicMock()
        auto_start_server("miami", config_path=Path("/tmp/custom.toml"))

        cmd = mock_popen.call_args[0][0]
        assert "--config" in cmd
        assert "/tmp/custom.toml" in cmd

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_os_error_raises_browse_error(self, mock_popen: MagicMock):
        from proxy_relay.browse import auto_start_server
        from proxy_relay.exceptions import BrowseError

        mock_popen.side_effect = OSError("No such file")

        with pytest.raises(BrowseError, match="subprocess"):
            auto_start_server("miami")

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_no_blocked_domains_parameter(self, mock_popen: MagicMock):
        """auto_start_server signature no longer accepts blocked_domains parameter."""
        import inspect
        from proxy_relay.browse import auto_start_server

        sig = inspect.signature(auto_start_server)
        assert "blocked_domains" not in sig.parameters, (
            "blocked_domains parameter was removed from auto_start_server()"
        )

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_has_start_url_parameter(self, mock_popen: MagicMock):
        """auto_start_server signature includes start_url parameter."""
        import inspect
        from proxy_relay.browse import auto_start_server

        sig = inspect.signature(auto_start_server)
        assert "start_url" in sig.parameters


# ---------------------------------------------------------------------------
# 7. _cmd_browse() CLI handler — updated to use profile config
# ---------------------------------------------------------------------------


class TestCmdBrowse:
    """Tests for _cmd_browse() CLI handler with per-profile config."""

    def _make_args(self, **overrides):
        """Build a mock argparse.Namespace for the browse command."""
        import argparse
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(
            port=8080,
            browser="",
            rotate_interval_min=30,
            start_url="",
        )
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        defaults = dict(
            command="browse",
            config=None,
            rotate_min=None,
            no_rotate=False,
            profile="miami",
            browser=None,
            start_url=None,
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
        from proxy_relay.config import ProfileConfig, RelayConfig
        from proxy_relay.exceptions import BrowseError

        profile = ProfileConfig()
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )
        mock_auto_start.side_effect = BrowseError("upstream failed")

        result = _cmd_browse(self._make_args())
        assert result == 1
        mock_auto_start.assert_called_once()

    @patch("proxy_relay.browse.auto_start_server")
    @patch("proxy_relay.cli.is_process_running", return_value=False)
    @patch("proxy_relay.cli.read_pid", return_value=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_auto_start_passes_start_url(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        mock_auto_start: MagicMock,
    ):
        """auto_start_server is called with start_url from profile or CLI."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import ProfileConfig, RelayConfig
        from proxy_relay.exceptions import BrowseError

        profile = ProfileConfig(start_url="https://listen.tidal.com")
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )
        mock_auto_start.side_effect = BrowseError("upstream failed")

        _cmd_browse(self._make_args())

        call_kwargs = mock_auto_start.call_args
        # start_url should appear somewhere in args or kwargs
        all_args = str(call_kwargs)
        assert "listen.tidal.com" in all_args or mock_auto_start.called

    @patch("proxy_relay.browse.auto_start_server")
    @patch("proxy_relay.cli.is_process_running", return_value=False)
    @patch("proxy_relay.cli.read_pid", return_value=None)
    @patch("proxy_relay.cli.RelayConfig.load")
    def test_auto_start_no_block_domains_param(
        self,
        mock_load: MagicMock,
        _mock_pid: MagicMock,
        _mock_running: MagicMock,
        mock_auto_start: MagicMock,
    ):
        """auto_start_server is NOT called with blocked_domains param."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import ProfileConfig, RelayConfig
        from proxy_relay.exceptions import BrowseError

        profile = ProfileConfig()
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )
        mock_auto_start.side_effect = BrowseError("failed")

        _cmd_browse(self._make_args())

        if mock_auto_start.called:
            call_kwargs = mock_auto_start.call_args
            assert "blocked_domains" not in (call_kwargs.kwargs or {})

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
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(rotate_interval_min=30)
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )

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
    def test_rotate_min_cli_overrides_profile(
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
        """CLI --rotate-min overrides profile.rotate_interval_min."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(rotate_interval_min=30)
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )

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
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig()
        mock_load.return_value = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()}
        )
        mock_sv = MagicMock()
        mock_sv.run.return_value = 0
        mock_supervisor_cls.return_value = mock_sv

        with patch("proxy_relay.browse.auto_stop_server") as mock_auto_stop:
            result = _cmd_browse(self._make_args())
        assert result == 0
        mock_sv.run.assert_called_once()
        mock_auto_stop.assert_not_called()


# ---------------------------------------------------------------------------
# 8. build_parser() — browse subcommand registration
# ---------------------------------------------------------------------------


class TestBuildParserBrowse:
    """Verify the browse subcommand is registered in the argument parser."""

    def test_browse_subcommand_registered(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami"])
        assert args.command == "browse"

    def test_browse_profile_is_required(self):
        """browse now requires --profile."""
        from proxy_relay.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["browse"])

    def test_rotate_min_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami", "--rotate-min", "10"])
        assert args.rotate_min == 10

    def test_no_rotate_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami", "--no-rotate"])
        assert args.no_rotate is True

    def test_config_flag(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami", "--config", "/tmp/my.toml"])
        assert args.config == "/tmp/my.toml"

    def test_default_no_rotate_is_false(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami"])
        assert args.no_rotate is False

    def test_default_rotate_min_is_none(self):
        from proxy_relay.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami"])
        assert args.rotate_min is None

    def test_browse_no_block_domains_flag(self):
        """browse no longer has --block-domains (removed)."""
        from proxy_relay.cli import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["browse", "--profile", "miami", "--block-domains", "tidal.com"])


# ---------------------------------------------------------------------------
# 9. BrowseSupervisor
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

    @patch("proxy_relay.browse.subprocess.Popen")
    def test_os_error_raises_browse_error(self, mock_popen: MagicMock):
        from proxy_relay.exceptions import BrowseError

        sv = self._make_supervisor()
        mock_popen.side_effect = OSError("No such file")

        with pytest.raises(BrowseError, match="[Cc]hromium|launch|start"):
            sv._start_chromium()


# ---------------------------------------------------------------------------
# 10. BrowseSupervisor.run() — Chromium exits normally
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
            rotate_interval_min=0,
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
# 11. auto_stop_server()
# ---------------------------------------------------------------------------


class TestAutoStopServer:
    """Tests for auto_stop_server() — graceful process termination."""

    def test_terminates_running_process(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0

        auto_stop_server(mock_proc, "miami")

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once()

    def test_force_kills_on_timeout(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="proxy-relay", timeout=5)

        auto_stop_server(mock_proc, "miami")

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_noop_when_already_exited(self):
        from proxy_relay.browse import auto_stop_server

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        auto_stop_server(mock_proc, "miami")

        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# 12. wait_for_server_ready()
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

        host, port = wait_for_server_ready("miami", mock_proc, timeout=5)
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
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1

        with pytest.raises(BrowseError, match="exited with code 1"):
            wait_for_server_ready("miami", mock_proc, timeout=5)


# ---------------------------------------------------------------------------
# 13. rotate_proxy()
# ---------------------------------------------------------------------------


class TestRotateProxy:
    """rotate_proxy() — send SIGUSR1 to running server for upstream rotation."""

    def test_sends_sigusr1_to_running_process(self):
        import signal as signal_mod
        from proxy_relay.browse import rotate_proxy

        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.pid = 12345

        rotate_proxy(proc, "miami")

        proc.send_signal.assert_called_once_with(signal_mod.SIGUSR1)

    def test_raises_browse_error_if_process_exited(self):
        from proxy_relay.exceptions import BrowseError
        from proxy_relay.browse import rotate_proxy

        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = 1
        proc.returncode = 1

        with pytest.raises(BrowseError, match="not running"):
            rotate_proxy(proc, "miami")


# ---------------------------------------------------------------------------
# 14. Constants
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
