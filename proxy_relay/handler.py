"""Connection handler: parse HTTP/CONNECT requests and dispatch.

Reads the initial HTTP request line and headers from the client,
determines whether it is a CONNECT tunnel request or a plain HTTP
request, and dispatches to the appropriate handler.
"""
from __future__ import annotations

import asyncio
import time

from proxy_relay.exceptions import TunnelError
from proxy_relay.forwarder import forward_http_request
from proxy_relay.logger import get_logger
from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome
from proxy_relay.tunnel import open_tunnel, relay_data
from proxy_relay.upstream import UpstreamInfo

log = get_logger(__name__)

# Maximum request line + headers size (64 KiB)
_MAX_HEADER_SIZE: int = 65536

# Maximum time to wait for the initial request line (seconds)
_REQUEST_TIMEOUT: float = 30.0


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream: UpstreamInfo,
    monitor: ConnectionMonitor | None = None,
) -> None:
    """Handle a single client connection to the proxy.

    Reads the HTTP request line and headers, then dispatches to either
    the CONNECT tunnel handler or the plain HTTP forwarder.

    Args:
        client_reader: Reader from the connected client.
        client_writer: Writer to the connected client.
        upstream: Upstream SOCKS5 connection parameters.
        monitor: Optional connection quality monitor for recording outcomes.
    """
    peer = client_writer.get_extra_info("peername")
    peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"

    log.debug("New connection from %s", peer_str)
    start = time.monotonic()

    try:
        # Read the request line and headers
        method, target, http_version, headers, body_start = await asyncio.wait_for(
            _read_request(client_reader),
            timeout=_REQUEST_TIMEOUT,
        )

        if method == "CONNECT":
            await _handle_connect(
                target,
                upstream,
                client_reader,
                client_writer,
                monitor=monitor,
            )
        else:
            await _handle_http(
                method,
                target,
                http_version,
                headers,
                body_start,
                upstream,
                client_reader,
                client_writer,
            )

    except asyncio.TimeoutError:
        log.warning("Request timeout from %s", peer_str)
        if monitor is not None:
            await monitor.record_error(ConnectionOutcome.TIMEOUT, peer_str, "request timeout")
        await _send_error(client_writer, 408, "Request Timeout")
    except TunnelError as exc:
        log.warning("Tunnel error for %s: %s", peer_str, exc)
        if monitor is not None:
            await monitor.record_error(
                ConnectionOutcome.TUNNEL_ERROR, peer_str, str(exc),
            )
        await _send_error(client_writer, 502, "Bad Gateway")
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("Connection error from %s: %s", peer_str, exc)
        if monitor is not None:
            await monitor.record_error(ConnectionOutcome.RESET, peer_str, str(exc))
    except Exception as exc:
        log.error("Unexpected error from %s: %s", peer_str, exc, exc_info=True)
        if monitor is not None:
            await monitor.record_error(
                ConnectionOutcome.TUNNEL_ERROR, peer_str, str(exc),
            )
        await _send_error(client_writer, 500, "Internal Server Error")
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        log.debug("Connection from %s closed (%.0fms)", peer_str, elapsed_ms)
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except OSError:
            pass


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, str, list[tuple[str, str]], bytes]:
    """Read and parse the HTTP request line and headers.

    Args:
        reader: Client stream reader.

    Returns:
        Tuple of (method, target, http_version, headers, body_start) where:
        - method: HTTP method (GET, CONNECT, etc.)
        - target: Request target (host:port for CONNECT, URL for others)
        - http_version: HTTP version string
        - headers: List of (name, value) header tuples
        - body_start: Any bytes read past the header boundary

    Raises:
        TunnelError: If the request is malformed or too large.
    """
    # Read until we find the empty line marking end of headers
    header_data = b""
    while b"\r\n\r\n" not in header_data:
        chunk = await reader.read(4096)
        if not chunk:
            raise TunnelError("Client disconnected before sending complete headers")
        header_data += chunk
        if len(header_data) > _MAX_HEADER_SIZE:
            raise TunnelError(
                f"Request headers exceed maximum size ({_MAX_HEADER_SIZE} bytes)"
            )

    # Split headers from any body data that was read
    header_end = header_data.index(b"\r\n\r\n")
    header_block = header_data[: header_end]
    body_start = header_data[header_end + 4 :]

    # Parse request line
    lines = header_block.split(b"\r\n")
    request_line = lines[0].decode("latin-1")
    parts = request_line.split(" ", 2)
    if len(parts) < 2:
        raise TunnelError(f"Malformed request line: {request_line!r}")

    method = parts[0].upper()
    target = parts[1]
    http_version = parts[2] if len(parts) > 2 else "HTTP/1.1"

    # Parse headers
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        decoded = line.decode("latin-1")
        colon_idx = decoded.find(":")
        if colon_idx == -1:
            continue
        name = decoded[:colon_idx].strip()
        value = decoded[colon_idx + 1 :].strip()
        headers.append((name, value))

    log.debug("Parsed: %s %s %s (%d headers)", method, target, http_version, len(headers))
    return method, target, http_version, headers, body_start


