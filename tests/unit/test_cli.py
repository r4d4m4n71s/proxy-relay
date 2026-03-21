"""Tests for proxy_relay.cli -- command-line interface."""
from __future__ import annotations

import argparse
import json
import signal
from unittest.mock import MagicMock, patch

import pytest

from proxy_relay.cli import build_parser, _cmd_stop, _cmd_status, _cmd_rotate


# ---------------------------------------------------------------------------
# TestBuildParser — parser structure
# ---------------------------------------------------------------------------


class TestBuildParser:
    """Test argument parser construction."""

    def test_parser_has_version(self):
        """--version flag is registered."""
        parser = build_parser()
        version_actions = [
            a for a in parser._actions if isinstance(a, argparse._VersionAction)
        ]
        assert len(version_actions) == 1

    def test_parser_has_all_subcommands(self):
        """All expected subcommands are registered."""
        parser = build_parser()
        for cmd in ("start", "stop", "status", "rotate", "browse"):
            args = parser.parse_args(
                [cmd, "--profile", "miami"] if cmd not in ("status",) else [cmd]
            )
            assert args.command == cmd
        # block/unblock also require --domains
        for cmd in ("block", "unblock"):
            args = parser.parse_args([cmd, "--profile", "miami", "--domains", "example.com"])
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

    def test_start_host_port_default_to_none(self):
        """Start --host and --port default to None (config provides real values)."""
        parser = build_parser()
        args = parser.parse_args(["start", "--profile", "miami"])
        assert args.host is None
        assert args.port is None

    def test_start_profile_is_required(self):
        """Start subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["start"])

    def test_stop_profile_is_required(self):
        """Stop subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["stop"])

    def test_rotate_profile_is_required(self):
        """Rotate subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["rotate"])

    def test_browse_profile_is_required(self):
        """Browse subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["browse"])

    def test_block_profile_is_required(self):
        """Block subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["block", "--domains", "example.com"])

    def test_unblock_profile_is_required(self):
        """Unblock subcommand requires --profile."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["unblock", "--domains", "example.com"])

    def test_status_profile_is_optional(self):
        """Status subcommand allows --profile as optional filter."""
        parser = build_parser()
        # Without --profile must succeed
        args = parser.parse_args(["status"])
        assert args.profile is None

    def test_status_profile_accepted_as_filter(self):
        """Status --profile is accepted as optional filter."""
        parser = build_parser()
        args = parser.parse_args(["status", "--profile", "miami"])
        assert args.profile == "miami"

    def test_status_no_all_flag(self):
        """Status does NOT have --all flag (removed in per-profile refactor)."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["status", "--all"])

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

    def test_start_no_block_domains_flag(self):
        """Start subcommand does NOT have --block-domains flag (removed)."""
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["start", "--profile", "miami", "--block-domains", "tidal.com"])

    def test_start_has_hidden_start_url_flag(self):
        """Start subcommand has hidden --start-url flag for auto_start_server()."""
        parser = build_parser()
        args = parser.parse_args([
            "start", "--profile", "miami", "--start-url", "https://listen.tidal.com"
        ])
        assert args.start_url == "https://listen.tidal.com"

    def test_start_url_defaults_to_empty_string(self):
        """--start-url defaults to empty string when not provided."""
        parser = build_parser()
        args = parser.parse_args(["start", "--profile", "miami"])
        assert args.start_url == ""

    def test_block_accepts_domains_flag(self):
        """Block subcommand accepts --domains flag."""
        parser = build_parser()
        args = parser.parse_args(["block", "--profile", "miami", "--domains", "example.com,other.org"])
        assert args.domains == "example.com,other.org"
        assert args.profile == "miami"

    def test_unblock_accepts_domains_flag(self):
        """Unblock subcommand accepts --domains flag."""
        parser = build_parser()
        args = parser.parse_args(["unblock", "--profile", "miami", "--domains", "tidal.com"])
        assert args.domains == "tidal.com"
        assert args.profile == "miami"

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
            parser.parse_args(["start", "--profile", "miami", "--log-level", bad_level])

    def test_version_string_contains_proxy_relay(self):
        """--version output contains 'proxy-relay'."""
        parser = build_parser()
        version_action = next(
            a for a in parser._actions if isinstance(a, argparse._VersionAction)
        )
        assert "proxy-relay" in version_action.version


# ---------------------------------------------------------------------------
# TestCmdStop
# ---------------------------------------------------------------------------


class TestCmdStop:
    """Test _cmd_stop subcommand."""

    def test_no_pid_file_returns_1(self):
        """Returns 1 when no PID file exists."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_stop(args)
        assert result == 1

    def test_stale_pid_returns_1(self):
        """Returns 1 when PID exists but process is not running."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.remove_pid"):
            result = _cmd_stop(args)
        assert result == 1

    def test_running_process_sends_sigterm(self):
        """Sends SIGTERM and returns 0 when process is running."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_stop(args)
        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGTERM)

    def test_signal_failure_returns_1(self):
        """Returns 1 when send_signal fails."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=False):
            result = _cmd_stop(args)
        assert result == 1


# ---------------------------------------------------------------------------
# TestCmdStatus — show-all-by-default behavior
# ---------------------------------------------------------------------------


class TestCmdStatus:
    """Test _cmd_status subcommand — shows all by default."""

    def test_no_profile_shows_all_instances(self, capsys):
        """status without --profile shows all running instances."""
        args = argparse.Namespace(json_output=False, profile=None)
        mock_statuses = [
            {"profile": "miami", "running": True, "pid": 1234,
             "host": "127.0.0.1", "port": 8081},
        ]
        with patch("proxy_relay.cli.scan_all_status", return_value=mock_statuses):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "miami" in captured.out

    def test_no_instances_returns_0_with_message(self, capsys):
        """status with no running instances returns 0 and prints a message."""
        args = argparse.Namespace(json_output=False, profile=None)
        with patch("proxy_relay.cli.scan_all_status", return_value=[]):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        # The actual message is "No proxy-relay instances found."
        assert (
            "no proxy-relay instances" in captured.out.lower()
            or "instances found" in captured.out.lower()
            or "not found" in captured.out.lower()
        )

    def test_with_profile_filter_shows_only_that_profile(self, capsys):
        """status --profile miami shows only miami's status."""
        args = argparse.Namespace(json_output=False, profile="miami")
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(True, 1234, {
            "host": "127.0.0.1", "port": 8081,
            "profile": "miami", "country": "co",
            "active_connections": 0, "total_connections": 0,
        })):
            result = _cmd_status(args)
        assert result == 0

    def test_profile_filter_not_running_returns_1(self):
        """status --profile miami returns 1 when miami is not running."""
        args = argparse.Namespace(json_output=False, profile="miami")
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(False, None, None)):
            result = _cmd_status(args)
        assert result == 1

    def test_json_output_all_profiles(self, capsys):
        """status --json with no profile outputs JSON array of all profiles."""
        args = argparse.Namespace(json_output=True, profile=None)
        mock_statuses = [
            {"profile": "miami", "running": True, "pid": 1234,
             "host": "127.0.0.1", "port": 8081},
            {"profile": "medellin", "running": False, "pid": None},
        ]
        with patch("proxy_relay.cli.scan_all_status", return_value=mock_statuses):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert len(output) == 2
        assert output[0]["profile"] == "miami"
        assert output[1]["profile"] == "medellin"

    def test_json_output_with_profile_filter(self, capsys):
        """status --profile miami --json outputs JSON for miami."""
        args = argparse.Namespace(json_output=True, profile="miami")
        status = {"host": "127.0.0.1", "port": 8081, "profile": "miami"}
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(True, 1234, status)):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["running"] is True
        assert output["pid"] == 1234

    def test_stale_pid_shows_not_running(self, capsys):
        """Stale PID is displayed in human-readable output when --profile given."""
        args = argparse.Namespace(json_output=False, profile="miami")
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(False, 99999, None)):
            result = _cmd_status(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "stale" in captured.out.lower() or "not running" in captured.out.lower()

    def test_status_displays_monitor_stats(self, capsys):
        """Status shows monitor stats when available."""
        args = argparse.Namespace(json_output=False, profile="miami")
        status = {
            "host": "127.0.0.1",
            "port": 8081,
            "upstream_url": "socks5://proxy:1234",
            "country": "co",
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
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(True, 12345, status)):
            result = _cmd_status(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "Monitor" in captured.out
        assert "150ms" in captured.out or "151ms" in captured.out


# ---------------------------------------------------------------------------
# TestCmdRotate
# ---------------------------------------------------------------------------


class TestCmdRotate:
    """Test _cmd_rotate subcommand."""

    def test_no_pid_returns_1(self):
        """Returns 1 when no PID file."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_rotate(args)
        assert result == 1

    def test_sends_sigusr1(self):
        """Sends SIGUSR1 and returns 0."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_rotate(args)
        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGUSR1)

    def test_stale_pid_returns_1(self):
        """Returns 1 when PID exists but process is not running."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=False):
            result = _cmd_rotate(args)
        assert result == 1

    def test_signal_failure_returns_1(self):
        """Returns 1 when send_signal fails."""
        args = argparse.Namespace(profile="miami")
        with patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=False):
            result = _cmd_rotate(args)
        assert result == 1


# ---------------------------------------------------------------------------
# TestCmdStart — mandatory --profile, port/blocked from profile
# ---------------------------------------------------------------------------


class TestCmdStart:
    """Test _cmd_start subcommand with mandatory profile."""

    def test_already_running_returns_1(self):
        """Returns 1 when another instance is already running."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(port=8081)
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        args = argparse.Namespace(
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="",
        )
        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.read_pid", return_value=99999), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.configure_logging"):
            result = _cmd_start(args)
        assert result == 1

    def test_config_error_returns_1(self):
        """Returns 1 on ConfigError."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.exceptions import ConfigError

        args = argparse.Namespace(
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="",
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
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="",
        )
        with patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.RelayConfig.load", side_effect=ConfigError("missing field")):
            _cmd_start(args)
        captured = capsys.readouterr()
        assert "missing field" in captured.err

    def test_start_accepts_config_flag(self):
        """--config flag is parsed and available on args."""
        parser = build_parser()
        args = parser.parse_args(["start", "--profile", "miami", "--config", "/tmp/custom.toml"])
        assert args.config == "/tmp/custom.toml"

    def test_start_config_defaults_to_none(self):
        """--config defaults to None when not provided."""
        parser = build_parser()
        args = parser.parse_args(["start", "--profile", "miami"])
        assert args.config is None

    def test_port_precedence_cli_over_profile(self):
        """CLI --port overrides profile.port."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(port=8081)
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        args = argparse.Namespace(
            host=None, port=9999, profile="miami", log_level=None,
            config=None, start_url="",
        )

        created_servers = []

        class FakeProxyServer:
            def __init__(self, host, port, **kwargs):
                created_servers.append(port)

        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.cli.ProxyServer", FakeProxyServer), \
             patch("proxy_relay.cli.asyncio.run", side_effect=KeyboardInterrupt):
            _cmd_start(args)

        # CLI port 9999 should take precedence over profile port 8081
        if created_servers:
            assert created_servers[0] == 9999

    def test_start_url_arg_passed_to_resolve_blocked(self):
        """Hidden --start-url is used to resolve blocked domains."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig(port=8080, blocked_domains=["tidal.com"])
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        args = argparse.Namespace(
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="https://listen.tidal.com",
        )

        resolve_calls = []

        def fake_resolve(prof, start_url=""):
            resolve_calls.append(start_url)
            return None

        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.cli.resolve_blocked_domains", side_effect=fake_resolve), \
             patch("proxy_relay.cli.asyncio.run", side_effect=KeyboardInterrupt):
            _cmd_start(args)

        if resolve_calls:
            assert resolve_calls[0] == "https://listen.tidal.com"


# ---------------------------------------------------------------------------
# TestBlockCommand — _cmd_block()
# ---------------------------------------------------------------------------


class TestBlockCommand:
    """Test _cmd_block subcommand."""

    def test_block_adds_domains_to_config(self, tmp_path):
        """Writes new domains to config.toml via tomlkit."""
        from proxy_relay.cli import _cmd_block
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        # Write minimal config with profiles.miami section
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com"]\n'
            "\n"
            "[profiles.miami]\n"
            'blocked_domains = ["tidal.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="example.com,other.org",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_block(args)

        assert result == 0
        updated = config_path.read_text()
        assert "example.com" in updated
        assert "other.org" in updated

    def test_block_sends_sigusr2_to_running_server(self, tmp_path):
        """Block command sends SIGUSR2 to the running server."""
        from proxy_relay.cli import _cmd_block
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = []\n'
            "\n"
            "[profiles.miami]\n"
        )

        args = argparse.Namespace(
            profile="miami",
            domains="example.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_block(args)

        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGUSR2)

    def test_block_server_not_running_still_writes_config(self, tmp_path, capsys):
        """block prints a warning but still writes config when server is not running."""
        from proxy_relay.cli import _cmd_block
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = []\n'
            "\n"
            "[profiles.miami]\n"
        )

        args = argparse.Namespace(
            profile="miami",
            domains="example.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_block(args)

        assert result == 0
        updated = config_path.read_text()
        assert "example.com" in updated

    def test_block_creates_profile_section_if_missing(self, tmp_path):
        """block creates [profiles.miami] section if it doesn't exist."""
        from proxy_relay.cli import _cmd_block
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        # Only profiles.default exists, no profiles.miami
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="example.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_block(args)

        assert result == 0
        updated = config_path.read_text()
        assert "example.com" in updated


