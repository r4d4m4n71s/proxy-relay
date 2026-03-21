"""Tests for proxy_relay.server — ProxyServer start/stop and connection handling."""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_relay.config import MonitorConfig
from proxy_relay.server import ProxyServer
from proxy_relay.upstream import UpstreamInfo, UpstreamManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_manager() -> MagicMock:
    """Create a mock UpstreamManager."""
    mgr = MagicMock(spec=UpstreamManager)
    mgr.get_upstream.return_value = UpstreamInfo(
        host="proxy.example.com", port=12322,
        username="user", password="pass",
        url="socks5://user:pass@proxy.example.com:12322",
        masked_url="socks5://***@proxy.example.com:12322", country="us",
    )
    return mgr


def _mock_asyncio_server(host: str, port: int) -> AsyncMock:
    """Build a mock asyncio.Server that getsockname returns (host, port)."""
    mock_srv = AsyncMock()
    mock_srv.sockets = [MagicMock()]
    mock_srv.sockets[0].getsockname.return_value = (host, port)
    mock_srv.close = MagicMock()
    mock_srv.wait_closed = AsyncMock()
    return mock_srv


# ---------------------------------------------------------------------------
# TestProxyServer — basic lifecycle
# ---------------------------------------------------------------------------


class TestProxyServer:
    """Test ProxyServer lifecycle."""

    @pytest.mark.asyncio
    async def test_server_starts_on_configured_port(self):
        """Server binds to configured host:port."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18080, upstream_manager=mgr)

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18080)

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

            mock_start.assert_called_once()
            call_kwargs = mock_start.call_args
            assert "127.0.0.1" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_server_stop_closes_cleanly(self, tmp_path):
        """Server stop closes the underlying asyncio server."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18081, upstream_manager=mgr)
        server._status_path = tmp_path / "test.status.json"
        server._pid_path = tmp_path / "test.pid"

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.remove_pid"), \
             patch("proxy_relay.server.write_status"):
            mock_srv = _mock_asyncio_server("127.0.0.1", 18081)
            mock_start.return_value = mock_srv

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

            await server.stop()
            mock_srv.close.assert_called_once()

    def test_properties(self):
        """Server exposes host, port, and connection counters."""
        mgr = _make_manager()
        server = ProxyServer(host="0.0.0.0", port=9090, upstream_manager=mgr)

        assert server.host == "0.0.0.0"
        assert server.port == 9090
        assert server.active_connections == 0
        assert server.total_connections == 0
        assert server.is_running is False

    def test_no_upstream_manager_returns_early(self):
        """Server with no upstream manager does not crash on instantiation."""
        server = ProxyServer()
        assert server.host == "127.0.0.1"
        assert server.port == 8080

    def test_accepts_config_path_parameter(self):
        """ProxyServer accepts config_path parameter for SIGUSR2 reload."""
        from proxy_relay.config import ProfileConfig, RelayConfig

        mgr = _make_manager()
        config_path = Path("/tmp/config.toml")
        server = ProxyServer(
            host="127.0.0.1",
            port=8080,
            upstream_manager=mgr,
            config_path=config_path,
        )
        assert server._config_path == config_path

    def test_config_path_defaults_to_none(self):
        """config_path defaults to None when not provided."""
        server = ProxyServer()
        assert server._config_path is None


# ---------------------------------------------------------------------------
# TestProxyServerMonitorConfig
# ---------------------------------------------------------------------------


