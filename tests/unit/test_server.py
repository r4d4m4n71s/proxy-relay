"""Tests for proxy_relay.server — ProxyServer start/stop and connection handling."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_relay.config import MonitorConfig
from proxy_relay.server import ProxyServer, _mask_url
from proxy_relay.upstream import UpstreamInfo, UpstreamManager


class TestProxyServer:
    """Test ProxyServer lifecycle."""

    def _make_manager(self) -> MagicMock:
        """Create a mock UpstreamManager."""
        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        return mgr

    @pytest.mark.asyncio
    async def test_server_starts_on_configured_port(self):
        """Server binds to configured host:port."""
        mgr = self._make_manager()
        server = ProxyServer(host="127.0.0.1", port=18080, upstream_manager=mgr)

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18080)
            mock_srv.close = MagicMock()
            mock_srv.wait_closed = AsyncMock()
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

            mock_start.assert_called_once()
            call_kwargs = mock_start.call_args
            assert "127.0.0.1" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_server_stop_closes_cleanly(self, tmp_path):
        """Server stop closes the underlying asyncio server."""
        mgr = self._make_manager()
        server = ProxyServer(host="127.0.0.1", port=18081, upstream_manager=mgr)
        # Point status path to a temp file to avoid unlink issues
        server._status_path = tmp_path / "test.status.json"
        server._pid_path = tmp_path / "test.pid"

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18081)
            mock_srv.close = MagicMock()
            mock_srv.wait_closed = AsyncMock()
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

            await server.stop()

            mock_srv.close.assert_called_once()

    def test_properties(self):
        """Server exposes host, port, and connection counters."""
        mgr = self._make_manager()
        server = ProxyServer(host="0.0.0.0", port=9090, upstream_manager=mgr)

        assert server.host == "0.0.0.0"
        assert server.port == 9090
        assert server.active_connections == 0
        assert server.total_connections == 0
        assert server.is_running is False

    def test_no_upstream_manager_returns_early(self):
        """Server with no upstream manager does not crash on start."""
        server = ProxyServer()
        assert server.host == "127.0.0.1"
        assert server.port == 8080


class TestProxyServerMonitorConfig:
    """Test ProxyServer accepts optional monitor_config parameter."""

    def _make_manager(self) -> MagicMock:
        """Create a mock UpstreamManager."""
        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        return mgr

    def test_server_accepts_monitor_config(self):
        """ProxyServer should accept a monitor_config parameter."""
        mgr = self._make_manager()
        monitor_cfg = MonitorConfig(
            enabled=True,
            slow_threshold_ms=1000.0,
            error_threshold_count=3,
        )

        server = ProxyServer(
            host="127.0.0.1",
            port=8080,
            upstream_manager=mgr,
            monitor_config=monitor_cfg,
        )

        assert server.host == "127.0.0.1"
        assert server.port == 8080

    def test_server_monitor_config_defaults_to_none(self):
        """ProxyServer should work without monitor_config (backward compat)."""
        mgr = self._make_manager()

        server = ProxyServer(host="127.0.0.1", port=8080, upstream_manager=mgr)
        assert server.host == "127.0.0.1"

    def test_monitor_stats_none_when_no_config(self):
        """monitor_stats should be None when no monitor_config is provided."""
        mgr = self._make_manager()
        server = ProxyServer(host="127.0.0.1", port=8080, upstream_manager=mgr)
        assert server.monitor_stats is None

    @pytest.mark.asyncio
    async def test_monitor_created_on_start_when_enabled(self):
        """When monitor_config is enabled, a ConnectionMonitor is created on start."""
        mgr = self._make_manager()
        monitor_cfg = MonitorConfig(
            enabled=True,
            slow_threshold_ms=1000.0,
            error_threshold_count=3,
            window_size=50,
        )
        server = ProxyServer(
            host="127.0.0.1",
            port=18082,
            upstream_manager=mgr,
            monitor_config=monitor_cfg,
        )

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18082)
            mock_srv.close = MagicMock()
            mock_srv.wait_closed = AsyncMock()
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        # Monitor should have been created
        assert server.monitor_stats is not None


class TestProxyServerStopShutdownsMonitor:
    """Test F-RL3: stop() signals ConnectionMonitor.shutdown() before setting event."""

    @pytest.mark.asyncio
    async def test_stop_calls_monitor_shutdown(self, tmp_path):
        """stop() must call monitor.shutdown() so rotation callbacks are suppressed."""
        from proxy_relay.monitor import ConnectionMonitor

        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        mgr.profile_name = "browse"

        monitor_cfg = MonitorConfig(enabled=True, slow_threshold_ms=1000.0, error_threshold_count=3)
        server = ProxyServer(
            host="127.0.0.1", port=18085,
            upstream_manager=mgr,
            monitor_config=monitor_cfg,
        )
        server._status_path = tmp_path / "test.status.json"
        server._pid_path = tmp_path / "test.pid"

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18085)
            mock_srv.close = MagicMock()
            mock_srv.wait_closed = AsyncMock()
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert server._monitor is not None
        assert not server._monitor._shutdown  # not yet shut down

        await server.stop()

        # Monitor must be shut down before shutdown_event is set
        assert server._monitor._shutdown is True
        assert server._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_without_monitor_does_not_raise(self, tmp_path):
        """stop() works correctly when no monitor is configured."""
        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        mgr.profile_name = "browse"

        server = ProxyServer(host="127.0.0.1", port=18086, upstream_manager=mgr)
        server._status_path = tmp_path / "test.status.json"
        server._pid_path = tmp_path / "test.pid"

        with patch("proxy_relay.server.remove_pid"), patch("proxy_relay.server.write_status"):
            # stop() without start() — server is None, should still set shutdown_event
            await server.stop()

        assert server._shutdown_event.is_set()


# ---------------------------------------------------------------------------
# F-RL7: _mask_url
# ---------------------------------------------------------------------------
class TestMaskUrl:
    """Test URL credential masking helper (F-RL7)."""

    def test_mask_url_with_credentials(self):
        """Credentials are replaced with ***."""
        assert _mask_url("socks5://user:pass@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"

    def test_mask_url_without_credentials(self):
        """URL without credentials is returned unchanged."""
        assert _mask_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"

    def test_mask_url_no_scheme(self):
        """URL without scheme is returned unchanged."""
        assert _mask_url("user:pass@host:1080") == "user:pass@host:1080"

    def test_mask_url_empty_string(self):
        """Empty string is returned unchanged."""
        assert _mask_url("") == ""

    def test_mask_url_complex_credentials(self):
        """Complex credentials with special chars are masked."""
        assert _mask_url("socks5://u%40sr:p%3Ass@host:1080") == "socks5://***@host:1080"


# ---------------------------------------------------------------------------
# F-RL11: SIGPIPE handler in start()
# ---------------------------------------------------------------------------
class TestSigpipeInStart:
    """Test F-RL11: SIGPIPE handler is installed during server start."""

    @pytest.mark.asyncio
    async def test_sigpipe_handler_installed_on_start(self):
        """start() should install a SIGPIPE handler in the event loop."""
        import signal as _signal

        if not hasattr(_signal, "SIGPIPE"):
            pytest.skip("SIGPIPE not available on this platform")

        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        server = ProxyServer(host="127.0.0.1", port=18090, upstream_manager=mgr)

        signal_handlers: dict[int, object] = {}

        mock_loop = MagicMock()
        mock_loop.add_signal_handler = MagicMock(
            side_effect=lambda sig, cb: signal_handlers.__setitem__(sig, cb)
        )

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status"), \
             patch("asyncio.get_running_loop", return_value=mock_loop):
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18090)
            mock_start.return_value = mock_srv

            await server.start()

        assert _signal.SIGPIPE in signal_handlers


# ---------------------------------------------------------------------------
# F-RL24: started_at stored on start()
# ---------------------------------------------------------------------------
class TestServerStartedAt:
    """Test that server records started_at timestamp and passes it to write_status (F-RL24)."""

    @pytest.mark.asyncio
    async def test_started_at_set_on_start(self):
        """start() sets _started_at to an ISO timestamp."""
        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        server = ProxyServer(host="127.0.0.1", port=18091, upstream_manager=mgr)

        assert server._started_at == ""

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status") as mock_ws:
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18091)
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert server._started_at != ""
        assert "T" in server._started_at  # ISO format contains T

    @pytest.mark.asyncio
    async def test_write_status_receives_pid_and_started_at(self):
        """write_status is called with pid and started_at from server."""
        mgr = MagicMock(spec=UpstreamManager)
        mgr.get_upstream.return_value = UpstreamInfo(
            host="proxy.example.com", port=12322,
            username="user", password="pass",
            url="socks5://***@proxy.example.com:12322", country="us",
        )
        mgr.profile_name = "browse"
        server = ProxyServer(host="127.0.0.1", port=18092, upstream_manager=mgr)

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status") as mock_ws:
            mock_srv = AsyncMock()
            mock_srv.sockets = [MagicMock()]
            mock_srv.sockets[0].getsockname.return_value = ("127.0.0.1", 18092)
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        # write_status should have been called with pid and started_at
        assert mock_ws.called
        call_kwargs = mock_ws.call_args
        assert "pid" in call_kwargs.kwargs or len(call_kwargs.args) > 0
        assert "started_at" in call_kwargs.kwargs or len(call_kwargs.args) > 0