# ---------------------------------------------------------------------------
# TestUnblockCommand — _cmd_unblock()
# ---------------------------------------------------------------------------


class TestUnblockCommand:
    """Test _cmd_unblock subcommand."""

    def test_unblock_removes_domains_from_config(self, tmp_path):
        """Removes specified domains from the profile's blocked list."""
        from proxy_relay.cli import _cmd_unblock
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com", "listen.tidal.com", "example.com"]\n'
            "\n"
            "[profiles.miami]\n"
            'blocked_domains = ["tidal.com", "listen.tidal.com", "example.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="tidal.com,listen.tidal.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_unblock(args)

        assert result == 0
        updated = config_path.read_text()
        # example.com should remain; tidal.com/listen.tidal.com removed
        assert "example.com" in updated

    def test_unblock_creates_profile_section_if_missing(self, tmp_path):
        """Unblock creates [profiles.miami] if it doesn't exist."""
        from proxy_relay.cli import _cmd_unblock
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com", "listen.tidal.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="tidal.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_unblock(args)

        assert result == 0

    def test_unblock_sends_sigusr2(self, tmp_path):
        """Unblock sends SIGUSR2 to running server."""
        from proxy_relay.cli import _cmd_unblock
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com"]\n'
            "\n"
            "[profiles.miami]\n"
            'blocked_domains = ["tidal.com", "example.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="tidal.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=12345), \
             patch("proxy_relay.cli.is_process_running", return_value=True), \
             patch("proxy_relay.cli.send_signal", return_value=True) as mock_send:
            result = _cmd_unblock(args)

        assert result == 0
        mock_send.assert_called_once_with(12345, signal.SIGUSR2)

    def test_unblock_server_not_running_still_writes_config(self, tmp_path):
        """Unblock writes config even when server is not running."""
        from proxy_relay.cli import _cmd_unblock
        from proxy_relay import config as _config

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            'blocked_domains = ["tidal.com"]\n'
            "\n"
            "[profiles.miami]\n"
            'blocked_domains = ["tidal.com", "example.com"]\n'
        )

        args = argparse.Namespace(
            profile="miami",
            domains="tidal.com",
            config=str(config_path),
        )

        with patch.object(_config, "CONFIG_PATH", config_path), \
             patch("proxy_relay.cli.read_pid", return_value=None):
            result = _cmd_unblock(args)

        assert result == 0