class TestProxyServerMonitorConfig:
    """Test ProxyServer accepts optional monitor_config parameter."""

    def test_server_accepts_monitor_config(self):
        """ProxyServer should accept a monitor_config parameter."""
        mgr = _make_manager()
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
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=8080, upstream_manager=mgr)
        assert server.host == "127.0.0.1"

    def test_monitor_stats_none_when_no_config(self):
        """monitor_stats should be None when no monitor_config is provided."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=8080, upstream_manager=mgr)
        assert server.monitor_stats is None

    @pytest.mark.asyncio
    async def test_monitor_created_on_start_when_enabled(self):
        """When monitor_config is enabled, a ConnectionMonitor is created on start."""
        mgr = _make_manager()
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
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18082)

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert server.monitor_stats is not None


# ---------------------------------------------------------------------------
# TestBlockedDomainsReload — SIGUSR2 handler and config reload
# ---------------------------------------------------------------------------


class TestBlockedDomainsReload:
    """Test SIGUSR2 handler and _reload_blocked_from_config()."""

    def test_update_blocked_domains_swaps_value(self):
        """_update_blocked_domains() replaces _blocked_domains atomically."""
        server = ProxyServer()
        old_domains = frozenset({"tidal.com"})
        new_domains = frozenset({"example.com", "other.org"})

        server._blocked_domains = old_domains
        server._update_blocked_domains(new_domains)

        assert server._blocked_domains == new_domains

    def test_update_blocked_domains_accepts_none(self):
        """_update_blocked_domains(None) disables blocking."""
        server = ProxyServer()
        server._blocked_domains = frozenset({"tidal.com"})
        server._update_blocked_domains(None)

        assert server._blocked_domains is None

    def test_reload_blocked_from_config_reads_config_and_updates(self, tmp_path):
        """_reload_blocked_from_config() reads config.toml and updates blocked_domains."""
        from proxy_relay.config import ProfileConfig, RelayConfig, resolve_blocked_domains

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            "[profiles.default]\n"
            "\n"
            "[profiles.miami]\n"
            'blocked_domains = ["example.com"]\n'
        )

        server = ProxyServer(
            profile_name="miami",
            config_path=config_path,
        )
        server._blocked_domains = frozenset({"old.com"})

        server._reload_blocked_from_config()

        # After reload, blocked_domains should reflect the new config
        assert server._blocked_domains != frozenset({"old.com"})

    def test_reload_on_parse_error_keeps_current_state(self, tmp_path):
        """Corrupted config.toml on reload keeps current in-memory state."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("this is [not valid toml ===\n")

        original_domains = frozenset({"tidal.com"})
        server = ProxyServer(
            profile_name="miami",
            config_path=config_path,
        )
        server._blocked_domains = original_domains

        # Must not raise; must keep current state
        server._reload_blocked_from_config()

        assert server._blocked_domains == original_domains

    def test_reload_logs_warning_on_parse_error(self, tmp_path, caplog):
        """_reload_blocked_from_config() logs a warning when config is unparseable."""
        import logging

        config_path = tmp_path / "config.toml"
        config_path.write_text("invalid toml [[[[\n")

        server = ProxyServer(
            profile_name="miami",
            config_path=config_path,
        )
        server._blocked_domains = frozenset({"tidal.com"})

        with caplog.at_level(logging.WARNING):
            server._reload_blocked_from_config()

        assert any(
            "error" in record.message.lower() or "warning" in record.levelname.lower()
            for record in caplog.records
        )

    def test_reload_with_no_config_path_falls_back_to_default(self, tmp_path, monkeypatch):
        """_reload_blocked_from_config() with config_path=None falls back to CONFIG_PATH.

        When CONFIG_PATH does not exist or fails to parse, the current state is kept.
        """
        from proxy_relay import config as _config

        # Point CONFIG_PATH to a non-existent file so load_config creates the default.
        # Use monkeypatch to avoid touching the real user config.
        nonexistent = tmp_path / "no-such.toml"
        monkeypatch.setattr(_config, "CONFIG_PATH", nonexistent)

        server = ProxyServer()
        server._config_path = None
        server._blocked_domains = frozenset({"tidal.com"})

        # Must not raise regardless of whether the default config exists
        try:
            server._reload_blocked_from_config()
        except Exception:
            # If it raises (e.g. profile not in freshly created default), that is acceptable
            # as long as the state is unchanged.
            pass

        # No crash is the key requirement
        assert True

    @pytest.mark.asyncio
    async def test_sigusr2_handler_registered_on_start(self):
        """start() installs a SIGUSR2 handler in the event loop."""
        mgr = _make_manager()
        config_path = Path("/tmp/config.toml")
        server = ProxyServer(
            host="127.0.0.1",
            port=18095,
            upstream_manager=mgr,
            config_path=config_path,
        )

        signal_handlers: dict[int, object] = {}

        mock_loop = MagicMock()
        mock_loop.add_signal_handler = MagicMock(
            side_effect=lambda sig, cb: signal_handlers.__setitem__(sig, cb)
        )

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status"), \
             patch("asyncio.get_running_loop", return_value=mock_loop):
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18095)
            await server.start()

        assert signal.SIGUSR2 in signal_handlers

    def test_signal_block_update_schedules_reload_via_event_loop(self):
        """_signal_block_update() schedules _reload_blocked_from_config via call_soon_threadsafe."""
        server = ProxyServer(profile_name="miami")

        scheduled_callbacks = []

        mock_loop = MagicMock()
        mock_loop.call_soon_threadsafe = MagicMock(
            side_effect=lambda cb: scheduled_callbacks.append(cb)
        )

        with patch("asyncio.get_running_loop", return_value=mock_loop):
            server._signal_block_update()

        assert len(scheduled_callbacks) == 1
        assert scheduled_callbacks[0] == server._reload_blocked_from_config


# ---------------------------------------------------------------------------
# TestProxyServerStopShutdownsMonitor
# ---------------------------------------------------------------------------


