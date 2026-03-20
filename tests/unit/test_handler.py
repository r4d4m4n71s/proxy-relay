"""Tests for proxy_relay.handler — handle_connection dispatch logic."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxy_relay.tunnel import TunnelResult
from proxy_relay.upstream import UpstreamInfo


def _make_upstream() -> UpstreamInfo:
    return UpstreamInfo(
        host="proxy.example.com", port=12322,
        username="user", password="pass",
        url="socks5://***@proxy.example.com:12322", country="us",
    )


def _make_tunnel_result(
    reader: AsyncMock | None = None,
    writer: AsyncMock | None = None,
    latency_ms: float = 50.0,
) -> TunnelResult:
    """Build a TunnelResult with mock streams."""
    if reader is None:
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")
    if writer is None:
        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
    return TunnelResult(reader=reader, writer=writer, latency_ms=latency_ms)


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
        tunnel_result = _make_tunnel_result()

        with patch("proxy_relay.handler.open_tunnel", new_callable=AsyncMock) as mock_tunnel, \
             patch("proxy_relay.handler.relay_data", new_callable=AsyncMock) as mock_relay:
            mock_tunnel.return_value = tunnel_result
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


class TestHandlerMonitorIntegration:
    """Test handle_connection with the optional monitor parameter."""

    @pytest.mark.asyncio
    async def test_monitor_none_does_not_crash(self):
        """handle_connection with monitor=None should work (backward compat)."""
        from proxy_relay.handler import handle_connection

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
        tunnel_result = _make_tunnel_result()

        with patch("proxy_relay.handler.open_tunnel", new_callable=AsyncMock) as mock_tunnel, \
             patch("proxy_relay.handler.relay_data", new_callable=AsyncMock):
            mock_tunnel.return_value = tunnel_result

            # Should not raise -- monitor=None is the default
            await handle_connection(reader, writer, upstream)

    @pytest.mark.asyncio
    async def test_monitor_record_success_called_on_connect(self):
        """On successful CONNECT, monitor.record_success should be called."""
        from proxy_relay.handler import handle_connection

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
        tunnel_result = _make_tunnel_result(latency_ms=75.0)

        # Create a mock monitor
        mock_monitor = AsyncMock()
        mock_monitor.enabled = True
        mock_monitor.record_success = AsyncMock()
        mock_monitor.record_error = AsyncMock()

        with patch("proxy_relay.handler.open_tunnel", new_callable=AsyncMock) as mock_tunnel, \
             patch("proxy_relay.handler.relay_data", new_callable=AsyncMock):
            mock_tunnel.return_value = tunnel_result

            await handle_connection(reader, writer, upstream, monitor=mock_monitor)

            mock_monitor.record_success.assert_called_once()
            # Verify latency was passed through
            call_args = mock_monitor.record_success.call_args
            assert call_args[0][0] == pytest.approx(75.0)

    @pytest.mark.asyncio
    async def test_monitor_record_error_called_on_tunnel_error(self):
        """On TunnelError, monitor.record_error should be called."""
        from proxy_relay.exceptions import TunnelError
        from proxy_relay.handler import handle_connection

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

        mock_monitor = AsyncMock()
        mock_monitor.record_success = AsyncMock()
        mock_monitor.record_error = AsyncMock()

        with patch("proxy_relay.handler.open_tunnel", new_callable=AsyncMock) as mock_tunnel, \
             patch("proxy_relay.handler.relay_data", new_callable=AsyncMock):
            mock_tunnel.side_effect = TunnelError("SOCKS5 handshake failed")

            await handle_connection(reader, writer, upstream, monitor=mock_monitor)

            mock_monitor.record_error.assert_called_once()


# ---------------------------------------------------------------------------
# F-RL10: _read_chunked_body trailer consumption
# ---------------------------------------------------------------------------
class TestReadChunkedBodyTrailers:
    """Test F-RL10: _read_chunked_body consumes trailing CRLF and optional trailers."""

    @pytest.mark.asyncio
    async def test_basic_chunked_body_no_trailers(self):
        """Basic chunked body without trailers is dechunked correctly."""
        from proxy_relay.handler import _read_chunked_body

        # "5\r\nhello\r\n0\r\n\r\n"
        raw = b"5\r\nhello\r\n0\r\n\r\n"

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")  # No additional data needed

        body = await _read_chunked_body(reader, raw, 1024)
        assert body == b"hello"

    @pytest.mark.asyncio
    async def test_chunked_body_with_trailers(self):
        """Chunked body with trailer headers is dechunked; trailers are consumed."""
        from proxy_relay.handler import _read_chunked_body

        # "5\r\nhello\r\n0\r\nX-Trailer: v\r\n\r\n"
        raw = b"5\r\nhello\r\n0\r\nX-Trailer: v\r\n\r\n"

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        body = await _read_chunked_body(reader, raw, 1024)
        assert body == b"hello"

    @pytest.mark.asyncio
    async def test_chunked_body_multiple_trailers(self):
        """Multiple trailer headers are all consumed."""
        from proxy_relay.handler import _read_chunked_body

        raw = b"3\r\nabc\r\n0\r\nX-A: 1\r\nX-B: 2\r\n\r\n"

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        body = await _read_chunked_body(reader, raw, 1024)
        assert body == b"abc"

    @pytest.mark.asyncio
    async def test_chunked_body_too_many_trailers_raises(self):
        """J-RL6: >100 trailer lines raises TunnelError."""
        from proxy_relay.exceptions import TunnelError
        from proxy_relay.handler import _read_chunked_body

        # Build: "0\r\n" + 101 non-empty trailer lines + "\r\n" (never reached)
        trailer_lines = b"".join(f"X-T-{i}: v\r\n".encode() for i in range(101))
        raw = b"0\r\n" + trailer_lines + b"\r\n"

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        with pytest.raises(TunnelError, match="Too many trailer"):
            await _read_chunked_body(reader, raw, 1024)

    @pytest.mark.asyncio
    async def test_chunked_body_99_trailers_ok(self):
        """J-RL6: 99 trailer lines + terminator is within the cap."""
        from proxy_relay.handler import _read_chunked_body

        trailer_lines = b"".join(f"X-T-{i}: v\r\n".encode() for i in range(99))
        raw = b"3\r\nabc\r\n0\r\n" + trailer_lines + b"\r\n"

        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=b"")

        body = await _read_chunked_body(reader, raw, 1024)
        assert body == b"abc"
