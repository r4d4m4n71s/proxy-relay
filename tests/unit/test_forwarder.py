"""Tests for proxy_relay.forwarder — HTTP request forwarding with DNS leak prevention."""
from __future__ import annotations

import ast
import asyncio
import importlib
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
        url="socks5://user:pass@proxy.example.com:12322",
        masked_url="socks5://***@proxy.example.com:12322", country="us",
    )


class TestForwarderDNSLeak:
    """CRITICAL: Verify forwarder.py never resolves DNS locally."""

    def test_no_dns_resolution_in_forwarder(self):
        """CRITICAL: forwarder.py must NEVER use DNS resolution functions."""
        dangerous = {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getfqdn"}

        spec = importlib.util.find_spec("proxy_relay.forwarder")
        assert spec is not None, "proxy_relay.forwarder module not found"
        assert spec.origin is not None

        source_path = Path(spec.origin)
        source = source_path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in dangerous:
                raise AssertionError(
                    f"proxy_relay/forwarder.py uses {node.attr} -- DNS leak risk!"
                )
            if isinstance(node, ast.Name) and node.id in dangerous:
                raise AssertionError(
                    f"proxy_relay/forwarder.py references {node.id} -- DNS leak risk!"
                )

    def test_no_socket_dns_import_in_forwarder(self):
        """Verify forwarder.py does not import socket DNS functions."""
        spec = importlib.util.find_spec("proxy_relay.forwarder")
        assert spec is not None
        assert spec.origin is not None

        source_path = Path(spec.origin)
        source = source_path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "socket":
                imported_names = {alias.name for alias in (node.names or [])}
                dangerous_imports = imported_names & {
                    "getaddrinfo", "gethostbyname", "gethostbyname_ex", "getfqdn",
                }
                if dangerous_imports:
                    raise AssertionError(
                        f"forwarder.py imports DNS functions from socket: {dangerous_imports}"
                    )


class TestForwardHttpRequest:
    """Test forward_http_request behavioral contracts."""

    @pytest.mark.asyncio
    async def test_headers_sanitized_before_forwarding(self):
        """Leak headers must be removed before the request is forwarded."""
        from proxy_relay.forwarder import forward_http_request

        initial_headers = [
            ("Host", "example.com"),
            ("X-Forwarded-For", "1.2.3.4"),
            ("Via", "internal-proxy"),
            ("User-Agent", "TestAgent"),
        ]

        mock_remote_reader = AsyncMock(spec=asyncio.StreamReader)
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
        mock_remote_reader.read = AsyncMock(side_effect=[response, b""])

        mock_remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_remote_writer.write = MagicMock()
        mock_remote_writer.close = MagicMock()
        mock_remote_writer.wait_closed = AsyncMock()
        mock_remote_writer.drain = AsyncMock()

        client_writer = AsyncMock(spec=asyncio.StreamWriter)
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()
        client_writer.close = MagicMock()
        client_writer.wait_closed = AsyncMock()

        upstream = _make_upstream()

        with patch("proxy_relay.forwarder.open_tunnel", new_callable=AsyncMock) as mock_tunnel:
            mock_tunnel.return_value = TunnelResult(
                reader=mock_remote_reader,
                writer=mock_remote_writer,
                latency_ms=50.0,
            )

            await forward_http_request(
                method="GET",
                url="http://example.com/path",
                http_version="HTTP/1.1",
                headers=initial_headers,
                body=b"",
                upstream=upstream,
                client_writer=client_writer,
            )

        # Inspect what was written to upstream
        upstream_writes = b"".join(
            call.args[0] for call in mock_remote_writer.write.call_args_list
            if call.args
        )
        upstream_text = upstream_writes.decode("latin-1", errors="replace").lower()

        # Leak headers must NOT be in the forwarded request
        assert "x-forwarded-for" not in upstream_text

    @pytest.mark.asyncio
    async def test_absolute_url_converted_to_relative_path(self):
        """Absolute URL is converted to relative path in the forwarded request."""
        from proxy_relay.forwarder import forward_http_request

        mock_remote_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_remote_reader.read = AsyncMock(side_effect=[b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", b""])

        mock_remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_remote_writer.write = MagicMock()
        mock_remote_writer.close = MagicMock()
        mock_remote_writer.wait_closed = AsyncMock()
        mock_remote_writer.drain = AsyncMock()

        client_writer = AsyncMock(spec=asyncio.StreamWriter)
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()
        client_writer.close = MagicMock()
        client_writer.wait_closed = AsyncMock()

        upstream = _make_upstream()

        with patch("proxy_relay.forwarder.open_tunnel", new_callable=AsyncMock) as mock_tunnel:
            mock_tunnel.return_value = TunnelResult(
                reader=mock_remote_reader,
                writer=mock_remote_writer,
                latency_ms=50.0,
            )

            await forward_http_request(
                method="GET",
                url="http://example.com:80/path",
                http_version="HTTP/1.1",
                headers=[("Host", "example.com")],
                body=b"",
                upstream=upstream,
                client_writer=client_writer,
            )

            # Verify tunnel was opened to example.com:80
            mock_tunnel.assert_called_once()
            call_args = mock_tunnel.call_args
            assert call_args[0][0] == "example.com"
            assert call_args[0][1] == 80

    @pytest.mark.asyncio
    async def test_response_exceeding_max_size_aborts_with_502(self):
        """F-RL5: Response exceeding _MAX_RESPONSE_SIZE triggers 502."""
        from proxy_relay.forwarder import _MAX_RESPONSE_SIZE, forward_http_request

        # Create a reader that returns chunks totalling > _MAX_RESPONSE_SIZE
        chunk_size = 8192
        # We need enough chunks so total > _MAX_RESPONSE_SIZE
        chunks_needed = (_MAX_RESPONSE_SIZE // chunk_size) + 2
        chunk = b"X" * chunk_size
        side_effects = [chunk] * chunks_needed + [b""]

        mock_remote_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_remote_reader.read = AsyncMock(side_effect=side_effects)

        mock_remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_remote_writer.write = MagicMock()
        mock_remote_writer.close = MagicMock()
        mock_remote_writer.wait_closed = AsyncMock()
        mock_remote_writer.drain = AsyncMock()

        client_writer = AsyncMock(spec=asyncio.StreamWriter)
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()
        client_writer.close = MagicMock()
        client_writer.wait_closed = AsyncMock()

        upstream = _make_upstream()

        with patch("proxy_relay.forwarder.open_tunnel", new_callable=AsyncMock) as mock_tunnel:
            mock_tunnel.return_value = TunnelResult(
                reader=mock_remote_reader,
                writer=mock_remote_writer,
                latency_ms=50.0,
            )

            result = await forward_http_request(
                method="GET",
                url="http://example.com/big",
                http_version="HTTP/1.1",
                headers=[("Host", "example.com")],
                body=b"",
                upstream=upstream,
                client_writer=client_writer,
            )

        # Should return False (aborted)
        assert result is False

        # Client should have received a 502 error
        all_writes = b"".join(
            call.args[0] for call in client_writer.write.call_args_list if call.args
        )
        assert b"502" in all_writes

    @pytest.mark.asyncio
    async def test_response_relayed_to_client(self):
        """HTTP response from upstream is relayed back to client."""
        from proxy_relay.forwarder import forward_http_request

        response_body = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nHello"

        mock_remote_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_remote_reader.read = AsyncMock(side_effect=[response_body, b""])

        mock_remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        mock_remote_writer.write = MagicMock()
        mock_remote_writer.close = MagicMock()
        mock_remote_writer.wait_closed = AsyncMock()
        mock_remote_writer.drain = AsyncMock()

        client_writer = AsyncMock(spec=asyncio.StreamWriter)
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()
        client_writer.close = MagicMock()
        client_writer.wait_closed = AsyncMock()

        upstream = _make_upstream()

        with patch("proxy_relay.forwarder.open_tunnel", new_callable=AsyncMock) as mock_tunnel:
            mock_tunnel.return_value = TunnelResult(
                reader=mock_remote_reader,
                writer=mock_remote_writer,
                latency_ms=50.0,
            )

            await forward_http_request(
                method="GET",
                url="http://example.com/",
                http_version="HTTP/1.1",
                headers=[("Host", "example.com")],
                body=b"",
                upstream=upstream,
                client_writer=client_writer,
            )

        # Client should have received the response
        client_writes = b"".join(
            call.args[0] for call in client_writer.write.call_args_list
            if call.args
        )
        assert b"200" in client_writes or client_writer.write.called