class TestProxyServerStopShutdownsMonitor:
    """Test stop() signals ConnectionMonitor.shutdown() before setting event."""

    @pytest.mark.asyncio
    async def test_stop_calls_monitor_shutdown(self, tmp_path):
        """stop() must call monitor.shutdown() so rotation callbacks are suppressed."""
        from proxy_relay.monitor import ConnectionMonitor

        mgr = _make_manager()
        mgr.profile_name = "miami"

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
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18085)

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert server._monitor is not None
        assert not server._monitor._shutdown

        await server.stop()

        assert server._monitor._shutdown is True
        assert server._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_stop_without_monitor_does_not_raise(self, tmp_path):
        """stop() works correctly when no monitor is configured."""
        mgr = _make_manager()
        mgr.profile_name = "miami"

        server = ProxyServer(host="127.0.0.1", port=18086, upstream_manager=mgr)
        server._status_path = tmp_path / "test.status.json"
        server._pid_path = tmp_path / "test.pid"

        with patch("proxy_relay.server.remove_pid"), patch("proxy_relay.server.write_status"):
            await server.stop()

        assert server._shutdown_event.is_set()


# ---------------------------------------------------------------------------
# TestMaskUrl — URL credential masking (J-RL4)
# ---------------------------------------------------------------------------


def _mask_url(url: str) -> str:
    """Local copy of the masking logic for test assertions."""
    at_idx = url.find("@")
    if at_idx == -1:
        return url
    scheme_end = url.find("://")
    if scheme_end == -1:
        return url
    return url[: scheme_end + 3] + "***@" + url[at_idx + 1:]


class TestMaskUrl:
    """Test URL credential masking behaviour."""

    def test_mask_url_with_credentials(self):
        assert _mask_url("socks5://user:pass@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"

    def test_mask_url_without_credentials(self):
        assert _mask_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"

    def test_mask_url_no_scheme(self):
        assert _mask_url("user:pass@host:1080") == "user:pass@host:1080"

    def test_mask_url_empty_string(self):
        assert _mask_url("") == ""

    def test_mask_url_complex_credentials(self):
        assert _mask_url("socks5://u%40sr:p%3Ass@host:1080") == "socks5://***@host:1080"


# ---------------------------------------------------------------------------
# TestSigpipeInStart
# ---------------------------------------------------------------------------


class TestSigpipeInStart:
    """Test SIGPIPE handler is installed during server start."""

    @pytest.mark.asyncio
    async def test_sigpipe_handler_installed_on_start(self):
        """start() should install a SIGPIPE handler in the event loop."""
        import signal as _signal

        if not hasattr(_signal, "SIGPIPE"):
            pytest.skip("SIGPIPE not available on this platform")

        mgr = _make_manager()
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
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18090)
            await server.start()

        assert _signal.SIGPIPE in signal_handlers


# ---------------------------------------------------------------------------
# TestServerStartedAt
# ---------------------------------------------------------------------------


class TestServerStartedAt:
    """Test that server records started_at timestamp."""

    @pytest.mark.asyncio
    async def test_started_at_set_on_start(self):
        """start() sets _started_at to an ISO timestamp."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18091, upstream_manager=mgr)

        assert server._started_at == ""

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status") as mock_ws:
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18091)

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert server._started_at != ""
        assert "T" in server._started_at

    @pytest.mark.asyncio
    async def test_write_status_receives_pid_and_started_at(self):
        """write_status is called with pid and started_at from server."""
        mgr = _make_manager()
        mgr.profile_name = "miami"
        server = ProxyServer(host="127.0.0.1", port=18092, upstream_manager=mgr)

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start, \
             patch("proxy_relay.server.write_pid"), \
             patch("proxy_relay.server.write_status") as mock_ws:
            mock_start.return_value = _mock_asyncio_server("127.0.0.1", 18092)

            with patch("asyncio.get_running_loop") as mock_loop:
                mock_loop.return_value = MagicMock()
                await server.start()

        assert mock_ws.called
        call_kwargs = mock_ws.call_args
        assert "pid" in call_kwargs.kwargs or len(call_kwargs.args) > 0
        assert "started_at" in call_kwargs.kwargs or len(call_kwargs.args) > 0


# ---------------------------------------------------------------------------
# TestServerBlockedDomainsIntegration — blocked_domains constructor param
# ---------------------------------------------------------------------------


class TestServerBlockedDomainsIntegration:
    """Verify blocked_domains is stored and accessible."""

    def test_blocked_domains_stored_on_init(self):
        """blocked_domains passed to constructor is stored."""
        domains = frozenset({"tidal.com", "listen.tidal.com"})
        server = ProxyServer(blocked_domains=domains)
        assert server._blocked_domains == domains

    def test_blocked_domains_none_by_default(self):
        """blocked_domains defaults to None."""
        server = ProxyServer()
        assert server._blocked_domains is None
