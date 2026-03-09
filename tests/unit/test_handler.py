"""Tests for proxy_relay.handler — handle_connection dispatch logic."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_relay.upstream import UpstreamInfo


def _make_upstream() -> UpstreamInfo:
    return UpstreamInfo(
        host="proxy.example.com", port=12322,
        username="user", password="pass",
        url="socks5://***@proxy.example.com:12322", country="us",
    )


class TestHandlerDNSLeak:
    """CRITICAL: Verify handler.py never resolves DNS locally."""

    def test_no_dns_resolution_in_handler(self):
        """CRITICAL: handler.py must NEVER use DNS resolution functions."""
        dangerous = {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getfqdn"}

        spec = importlib.util.find_spec("proxy_relay.handler")
        assert spec is not None, "proxy_relay.handler module not found"
        assert spec.origin is not None

        source_path = Path(spec.origin)
        source = source_path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in dangerous:
                raise AssertionError(
                    f"proxy_relay/handler.py uses {node.attr} -- DNS leak risk!"
                )
            if isinstance(node, ast.Name) and node.id in dangerous:
                raise AssertionError(
                    f"proxy_relay/handler.py references {node.id} -- DNS leak risk!"
                )


class TestHandleConnection:
    """Test handle_connection request dispatch."""

    @pytest.mark.asyncio
    async def test_connect_dispatches_to_tunnel(self):
        """CONNECT request dispatches to open_tunnel + relay_data."""
        from proxy_relay.handler import handle_connection

        # Build a reader that returns a CONNECT request
        reader = AsyncMock(spec=asyncio.StreamReader)
        request = (
            b"CONNECT example.com:443 HTTP/1.1\r\n"
            b"Host: example.com:443\r\n"
            b"\r\n"
        )
        reader.read = AsyncMock(return_value=request)

        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 9999))

        upstream = _make_upstream()

        mock_remote_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_remote_reader.read = AsyncMock(return_value=b"")
        mock_remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_remote_writer.close = MagicMock()
        mock_remote_writer.wait_closed = AsyncMock()

        with patch("proxy_relay.handler.open_tunnel", new_callable=AsyncMock) as mock_tunnel, \
             patch("proxy_relay.handler.relay_data", new_callable=AsyncMock) as mock_relay:
            mock_tunnel.return_value = (mock_remote_reader, mock_remote_writer)
            await handle_connection(reader, writer, upstream)

            mock_tunnel.assert_called_once()
            call_args = mock_tunnel.call_args
            assert call_args[0][0] == "example.com"
            assert call_args[0][1] == 443

    @pytest.mark.asyncio
    async def test_get_dispatches_to_forwarder(self):
        """GET request dispatches to forward_http_request."""
        from proxy_relay.handler import handle_connection

        reader = AsyncMock(spec=asyncio.StreamReader)
        request = (
            b"GET http://example.com/ HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"\r\n"
        )
        reader.read = AsyncMock(return_value=request)

        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 9999))

        upstream = _make_upstream()

        with patch("proxy_relay.handler.forward_http_request", new_callable=AsyncMock) as mock_fwd:
            await handle_connection(reader, writer, upstream)
            mock_fwd.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_request_closes_connection(self):
        """Empty request (EOF) closes connection gracefully."""
        from proxy_relay.handler import handle_connection

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 9999))

        upstream = _make_upstream()

        # Should not raise
        await handle_connection(reader, writer, upstream)

        # Writer should be closed
        assert writer.close.called
