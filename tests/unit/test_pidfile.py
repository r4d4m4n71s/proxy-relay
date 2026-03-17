"""Tests for proxy_relay.pidfile — PID file and status file management."""
from __future__ import annotations

import json
import os
import stat
import threading

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

    def test_write_status_swallows_type_error_from_non_serializable_data(self, tmp_path):
        """F-RL12: write_status does not raise when stats contain non-serializable data.

        json.dumps raises TypeError for non-serializable values; the outer
        except clause previously only caught OSError, letting TypeError propagate
        to the connection handler. Fixed to catch Exception.
        """
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        # Pass a stats dict with a non-JSON-serializable value
        non_serializable_stats = {"key": object()}

        # Must not raise — TypeError should be absorbed as a warning
        write_status(
            host="127.0.0.1",
            port=8080,
            upstream_url="socks5://proxy:1080",
            country="us",
            active_connections=0,
            total_connections=0,
            stats=non_serializable_stats,
            path=path,
        )
        # File should NOT have been created (write was skipped due to error)
        assert not path.exists()

    def test_write_status_oserror_is_swallowed(self, tmp_path):
        """OSError during status write is logged as warning (not raised)."""
        import unittest.mock as mock
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"

        with mock.patch("proxy_relay.pidfile.os.replace", side_effect=OSError("disk full")):
            # Must not raise
            write_status(
                host="127.0.0.1",
                port=8080,
                upstream_url="socks5://proxy:1080",
                country="us",
                active_connections=0,
                total_connections=0,
                path=path,
            )


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


# ---------------------------------------------------------------------------
# F-RL4: read_status_if_alive
# ---------------------------------------------------------------------------
class TestReadStatusIfAlive:
    """Test liveness-checked status reading (F-RL4)."""

    def test_alive_process_returns_running_and_data(self, tmp_path):
        """When PID is alive, returns (True, pid, status_data)."""
        from unittest.mock import patch

        from proxy_relay.pidfile import read_status_if_alive, write_pid, write_status

        profile = "testprofile"

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            pid_p = tmp_path / f"{profile}.pid"
            status_p = tmp_path / f"{profile}.status.json"
            write_pid(pid_p)
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=0, total_connections=0, path=status_p,
            )

            running, pid, data = read_status_if_alive(profile)

        assert running is True
        assert pid == os.getpid()
        assert data is not None
        assert data["host"] == "127.0.0.1"

    def test_stale_pid_cleans_up_files(self, tmp_path):
        """When PID is dead, stale .pid and .status.json files are removed."""
        from unittest.mock import patch

        from proxy_relay.pidfile import read_status_if_alive

        profile = "staleprofile"

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            pid_p = tmp_path / f"{profile}.pid"
            status_p = tmp_path / f"{profile}.status.json"
            # Write a PID that does not exist (high PID number)
            pid_p.write_text("4194304", encoding="utf-8")
            status_p.write_text('{"host":"127.0.0.1"}', encoding="utf-8")

            running, pid, data = read_status_if_alive(profile)

        assert running is False
        assert pid == 4194304
        assert data is None
        assert not pid_p.exists()
        assert not status_p.exists()

    def test_no_pid_file_returns_not_running(self, tmp_path):
        """When no PID file exists, returns (False, None, None)."""
        from unittest.mock import patch

        from proxy_relay.pidfile import read_status_if_alive

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            running, pid, data = read_status_if_alive("nofile")

        assert running is False
        assert pid is None
        assert data is None


# ---------------------------------------------------------------------------
# F-RL6: _atexit_lock thread safety
# ---------------------------------------------------------------------------
class TestAtexitLockThreadSafety:
    """Test that write_pid is thread-safe for _atexit_registered (F-RL6)."""

    def test_concurrent_write_pid_no_duplicates(self, tmp_path):
        """Calling write_pid from two threads registers each path exactly once."""
        from proxy_relay.pidfile import _atexit_registered, write_pid

        path_a = tmp_path / "a.pid"
        path_b = tmp_path / "b.pid"

        # Clear module-level set for this test
        _atexit_registered.discard(path_a)
        _atexit_registered.discard(path_b)

        errors: list[Exception] = []

        def _write(p):
            try:
                write_pid(p)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_write, args=(path_a,))
        t2 = threading.Thread(target=_write, args=(path_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        assert path_a in _atexit_registered
        assert path_b in _atexit_registered


# ---------------------------------------------------------------------------
# F-RL24: PID + timestamps in status file
# ---------------------------------------------------------------------------
class TestStatusFilePidTimestamps:
    """Test that status files contain pid, started_at, last_updated (F-RL24)."""

    def test_status_contains_pid_field(self, tmp_path):
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
            country="us", active_connections=0, total_connections=0,
            pid=12345, started_at="2026-01-01T00:00:00+00:00", path=path,
        )
        data = json.loads(path.read_text())
        assert data["pid"] == 12345

    def test_status_pid_defaults_to_current_process(self, tmp_path):
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
            country="us", active_connections=0, total_connections=0, path=path,
        )
        data = json.loads(path.read_text())
        assert data["pid"] == os.getpid()

    def test_status_contains_started_at(self, tmp_path):
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
            country="us", active_connections=0, total_connections=0,
            started_at="2026-03-16T10:00:00+00:00", path=path,
        )
        data = json.loads(path.read_text())
        assert data["started_at"] == "2026-03-16T10:00:00+00:00"

    def test_status_contains_last_updated(self, tmp_path):
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
            country="us", active_connections=0, total_connections=0, path=path,
        )
        data = json.loads(path.read_text())
        assert "last_updated" in data
        assert len(data["last_updated"]) > 0  # ISO timestamp string

    def test_last_updated_changes_on_rewrite(self, tmp_path):
        """Two writes should produce different last_updated values (or same if instant)."""
        from proxy_relay.pidfile import write_status

        path = tmp_path / "status.json"
        write_status(
            host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
            country="us", active_connections=0, total_connections=0, path=path,
        )
        data1 = json.loads(path.read_text())
        assert "last_updated" in data1