# ---------------------------------------------------------------------------
# TestCmdStatusWithReadStatusIfAlive — per-profile read path
# ---------------------------------------------------------------------------


class TestCmdStatusWithReadStatusIfAlive:
    """_cmd_status uses read_status_if_alive for single-profile queries."""

    def test_stale_pid_cleaned_up_in_status(self, capsys):
        """Stale PID causes read_status_if_alive to clean up and return not running."""
        args = argparse.Namespace(json_output=False, profile="miami")
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(False, 99999, None)):
            result = _cmd_status(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "not running" in captured.out.lower()

    def test_alive_returns_status_data(self, capsys):
        """Running process returns data from read_status_if_alive."""
        status = {"host": "127.0.0.1", "port": 8081, "upstream_url": "socks5://p:1080",
                  "country": "co", "active_connections": 1, "total_connections": 5}
        args = argparse.Namespace(json_output=True, profile="miami")
        with patch("proxy_relay.cli.read_status_if_alive", return_value=(True, 12345, status)):
            result = _cmd_status(args)
        assert result == 0
        output = json.loads(capsys.readouterr().out)
        assert output["running"] is True
        assert output["host"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# TestSigpipeHandling
# ---------------------------------------------------------------------------


class TestSigpipeHandling:
    """main() installs SIGPIPE handler."""

    def test_sigpipe_ignored_after_main(self):
        """After main() runs, SIGPIPE should be SIG_IGN (Linux only)."""
        import signal as _signal

        if not hasattr(_signal, "SIGPIPE"):
            pytest.skip("SIGPIPE not available on this platform")

        original = _signal.getsignal(_signal.SIGPIPE)
        try:
            from proxy_relay.cli import main

            with patch("sys.argv", ["proxy-relay"]), \
                 pytest.raises(SystemExit):
                main()

            assert _signal.getsignal(_signal.SIGPIPE) is _signal.SIG_IGN
        finally:
            _signal.signal(_signal.SIGPIPE, original)


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# TestBrowseCapturePreCheck — capture dependency check still works
# ---------------------------------------------------------------------------


class TestBrowseCapturePreCheck:
    """--capture dependency check happens before server auto-start."""

    def test_capture_fails_early_if_deps_missing(self, capsys):
        """browse --capture returns 1 immediately when capture deps are missing."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig()
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        args = argparse.Namespace(
            config=None, profile="miami", rotate_min=None, no_rotate=False,
            capture=True, capture_domains=None, browser=None, start_url=None,
        )
        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.capture.is_capture_available", return_value=False):
            result = _cmd_browse(args)

        assert result == 1
        captured = capsys.readouterr()
        assert "capture requires optional dependencies" in captured.err.lower()

    def test_capture_pre_check_runs_before_server_start(self):
        """Capture deps check must run before auto_start_server is called."""
        from proxy_relay.cli import _cmd_browse
        from proxy_relay.config import ProfileConfig, RelayConfig

        profile = ProfileConfig()
        config = RelayConfig(profiles={"miami": profile, "default": ProfileConfig()})

        args = argparse.Namespace(
            config=None, profile="miami", rotate_min=None, no_rotate=False,
            capture=True, capture_domains=None, browser=None, start_url=None,
        )
        auto_start_called = []

        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.capture.is_capture_available", return_value=False), \
             patch("proxy_relay.cli._browse.auto_start_server",
                   side_effect=lambda *a, **kw: auto_start_called.append(True)):
            _cmd_browse(args)

        assert auto_start_called == []


# ---------------------------------------------------------------------------
# TestBuildParserProfile — --profile on various subcommands
# ---------------------------------------------------------------------------


class TestBuildParserProfile:
    """Verify --profile behavior across subcommands."""

    def test_stop_accepts_profile(self):
        parser = build_parser()
        args = parser.parse_args(["stop", "--profile", "steal"])
        assert args.profile == "steal"

    def test_status_accepts_profile(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--profile", "steal"])
        assert args.profile == "steal"

    def test_status_profile_defaults_to_none(self):
        """Status --profile defaults to None (shows all)."""
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.profile is None

    def test_rotate_accepts_profile(self):
        parser = build_parser()
        args = parser.parse_args(["rotate", "--profile", "steal"])
        assert args.profile == "steal"

    def test_browse_accepts_profile(self):
        parser = build_parser()
        args = parser.parse_args(["browse", "--profile", "miami"])
        assert args.profile == "miami"

    def test_block_accepts_profile_and_domains(self):
        parser = build_parser()
        args = parser.parse_args(["block", "--profile", "miami", "--domains", "tidal.com"])
        assert args.profile == "miami"
        assert args.domains == "tidal.com"

    def test_unblock_accepts_profile_and_domains(self):
        parser = build_parser()
        args = parser.parse_args(["unblock", "--profile", "miami", "--domains", "tidal.com"])
        assert args.profile == "miami"
        assert args.domains == "tidal.com"


# ---------------------------------------------------------------------------
# TestTimezoneCheck — --profile-aware timezone check
# ---------------------------------------------------------------------------


class TestTimezoneCheck:
    """F-RL9: timezone check uses profile from --profile flag."""

    def test_timezone_check_uses_profile_not_default(self):
        """Timezone check uses the --profile name, not a hardcoded default."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.config import AntiLeakConfig, ProfileConfig, RelayConfig

        profile = ProfileConfig(port=8081)
        config = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()},
        )
        config.anti_leak = AntiLeakConfig(warn_timezone_mismatch=True)

        args = argparse.Namespace(
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="",
        )

        pst_profile = MagicMock()
        pst_profile.country = "co"
        pst_config = MagicMock()
        pst_config.profiles = {"miami": pst_profile}

        import sys
        fake_pst_config_module = MagicMock()
        fake_pst_config_module.AppConfig.load.return_value = pst_config

        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.cli.asyncio.run", side_effect=KeyboardInterrupt), \
             patch("proxy_relay.cli.check_timezone_mismatch") as mock_tz, \
             patch.dict(sys.modules, {"proxy_st.config": fake_pst_config_module}):
            _cmd_start(args)

        mock_tz.assert_called_once_with("co")

    def test_timezone_check_exception_swallowed(self):
        """Exception during timezone check is swallowed with a warning."""
        from proxy_relay.cli import _cmd_start
        from proxy_relay.config import AntiLeakConfig, ProfileConfig, RelayConfig

        profile = ProfileConfig(port=8081)
        config = RelayConfig(
            profiles={"miami": profile, "default": ProfileConfig()},
        )
        config.anti_leak = AntiLeakConfig(warn_timezone_mismatch=True)

        args = argparse.Namespace(
            host=None, port=None, profile="miami", log_level=None,
            config=None, start_url="",
        )

        import sys
        broken_module = MagicMock()
        broken_module.AppConfig.load.side_effect = RuntimeError("pst unavailable")

        with patch("proxy_relay.cli.RelayConfig.load", return_value=config), \
             patch("proxy_relay.cli.read_pid", return_value=None), \
             patch("proxy_relay.cli.is_process_running", return_value=False), \
             patch("proxy_relay.cli.configure_logging"), \
             patch("proxy_relay.cli.asyncio.run", side_effect=KeyboardInterrupt), \
             patch.dict(sys.modules, {"proxy_st.config": broken_module}):
            # Should not raise
            _cmd_start(args)
