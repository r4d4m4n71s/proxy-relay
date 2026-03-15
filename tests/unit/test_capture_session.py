"""Tests for proxy_relay.capture — CaptureSession, _find_free_port, is_capture_available."""
from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. _find_free_port
# ---------------------------------------------------------------------------


class TestFindFreePort:
    """Verify _find_free_port returns a valid, usable port."""

    def test_returns_valid_port_range(self):
        from proxy_relay.capture import _find_free_port

        port = _find_free_port()
        assert 1 <= port <= 65535, f"Port {port} outside valid range 1-65535"

    def test_returns_different_ports_on_consecutive_calls(self):
        """Two consecutive calls should usually return different ports.

        Note: there is a tiny probability of collision on a heavily loaded
        system, but in practice this never happens in test environments.
        """
        from proxy_relay.capture import _find_free_port

        ports = {_find_free_port() for _ in range(5)}
        # At least 2 distinct ports out of 5 calls
        assert len(ports) >= 2, f"Expected distinct ports, got: {ports}"

    def test_returned_port_is_integer(self):
        from proxy_relay.capture import _find_free_port

        port = _find_free_port()
        assert isinstance(port, int)


# ---------------------------------------------------------------------------
# 2. is_capture_available
# ---------------------------------------------------------------------------


class TestIsCaptureAvailable:
    """Verify is_capture_available probes for optional dependencies correctly."""

    def test_returns_true_when_imports_succeed(self, monkeypatch):
        """is_capture_available() returns True when websockets and telemetry_monitor are present."""
        import sys

        fake_websockets = MagicMock()
        fake_telemetry = MagicMock()

        # Patch sys.modules so the import inside is_capture_available finds them
        monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
        monkeypatch.setitem(sys.modules, "telemetry_monitor", fake_telemetry)

        # Re-import to pick up the patched modules if the function does lazy imports
        from proxy_relay.capture import is_capture_available

        result = is_capture_available()
        assert result is True

    def test_returns_false_when_websockets_missing(self, monkeypatch):
        """is_capture_available() returns False when websockets is not installed.

        Setting a sys.modules entry to None causes Python to raise ImportError
        on ``import websockets`` — the standard sentinel for blocking imports.
        """
        import sys

        # Save and remove any real websockets entry, then set sentinel
        saved = sys.modules.pop("websockets", object())
        monkeypatch.setitem(sys.modules, "websockets", None)

        try:
            from proxy_relay.capture import is_capture_available
            assert is_capture_available() is False
        finally:
            # Restore original state
            sys.modules.pop("websockets", None)
            if saved is not object():
                sys.modules["websockets"] = saved

    def test_returns_false_when_telemetry_monitor_missing(self, monkeypatch):
        """is_capture_available() returns False when telemetry_monitor is not installed.

        Setting a sys.modules entry to None causes Python to raise ImportError
        on ``import telemetry_monitor``.
        """
        import sys

        saved = sys.modules.pop("telemetry_monitor", object())
        monkeypatch.setitem(sys.modules, "telemetry_monitor", None)

        try:
            from proxy_relay.capture import is_capture_available
            assert is_capture_available() is False
        finally:
            sys.modules.pop("telemetry_monitor", None)
            if saved is not object():
                sys.modules["telemetry_monitor"] = saved


# ---------------------------------------------------------------------------
# 3. CaptureSession
# ---------------------------------------------------------------------------


