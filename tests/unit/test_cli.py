"""Tests for proxy_relay.cli -- command-line interface."""
from __future__ import annotations

import argparse
import json
import signal
from unittest.mock import MagicMock, patch

import pytest

from proxy_relay.cli import build_parser, _cmd_stop, _cmd_status, _cmd_rotate


class TestBuildParser:
    """Test argument parser construction."""

    def test_parser_has_version(self):
        """--version flag is registered."""
        parser = build_parser()
        version_actions = [
            a for a in parser._actions if isinstance(a, argparse._VersionAction)
        ]
        assert len(version_actions) == 1

    def test_parser_has_subcommands(self):
        """All 4 subcommands are registered."""
        parser = build_parser()
        for cmd in ("start", "stop", "status", "rotate"):
            args = parser.parse_args([cmd])
            assert args.command == cmd

    def test_start_accepts_host_port_profile(self):
        """Start subcommand accepts --host, --port, --profile, --log-level."""
        parser = build_parser()
        args = parser.parse_args([
            "start",
            "--host", "0.0.0.0",
            "--port", "9090",
            "--profile", "stealth",
            "--log-level", "DEBUG",
        ])
        assert args.host == "0.0.0.0"
        assert args.port == 9090
        assert args.profile == "stealth"
        assert args.log_level == "DEBUG"

    def test_start_defaults_are_none(self):
        """Start subcommand defaults are all None (config provides real defaults)."""
        parser = build_parser()
        args = parser.parse_args(["start"])
        assert args.host is None
        assert args.port is None
        assert args.profile is None
        assert args.log_level is None

    def test_status_accepts_json_flag(self):
        """Status subcommand accepts --json flag."""
        parser = build_parser()
        args = parser.parse_args(["status", "--json"])
        assert args.json_output is True

    def test_status_json_defaults_to_false(self):
        """Status subcommand --json defaults to False."""
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.json_output is False

    def test_no_command_returns_none(self):
        """No subcommand sets command to None."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    @pytest.mark.parametrize("bad_level", ["TRACE", "verbose", "info"])
    def test_start_rejects_invalid_log_level(self, bad_level):
        """Start subcommand rejects invalid --log-level values."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["start", "--log-level", bad_level])

    def test_version_string_contains_proxy_relay(self):
        """--version output contains 'proxy-relay'."""
        parser = build_parser()
        version_action = next(
            a for a in parser._actions if isinstance(a, argparse._VersionAction)
        )
        assert "proxy-relay" in version_action.version


class TestCmdStop:
    """Test _cmd_stop subcommand."""

    def test_no_pid_file_returns_1(self):
        """Returns 1 when no PID file exists."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_stop(args)
        assert result == 1

    def test_stale_pid_returns_1(self):
        """Returns 1 when PID exists but process is not running."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.pidfile.remove_pid"):
            result = _cmd_stop(args)
        assert result == 1

    def test_running_process_sends_sigterm(self):
        """Sends SIGTERM and returns 0 when process is running."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_stop(args)
        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGTERM)

    def test_signal_failure_returns_1(self):
        """Returns 1 when send_signal fails."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=False):
            result = _cmd_stop(args)
        assert result == 1