async def _handle_connect(
    target: str,
    upstream: UpstreamInfo,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    monitor: ConnectionMonitor | None = None,
) -> None:
    """Handle a CONNECT tunnel request.

    Opens a SOCKS5 tunnel to the target, sends 200 to the client,
    then relays bytes bidirectionally.

    Args:
        target: Target as "host:port".
        upstream: Upstream SOCKS5 connection parameters.
        client_reader: Client stream reader.
        client_writer: Client stream writer.
        monitor: Optional connection quality monitor.

    Raises:
        TunnelError: If the tunnel cannot be established.
    """
    host, port = _parse_connect_target(target)

    log.info("CONNECT %s:%d", host, port)

    # Open the SOCKS5 tunnel (hostname passed as string — no local DNS!)
    result = await open_tunnel(host, port, upstream)
    remote_reader = result.reader
    remote_writer = result.writer
    latency_ms = result.latency_ms

    # Record successful tunnel establishment
    if monitor is not None:
        await monitor.record_success(latency_ms, target)

    # Tell the client the tunnel is established
    client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await client_writer.drain()

    # Bidirectional relay (TLS traffic flows opaquely)
    await relay_data(client_reader, client_writer, remote_reader, remote_writer)


async def _handle_http(
    method: str,
    url: str,
    http_version: str,
    headers: list[tuple[str, str]],
    body_start: bytes,
    upstream: UpstreamInfo,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Handle a plain HTTP request by forwarding through SOCKS5.

    Reads any remaining request body (based on Content-Length), then
    delegates to the forwarder.

    Args:
        method: HTTP method.
        url: Absolute URL from the request line.
        http_version: HTTP version string.
        headers: Parsed request headers.
        body_start: Any body bytes already read past the headers.
        upstream: Upstream SOCKS5 connection parameters.
        client_reader: Client stream reader.
        client_writer: Client stream writer.
    """
    # Read remaining body if Content-Length is set
    body = body_start
    content_length = 0
    for name, value in headers:
        if name.lower() == "content-length":
            try:
                content_length = int(value)
            except ValueError:
                pass
            break

    if content_length > len(body):
        remaining = content_length - len(body)
        extra = await client_reader.read(remaining)
        body += extra

    await forward_http_request(
        method=method,
        url=url,
        http_version=http_version,
        headers=headers,
        body=body,
        upstream=upstream,
        client_writer=client_writer,
    )


def _parse_connect_target(target: str) -> tuple[str, int]:
    """Parse a CONNECT target into (host, port).

    Args:
        target: Target string in "host:port" format.

    Returns:
        Tuple of (hostname, port).

    Raises:
        TunnelError: If the target cannot be parsed.
    """
    if ":" not in target:
        raise TunnelError(f"CONNECT target missing port: {target!r}")

    host, port_str = target.rsplit(":", 1)

    # Handle IPv6 bracket notation: [::1]:443
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    try:
        port = int(port_str)
    except ValueError as exc:
        raise TunnelError(f"Invalid port in CONNECT target: {target!r}") from exc

    if not host:
        raise TunnelError(f"Empty host in CONNECT target: {target!r}")

    if port < 1 or port > 65535:
        raise TunnelError(f"Port out of range in CONNECT target: {target!r}")

    return host, port


async def _send_error(
    writer: asyncio.StreamWriter,
    status_code: int,
    reason: str,
) -> None:
    """Send a minimal HTTP error response.

    Args:
        writer: Client stream writer.
        status_code: HTTP status code.
        reason: HTTP reason phrase.
    """
    body = f"{status_code} {reason}\r\n"
    response = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
        f"{body}"
    )
    try:
        writer.write(response.encode("latin-1"))
        await writer.drain()
    except OSError:
        pass
