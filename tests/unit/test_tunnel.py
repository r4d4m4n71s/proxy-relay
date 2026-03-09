"""Tests for proxy_relay.tunnel — SOCKS5 tunnel establishment and DNS leak prevention."""
from __future__ import annotations

import ast
import asyncio
import importlib
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


class TestTunnelDNSLeak:
    """CRITICAL: Verify tunnel.py never resolves DNS locally."""

    def test_no_dns_resolution_in_tunnel(self):
        """CRITICAL: tunnel.py must NEVER use DNS resolution functions."""
        dangerous = {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getfqdn"}

        spec = importlib.util.find_spec("proxy_relay.tunnel")
        assert spec is not None, "proxy_relay.tunnel module not found"
        assert spec.origin is not None

        source_path = Path(spec.origin)
        source = source_path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in dangerous:
                raise AssertionError(
                    f"proxy_relay/tunnel.py uses {node.attr} -- DNS leak risk!"
                )
            if isinstance(node, ast.Name) and node.id in dangerous:
                raise AssertionError(
                    f"proxy_relay/tunnel.py references {node.id} -- DNS leak risk!"
                )

    def test_no_socket_dns_import_in_tunnel(self):
        """Verify tunnel.py does not import socket DNS functions."""
        spec = importlib.util.find_spec("proxy_relay.tunnel")
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
                        f"tunnel.py imports DNS functions from socket: {dangerous_imports}"
                    )


class TestOpenTunnel:
    """Test open_tunnel behavioral contracts."""

    @pytest.mark.asyncio
    async def test_hostname_passed_as_string_not_ip(self):
        """DNS leak behavioral test: hostname must reach SOCKS connector as-is."""
        from proxy_relay.tunnel import open_tunnel

        upstream = _make_upstream()
        target_host = "example.com"
        target_port = 443

        mock_sock = MagicMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)

        proxy_instance = MagicMock()
        proxy_instance.connect = AsyncMock(return_value=mock_sock)

        with patch("python_socks.async_.asyncio.Proxy", return_value=proxy_instance) as MockProxy, \
             patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_open.return_value = (mock_reader, mock_writer)

            result = await open_tunnel(target_host, target_port, upstream)

            # Result may be a TunnelResult (3-tuple) or plain (reader, writer)
            # Either way, the SOCKS connector must receive the hostname as string
            proxy_instance.connect.assert_called_once_with(
                dest_host=target_host,
                dest_port=target_port,
            )

    @pytest.mark.asyncio
    async def test_rdns_enabled(self):
        """Remote DNS resolution (rdns=True) must be set on the Proxy."""
        from proxy_relay.tunnel import open_tunnel

        upstream = _make_upstream()

        mock_sock = MagicMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)

        proxy_instance = MagicMock()
        proxy_instance.connect = AsyncMock(return_value=mock_sock)

        with patch("python_socks.async_.asyncio.Proxy", return_value=proxy_instance) as MockProxy, \
             patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_open.return_value = (mock_reader, mock_writer)

            _result = await open_tunnel("example.com", 443, upstream)

            # Verify rdns=True was passed to Proxy constructor
            call_kwargs = MockProxy.call_args.kwargs
            assert call_kwargs.get("rdns") is True


class TestTunnelResult:
    """Test TunnelResult NamedTuple returned by open_tunnel."""

    @pytest.mark.asyncio
    async def test_open_tunnel_returns_tunnel_result(self):
        """open_tunnel should return a TunnelResult with reader, writer, latency_ms."""
        from proxy_relay.tunnel import TunnelResult, open_tunnel

        upstream = _make_upstream()

        mock_sock = MagicMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)

        proxy_instance = MagicMock()
        proxy_instance.connect = AsyncMock(return_value=mock_sock)

        with patch("python_socks.async_.asyncio.Proxy", return_value=proxy_instance), \
             patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_open.return_value = (mock_reader, mock_writer)

            result = await open_tunnel("example.com", 443, upstream)

            # Should be a TunnelResult with all three fields
            assert hasattr(result, "reader")
            assert hasattr(result, "writer")
            assert hasattr(result, "latency_ms")
            assert result.reader is mock_reader
            assert result.writer is mock_writer
            assert result.latency_ms >= 0.0

    def test_tunnel_result_is_named_tuple(self):
        """TunnelResult should be a NamedTuple with 3 fields."""
        from proxy_relay.tunnel import TunnelResult

        # Verify it has the expected field names
        assert "reader" in TunnelResult._fields
        assert "writer" in TunnelResult._fields
        assert "latency_ms" in TunnelResult._fields

    @pytest.mark.asyncio
    async def test_tunnel_result_unpacks_to_three_values(self):
        """TunnelResult can be unpacked into reader, writer, latency_ms."""
        from proxy_relay.tunnel import open_tunnel

        upstream = _make_upstream()

        mock_sock = MagicMock()
        mock_reader = AsyncMock(spec=asyncio.StreamReader)
        mock_writer = AsyncMock(spec=asyncio.StreamWriter)

        proxy_instance = MagicMock()
        proxy_instance.connect = AsyncMock(return_value=mock_sock)

        with patch("python_socks.async_.asyncio.Proxy", return_value=proxy_instance), \
             patch("asyncio.open_connection", new_callable=AsyncMock) as mock_open:
            mock_open.return_value = (mock_reader, mock_writer)

            reader, writer, latency_ms = await open_tunnel("example.com", 443, upstream)

            assert reader is mock_reader
            assert writer is mock_writer
            assert latency_ms >= 0.0


class TestRelayData:
    """Test relay_data bidirectional byte relay."""

    @pytest.mark.asyncio
    async def test_bidirectional_relay(self):
        """Data flows from client to remote and vice versa."""
        from proxy_relay.tunnel import relay_data

        client_data = b"client payload"
        remote_data = b"remote response"

        client_reader = AsyncMock(spec=asyncio.StreamReader)
        client_reader.read = AsyncMock(side_effect=[client_data, b""])

        client_writer = AsyncMock(spec=asyncio.StreamWriter)
        client_writer.write = MagicMock()
        client_writer.drain = AsyncMock()
        client_writer.close = MagicMock()

        remote_reader = AsyncMock(spec=asyncio.StreamReader)
        remote_reader.read = AsyncMock(side_effect=[remote_data, b""])

        remote_writer = AsyncMock(spec=asyncio.StreamWriter)
        remote_writer.write = MagicMock()
        remote_writer.drain = AsyncMock()
        remote_writer.close = MagicMock()

        await relay_data(client_reader, client_writer, remote_reader, remote_writer)

        # Client data should have been written to remote
        remote_writes = b"".join(
            call.args[0] for call in remote_writer.write.call_args_list
            if call.args
        )
        assert client_data in remote_writes

        # Remote data should have been written to client
        client_writes = b"".join(
            call.args[0] for call in client_writer.write.call_args_list
            if call.args
        )
        assert remote_data in client_writes