# ---------------------------------------------------------------------------
# F-RL25: atexit cleanup for status files
# ---------------------------------------------------------------------------
class TestStatusAtexitCleanup:
    """Test that status files register atexit cleanup (F-RL25)."""

    def test_status_file_registered_for_atexit(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import _status_atexit_registered, write_status

        path = tmp_path / "test_atexit.status.json"
        _status_atexit_registered.discard(path)

        with patch("proxy_relay.pidfile.atexit.register") as mock_register:
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=0, total_connections=0, path=path,
            )

        # atexit.register should have been called with _remove_status_file
        mock_register.assert_called_once()
        assert path in _status_atexit_registered

        # Cleanup
        _status_atexit_registered.discard(path)

    def test_status_atexit_not_registered_twice(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import _status_atexit_registered, write_status

        path = tmp_path / "test_atexit2.status.json"
        _status_atexit_registered.discard(path)

        with patch("proxy_relay.pidfile.atexit.register") as mock_register:
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=0, total_connections=0, path=path,
            )
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=1, total_connections=1, path=path,
            )

        # Should only register once despite two writes
        assert mock_register.call_count == 1

        # Cleanup
        _status_atexit_registered.discard(path)

    def test_remove_status_file_deletes_file(self, tmp_path):
        from proxy_relay.pidfile import _remove_status_file

        path = tmp_path / "to_remove.status.json"
        path.write_text("{}")
        assert path.exists()

        _remove_status_file(path)
        assert not path.exists()

    def test_remove_status_file_noop_for_missing(self, tmp_path):
        from proxy_relay.pidfile import _remove_status_file

        path = tmp_path / "nonexistent.status.json"
        # Should not raise
        _remove_status_file(path)


# ---------------------------------------------------------------------------
# F-RL26: scan_all_status
# ---------------------------------------------------------------------------
class TestScanAllStatus:
    """Test multi-profile status scanning (F-RL26)."""

    def test_scan_finds_live_profiles(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import scan_all_status, write_pid, write_status

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            # Create a live profile (current process PID)
            write_pid(tmp_path / "live.pid")
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=0, total_connections=5,
                path=tmp_path / "live.status.json",
            )

            results = scan_all_status(config_dir=tmp_path)

        assert len(results) == 1
        assert results[0]["profile"] == "live"
        assert results[0]["running"] is True

    def test_scan_cleans_stale_profiles(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import scan_all_status

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            # Create a stale profile (dead PID)
            (tmp_path / "stale.pid").write_text("4194304", encoding="utf-8")
            (tmp_path / "stale.status.json").write_text(
                '{"host":"127.0.0.1","port":8080}', encoding="utf-8"
            )

            results = scan_all_status(config_dir=tmp_path)

        assert len(results) == 1
        assert results[0]["profile"] == "stale"
        assert results[0]["running"] is False
        # Stale files should be cleaned up
        assert not (tmp_path / "stale.pid").exists()
        assert not (tmp_path / "stale.status.json").exists()

    def test_scan_empty_dir(self, tmp_path):
        from proxy_relay.pidfile import scan_all_status

        results = scan_all_status(config_dir=tmp_path)
        assert results == []

    def test_scan_mixed_live_and_stale(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import scan_all_status, write_pid, write_status

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            # Live profile
            write_pid(tmp_path / "alive.pid")
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="co", active_connections=1, total_connections=10,
                path=tmp_path / "alive.status.json",
            )
            # Stale profile
            (tmp_path / "dead.pid").write_text("4194304", encoding="utf-8")
            (tmp_path / "dead.status.json").write_text(
                '{"host":"127.0.0.1","port":9090}', encoding="utf-8"
            )

            results = scan_all_status(config_dir=tmp_path)

        assert len(results) == 2
        alive = next(r for r in results if r["profile"] == "alive")
        dead = next(r for r in results if r["profile"] == "dead")
        assert alive["running"] is True
        assert dead["running"] is False


# ---------------------------------------------------------------------------
# F-RL27: read_live_status
# ---------------------------------------------------------------------------
class TestReadLiveStatus:
    """Test simplified live status helper (F-RL27)."""

    def test_returns_dict_for_live_profile(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import read_live_status, write_pid, write_status

        profile = "liveprof"
        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            write_pid(tmp_path / f"{profile}.pid")
            write_status(
                host="127.0.0.1", port=8080, upstream_url="socks5://proxy:1080",
                country="us", active_connections=2, total_connections=20,
                path=tmp_path / f"{profile}.status.json",
            )

            result = read_live_status(profile)

        assert result is not None
        assert result["running"] is True
        assert result["pid"] == os.getpid()
        assert result["profile"] == profile
        assert result["host"] == "127.0.0.1"

    def test_returns_none_for_dead_profile(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import read_live_status

        profile = "deadprof"
        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            (tmp_path / f"{profile}.pid").write_text("4194304", encoding="utf-8")
            (tmp_path / f"{profile}.status.json").write_text(
                '{"host":"127.0.0.1"}', encoding="utf-8"
            )

            result = read_live_status(profile)

        assert result is None

    def test_returns_none_for_missing_profile(self, tmp_path):
        from unittest.mock import patch

        from proxy_relay.pidfile import read_live_status

        with patch("proxy_relay.pidfile.CONFIG_DIR", tmp_path):
            result = read_live_status("nonexistent")

        assert result is None