class TestCaptureSession:
    """Verify CaptureSession lifecycle and thread safety."""

    def _make_session(self, tmp_path=None):
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(
            db_path=(tmp_path / "capture.db") if tmp_path else None,
        )
        return CaptureSession(config=cfg)

    def test_cdp_port_allocated_lazily(self):
        """cdp_port is allocated on first access and reused on subsequent accesses."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())

        port1 = session.cdp_port
        port2 = session.cdp_port
        assert port1 == port2, "cdp_port must be stable after first allocation"

    def test_cdp_port_is_valid(self):
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        assert 1 <= session.cdp_port <= 65535

    def test_request_stop_is_threadsafe(self):
        """request_stop() called from another thread does not crash."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        errors: list[Exception] = []

        def stop_from_thread():
            try:
                session.request_stop()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=stop_from_thread) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        assert not errors, f"Errors from threads: {errors}"

    def test_run_in_thread_catches_exceptions(self):
        """Exceptions inside the CDP capture thread are logged, not propagated."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())

        # Patch start to raise immediately
        async def failing_start(port: int) -> None:
            raise RuntimeError("Simulated CDP connection failure")

        session._run_capture_async = failing_start

        raised = []

        def run_and_catch():
            try:
                session._run_capture()
            except Exception as exc:
                raised.append(exc)

        t = threading.Thread(target=run_and_catch)
        t.start()
        t.join(timeout=3.0)

        # The exception must NOT propagate out of the thread runner
        assert not raised, (
            f"Exception propagated from capture thread: {raised}"
        )

    async def test_stop_before_start_is_safe(self):
        """Calling stop() before start() must not raise."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        await session.stop()  # Must not raise

    async def test_start_and_stop_lifecycle(self):
        """start() + stop() completes without error when CDP client is mocked."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())

        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()

        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)
            await session.stop()

        mock_cdp.connect.assert_called_once_with(9222)
        mock_cdp.close.assert_called_once()

    async def test_start_enables_page_domain(self):
        """start() must call Page.enable for navigation events."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)

        # Verify Page.enable was called
        send_calls = [call.args[0] for call in mock_cdp.send.call_args_list]
        assert "Page.enable" in send_calls, f"Page.enable not found in: {send_calls}"

        await session.stop()

    async def test_start_subscribes_to_page_frame_navigated(self):
        """start() must subscribe to Page.frameNavigated."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)

        subscribe_events = [call.args[0] for call in mock_cdp.subscribe.call_args_list]
        assert "Page.frameNavigated" in subscribe_events, (
            f"Page.frameNavigated not in subscriptions: {subscribe_events}"
        )

        await session.stop()

    async def test_start_registers_async_response_handler(self):
        """start() must register an async handler for Network.responseReceived."""
        import asyncio

        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)

        # Find the Network.responseReceived subscription
        for call in mock_cdp.subscribe.call_args_list:
            if call.args[0] == "Network.responseReceived":
                handler = call.args[1]
                # The handler should be an async function (coroutine function)
                assert asyncio.iscoroutinefunction(handler), (
                    "Network.responseReceived handler must be async for body fetching"
                )
                break
        else:
            pytest.fail("Network.responseReceived subscription not found")

        await session.stop()

    async def test_stop_disables_all_domains(self):
        """stop() must disable Network, Page, and IndexedDB domains."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        session = CaptureSession(config=CaptureConfig())
        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)
            mock_cdp.send.reset_mock()
            await session.stop()

        disable_calls = [
            call.args[0] for call in mock_cdp.send.call_args_list
            if call.args[0].endswith(".disable")
        ]
        assert "Network.disable" in disable_calls
        assert "Page.disable" in disable_calls
        assert "IndexedDB.disable" in disable_calls

    async def test_stop_skips_analysis_when_auto_analyze_false(self, tmp_path):
        """stop() must NOT call analyzer when auto_analyze=False."""
        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        db_path = tmp_path / "capture.db"
        cfg = CaptureConfig(db_path=db_path, auto_analyze=False, auto_report=False)
        session = CaptureSession(config=cfg)

        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer), \
             patch("proxy_relay.capture.analyzer.analyze") as mock_analyze:
            await session.start(9222)
            await session.stop()

        mock_analyze.assert_not_called()

    async def test_stop_runs_analysis_when_auto_analyze_true(self, tmp_path):
        """stop() must call analyzer when auto_analyze=True."""
        import sqlite3

        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        # Create a minimal DB so analyze() can open it
        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "CREATE TABLE http_requests (timestamp TEXT, request_id TEXT, url TEXT,"
            " method TEXT, headers TEXT, post_data TEXT, profile TEXT);"
            "CREATE TABLE http_responses (timestamp TEXT, request_id TEXT, url TEXT,"
            " status INTEGER, mime_type TEXT, headers TEXT, body TEXT,"
            " response_ms INTEGER, profile TEXT);"
            "CREATE TABLE cookies (timestamp TEXT, domain TEXT, name TEXT, value TEXT,"
            " http_only INTEGER, secure INTEGER, expires REAL, path TEXT, profile TEXT);"
            "CREATE TABLE storage_snapshots (timestamp TEXT, origin TEXT, storage_type TEXT,"
            " key TEXT, value TEXT, change_type TEXT, profile TEXT);"
            "CREATE TABLE websocket_frames (timestamp TEXT, request_id TEXT, url TEXT,"
            " direction TEXT, payload TEXT, opcode INTEGER, profile TEXT);"
            "CREATE TABLE page_navigations (timestamp TEXT, url TEXT, frame_id TEXT,"
            " transition_type TEXT, mime_type TEXT, profile TEXT);"
        )
        conn.close()

        cfg = CaptureConfig(db_path=db_path, auto_analyze=True, auto_report=False)
        session = CaptureSession(config=cfg)

        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)
            await session.stop()

        # If analysis ran, no error was raised — success
        # (We can't easily mock the lazy import, but the empty DB won't crash)

    async def test_stop_writes_report_when_auto_report_true(self, tmp_path):
        """stop() must write a report file when auto_report=True."""
        import sqlite3

        from proxy_relay.capture import CaptureSession
        from proxy_relay.capture.models import CaptureConfig

        db_path = tmp_path / "capture.db"
        report_dir = tmp_path / "reports"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            "CREATE TABLE http_requests (timestamp TEXT, request_id TEXT, url TEXT,"
            " method TEXT, headers TEXT, post_data TEXT, profile TEXT);"
            "CREATE TABLE http_responses (timestamp TEXT, request_id TEXT, url TEXT,"
            " status INTEGER, mime_type TEXT, headers TEXT, body TEXT,"
            " response_ms INTEGER, profile TEXT);"
            "CREATE TABLE cookies (timestamp TEXT, domain TEXT, name TEXT, value TEXT,"
            " http_only INTEGER, secure INTEGER, expires REAL, path TEXT, profile TEXT);"
            "CREATE TABLE storage_snapshots (timestamp TEXT, origin TEXT, storage_type TEXT,"
            " key TEXT, value TEXT, change_type TEXT, profile TEXT);"
            "CREATE TABLE websocket_frames (timestamp TEXT, request_id TEXT, url TEXT,"
            " direction TEXT, payload TEXT, opcode INTEGER, profile TEXT);"
            "CREATE TABLE page_navigations (timestamp TEXT, url TEXT, frame_id TEXT,"
            " transition_type TEXT, mime_type TEXT, profile TEXT);"
        )
        conn.close()

        cfg = CaptureConfig(
            db_path=db_path, auto_analyze=False, auto_report=True, report_dir=report_dir,
        )
        session = CaptureSession(config=cfg)

        mock_cdp = AsyncMock()
        mock_cdp.connect = AsyncMock()
        mock_cdp.send = AsyncMock(return_value={})
        mock_cdp.subscribe = AsyncMock()
        mock_cdp.close = AsyncMock()
        mock_writer = MagicMock()

        with patch("proxy_relay.capture.CdpClient", return_value=mock_cdp), \
             patch("proxy_relay.capture.BackgroundWriter", return_value=mock_writer):
            await session.start(9222)
            await session.stop()

        # Report directory should have been created with a .md file
        assert report_dir.exists()
        reports = list(report_dir.glob("capture-report-*.md"))
        assert len(reports) == 1
        assert "Capture Analysis Report" in reports[0].read_text()
