"""Tests for proxy_relay.pidfile — PID file and status file management."""
from __future__ import annotations

import json
import os
import stat

import pytest


# ---------------------------------------------------------------------------
# write_pid / read_pid
# ---------------------------------------------------------------------------
class TestPidFileWriteRead:
    """Test PID file creation and reading."""

    def test_write_pid_creates_file_with_current_pid(self, tmp_path):
        from proxy_relay.pidfile import write_pid

        path = tmp_path / "relay.pid"
        write_pid(path)

        content = path.read_text().strip()
        assert content == str(os.getpid())

    def test_read_pid_returns_correct_pid(self, tmp_path):
        from proxy_relay.pidfile import read_pid, write_pid

        path = tmp_path / "relay.pid"
        write_pid(path)

        pid = read_pid(path)
        assert pid == os.getpid()

    def test_read_pid_returns_none_for_missing_file(self, tmp_path):
        from proxy_relay.pidfile import read_pid

        path = tmp_path / "nonexistent.pid"
        assert read_pid(path) is None

    def test_read_pid_returns_none_for_non_numeric_content(self, tmp_path):
        from proxy_relay.pidfile import read_pid

        path = tmp_path / "relay.pid"
        path.write_text("not-a-number\n")

        assert read_pid(path) is None

    def test_pid_file_permissions_0600(self, tmp_path):
        from proxy_relay.pidfile import write_pid

        path = tmp_path / "relay.pid"
        write_pid(path)

        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# remove_pid
# ---------------------------------------------------------------------------
class TestRemovePid:
    """Test PID file removal."""

    def test_remove_pid_deletes_file(self, tmp_path):
        from proxy_relay.pidfile import remove_pid, write_pid

        path = tmp_path / "relay.pid"
        write_pid(path)
        assert path.exists()

        remove_pid(path)
        assert not path.exists()

    def test_remove_pid_noop_for_missing_file(self, tmp_path):
        from proxy_relay.pidfile import remove_pid

        path = tmp_path / "nonexistent.pid"
        # Should not raise
        remove_pid(path)


# ---------------------------------------------------------------------------
# is_process_running
# ---------------------------------------------------------------------------
class TestIsProcessRunning:
    """Test process liveness checks."""

    def test_own_pid_is_running(self):
        from proxy_relay.pidfile import is_process_running

        assert is_process_running(os.getpid()) is True

    def test_nonexistent_pid_is_not_running(self):
        from proxy_relay.pidfile import is_process_running

        # PID 4194304 is above typical max PID range on Linux
        assert is_process_running(4194304) is False


# ---------------------------------------------------------------------------
# send_signal
# ---------------------------------------------------------------------------
class TestSendSignal:
    """Test signal sending."""

    def test_send_signal_to_nonexistent_pid_returns_false(self):
        import signal

        from proxy_relay.pidfile import send_signal

        assert send_signal(4194304, signal.SIGUSR1) is False


# ---------------------------------------------------------------------------
# write_status / read_status
# ---------------------------------------------------------------------------
class TestStatusFile:
    """Test JSON status file creation and reading."""

    def test_write_status_creates_valid_json(self, tmp_path):
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1",
            port=8080,
            upstream_url="socks5://***@proxy:1080",
            country="us",
            active_connections=3,
            total_connections=42,
            path=path,
        )

        data = json.loads(path.read_text())
        assert data["host"] == "127.0.0.1"
        assert data["port"] == 8080
        assert data["upstream_url"] == "socks5://***@proxy:1080"
        assert data["country"] == "us"
        assert data["active_connections"] == 3
        assert data["total_connections"] == 42

    def test_write_status_with_monitor_stats(self, tmp_path):
        from proxy_relay.pidfile import write_status

        mock_stats = {
            "total_connections": 100,
            "total_errors": 5,
            "avg_latency_ms": 150.0,
        }
        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1",
            port=8080,
            upstream_url="socks5://proxy:1080",
            country="co",
            active_connections=0,
            total_connections=100,
            stats=mock_stats,
            path=path,
        )

        data = json.loads(path.read_text())
        assert "stats" in data or "monitor" in data

    def test_read_status_returns_dict(self, tmp_path):
        from proxy_relay.pidfile import read_status, write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1",
            port=8080,
            upstream_url="socks5://proxy:1080",
            country="us",
            active_connections=0,
            total_connections=0,
            path=path,
        )

        result = read_status(path)
        assert result is not None
        assert result["host"] == "127.0.0.1"

    def test_read_status_returns_none_for_missing_file(self, tmp_path):
        from proxy_relay.pidfile import read_status

        path = tmp_path / "nonexistent.json"
        assert read_status(path) is None


# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
class TestPidfileConstants:
    """Verify module exposes expected path constants."""

    def test_pid_path_constant_exists(self):
        from proxy_relay.pidfile import PID_PATH

        assert PID_PATH is not None

    def test_status_path_constant_exists(self):
        from proxy_relay.pidfile import STATUS_PATH

        assert STATUS_PATH is not None