class TestCmdStatus:
    """Test _cmd_status subcommand."""

    def test_not_running_returns_1(self):
        """Returns 1 when not running."""
        args = argparse.Namespace(json_output=False)
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.read_status", return_value=None):
            result = _cmd_status(args)
        assert result == 1

    def test_running_returns_0(self):
        """Returns 0 when running."""
        args = argparse.Namespace(json_output=False)
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.read_status", return_value=None):
            result = _cmd_status(args)
        assert result == 0

    def test_json_output_returns_0_when_not_running(self):
        """JSON output returns 0 even when not running."""
        args = argparse.Namespace(json_output=True)
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.read_status", return_value=None):
            result = _cmd_status(args)
        assert result == 0

    def test_json_output_includes_status_data(self, capsys):
        """JSON output includes status data when available."""
        args = argparse.Namespace(json_output=True)
        status = {"host": "127.0.0.1", "port": 8080}
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.read_status", return_value=status):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["running"] is True
        assert output["pid"] == 12345
        assert output["host"] == "127.0.0.1"
        assert output["port"] == 8080

    def test_json_output_not_running_shows_false(self, capsys):
        """JSON output shows running=false when not running."""
        args = argparse.Namespace(json_output=True)
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.read_status", return_value=None):
            _cmd_status(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["running"] is False
        assert output["pid"] is None

    def test_status_displays_monitor_stats(self, capsys):
        """Status shows monitor stats when available."""
        args = argparse.Namespace(json_output=False)
        status = {
            "host": "127.0.0.1",
            "port": 8080,
            "upstream_url": "socks5://proxy:1234",
            "country": "us",
            "active_connections": 2,
            "total_connections": 10,
            "monitor": {
                "window_error_count": 1,
                "total_errors": 3,
                "total_rotations": 0,
                "avg_latency_ms": 150.5,
                "p95_latency_ms": 300.0,
            },
        }
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.read_status", return_value=status):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "Monitor" in captured.out
        assert "150ms" in captured.out or "151ms" in captured.out

    def test_stale_pid_shows_not_running(self, capsys):
        """Stale PID is displayed in human-readable output."""
        args = argparse.Namespace(json_output=False)
        with patch("proxy_relay.cli.read_pid", return_value=99999), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.read_status", return_value=None):
            result = _cmd_status(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "stale" in captured.out.lower()
        assert "99999" in captured.out


class TestCmdRotate:
    """Test _cmd_rotate subcommand."""

    def test_no_pid_returns_1(self):
        """Returns 1 when no PID file."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_rotate(args)
        assert result == 1

    def test_sends_sigusr1(self):
        """Sends SIGUSR1 and returns 0."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_rotate(args)
        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGUSR1)

    def test_stale_pid_returns_1(self):
        """Returns 1 when PID exists but process is not running."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=False):
            result = _cmd_rotate(args)
        assert result == 1

    def test_signal_failure_returns_1(self):
        """Returns 1 when send_signal fails."""
        args = argparse.Namespace()
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=False):
            result = _cmd_rotate(args)
        assert result == 1


class TestCmdStart:
    """Test _cmd_start subcommand -- selected aspects only."""

    def test_already_running_returns_1(self):
        """Returns 1 when another instance is already running."""
        from proxy_relay.cli import _cmd_start

        args = argparse.Namespace(
            host=None, port=None, profile=None, log_level=None, config=None,
        )
        with patch("proxy_relay.cli.read_pid", return_value=99999), \
             patch("proxy_relay.cli.is_process_running", return_value=True):
            result = _cmd_start(args)
        assert result == 1

    def test_config_error_returns_1(self):
        """Returns 1 on ConfigError."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.exceptions import ConfigError

        args = argparse.Namespace(
            host=None, port=None, profile=None, log_level=None, config=None,
        )
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.RelayConfig.load", side_effect=ConfigError("bad")):
            result = _cmd_start(args)
        assert result == 1

    def test_config_error_prints_to_stderr(self, capsys):
        """ConfigError message is printed to stderr."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.exceptions import ConfigError

        args = argparse.Namespace(
            host=None, port=None, profile=None, log_level=None, config=None,
        )
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.RelayConfig.load", side_effect=ConfigError("missing field")):
            _cmd_start(args)
        captured = capsys.readouterr()
        assert "missing field" in captured.err

    def test_start_accepts_config_flag(self):
        """--config flag is parsed and available on args."""
        parser = build_parser()
        args = parser.parse_args(["start", "--config", "/tmp/custom.toml"])
        assert args.config == "/tmp/custom.toml"

    def test_start_config_defaults_to_none(self):
        """--config defaults to None when not provided."""
        parser = build_parser()
        args = parser.parse_args(["start"])
        assert args.config is None


class TestMain:
    """Test main() entry point."""

    def test_no_command_prints_help_and_exits_0(self):
        """No command shows help and exits 0."""
        with patch("sys.argv", ["proxy-relay"]), \
             pytest.raises(SystemExit) as exc_info:
            from proxy_relay.cli import main
            main()
        assert exc_info.value.code == 0

    def test_unknown_command_exits_with_error(self):
        """Unknown subcommand causes argparse to exit with error."""
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["nonexistent"])
        assert exc_info.value.code == 2
