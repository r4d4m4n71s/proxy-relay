"""Tests for proxy_relay.server — ProxyServer start/stop and connection handling."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_relay.server import ProxyServer
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

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start:
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
    async def test_server_stop_closes_cleanly(self):
        """Server stop closes the underlying asyncio server."""
        mgr = self._make_manager()
        server = ProxyServer(host="127.0.0.1", port=18081, upstream_manager=mgr)

        with patch("asyncio.start_server", new_callable=AsyncMock) as mock_start:
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
