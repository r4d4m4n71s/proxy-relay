"""Connection handler: parse HTTP/CONNECT requests and dispatch.

Reads the initial HTTP request line and headers from the client,
determines whether it is a CONNECT tunnel request or a plain HTTP
request, and dispatches to the appropriate handler.
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from proxy_relay.exceptions import TunnelError
from proxy_relay.forwarder import forward_http_request
from proxy_relay.logger import get_logger
from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome
from proxy_relay.response import send_error
from proxy_relay.tunnel import open_tunnel, relay_data
from proxy_relay.upstream import UpstreamInfo

log = get_logger(__name__)

# Maximum request line + headers size (64 KiB)
_MAX_HEADER_SIZE: int = 65536

# Maximum allowed Content-Length for plain HTTP request bodies (10 MiB)
_MAX_BODY_SIZE: int = 10 * 1024 * 1024

# Maximum time to wait for the initial request line (seconds)
_REQUEST_TIMEOUT: float = 30.0


# Internal health endpoint path.
HEALTH_PATH: str = "/__health"


def _is_loopback(peer_str: str) -> bool:
    """Return True if peer address is loopback (127.x.x.x or ::1).

    Args:
        peer_str: Peer address string in "host:port" or "[ipv6]:port" format.

    Returns:
        True if the peer IP is a loopback address, False otherwise (including
        on parse error, which fails safe by denying access).
    """
    try:
        host = peer_str.rsplit(":", 1)[0].strip("[]")
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, IndexError):
        return False


def _is_domain_blocked(host: str, blocked_domains: frozenset[str]) -> bool:
    """Return True if host matches or is a subdomain of any blocked domain.

    Args:
        host: Hostname to check (without port, without IPv6 brackets).
        blocked_domains: Set of domain names to block (e.g. ``{"tidal.com"}``).

    Returns:
        True if *host* is an exact match or a subdomain of any entry in
        *blocked_domains*.
    """
    host_lower = host.lower().strip("[]")  # handle IPv6 brackets
    for domain in blocked_domains:
        if host_lower == domain or host_lower.endswith("." + domain):
            return True
    return False


async def handle_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream: UpstreamInfo,
    monitor: ConnectionMonitor | None = None,
    health_callback: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
    blocked_domains: frozenset[str] | None = None,
) -> None:
    """Handle a single client connection to the proxy.

    Reads the HTTP request line and headers, then dispatches to either
    the CONNECT tunnel handler or the plain HTTP forwarder.

    Args:
        client_reader: Reader from the connected client.
        client_writer: Writer to the connected client.
        upstream: Upstream SOCKS5 connection parameters.
        monitor: Optional connection quality monitor for recording outcomes.
        health_callback: Optional async callback for the internal health
            endpoint.  When provided and the request is ``GET /__health``,
            the callback is invoked instead of forwarding through SOCKS5.
        blocked_domains: Optional set of domain names to block.  When set,
            any CONNECT or HTTP request whose target host matches or is a
            subdomain of an entry is rejected with ``403 Forbidden``.
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

        if method != "CONNECT" and target.endswith(HEALTH_PATH) and health_callback is not None:
            # PR-6: restrict health endpoint to loopback clients only to prevent
            # remote callers from probing internal server state / exit IP.
            if not _is_loopback(peer_str):
                client_writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await client_writer.drain()
                log.warning("Health endpoint access denied for non-loopback peer %s", peer_str)
                return
            await _handle_health(client_writer, health_callback)
            return

        # Domain blocking check — runs after health endpoint, before dispatch.
        if blocked_domains is not None:
            if method == "CONNECT":
                check_host = target.rsplit(":", 1)[0].strip("[]")
            else:
                check_host = urlparse(target).hostname or ""

            if _is_domain_blocked(check_host, blocked_domains):
                log.warning(
                    "Blocked request to %s — domain not allowed in this session "
                    "(use --start-url to enable TIDAL browsing)",
                    check_host,
                )
                await send_error(client_writer, 403, "Forbidden")
                return

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
                monitor=monitor,
            )

    except TimeoutError:
        log.warning("Request timeout from %s", peer_str)
        if monitor is not None:
            await monitor.record_error(ConnectionOutcome.TIMEOUT, peer_str, "request timeout")
        await send_error(client_writer, 408, "Request Timeout")
    except TunnelError as exc:
        log.warning("Tunnel error for %s: %s", peer_str, exc)
        if monitor is not None:
            await monitor.record_error(
                ConnectionOutcome.TUNNEL_ERROR, peer_str, str(exc),
            )
        await send_error(client_writer, 502, "Bad Gateway")
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
        await send_error(client_writer, 500, "Internal Server Error")
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
    # Read until we find the empty line marking end of headers.
    # Use bytearray to avoid O(n²) concatenation (E-RL9).
    header_buf = bytearray()
    while b"\r\n\r\n" not in header_buf:
        if len(header_buf) >= _MAX_HEADER_SIZE:
            raise TunnelError(
                f"Request headers exceed maximum size ({_MAX_HEADER_SIZE} bytes)"
            )
        remaining = _MAX_HEADER_SIZE - len(header_buf)
        chunk = await reader.read(min(4096, remaining))
        if not chunk:
            raise TunnelError("Client disconnected before sending complete headers")
        header_buf.extend(chunk)

    # Convert to bytes for slicing/indexing (bytearray supports all the same ops
    # but bytes is safer to pass downstream as it is immutable).
    header_data = bytes(header_buf)

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
    *,
    monitor: ConnectionMonitor | None = None,
) -> None:
    """Handle a plain HTTP request by forwarding through SOCKS5.

    Reads any remaining request body (based on Content-Length), then
    delegates to the forwarder and records the outcome in the monitor.

    Args:
        method: HTTP method.
        url: Absolute URL from the request line.
        http_version: HTTP version string.
        headers: Parsed request headers.
        body_start: Any body bytes already read past the headers.
        upstream: Upstream SOCKS5 connection parameters.
        client_reader: Client stream reader.
        client_writer: Client stream writer.
        monitor: Optional connection quality monitor for recording outcomes.
    """
    # Detect Transfer-Encoding: chunked (E-RL2).
    is_chunked = any(
        name.lower() == "transfer-encoding" and "chunked" in value.lower()
        for name, value in headers
    )

    if is_chunked:
        # Dechunk the body before forwarding.  The upstream is a plain HTTP
        # server reached via SOCKS5, so we forward a Content-Length request.
        try:
            body = await _read_chunked_body(client_reader, body_start, _MAX_BODY_SIZE)
        except TunnelError:
            await send_error(client_writer, 400, "Bad Request")
            return
        if len(body) > _MAX_BODY_SIZE:
            await send_error(client_writer, 413, "Content Too Large")
            return
    else:
        # Read remaining body if Content-Length is set.
        # Use bytearray to avoid O(n²) concatenation for large bodies (E-RL10).
        body_buf = bytearray(body_start)
        content_length = 0
        for name, value in headers:
            if name.lower() == "content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    pass
                break

        if content_length > _MAX_BODY_SIZE:
            log.warning(
                "Request body too large: Content-Length %d exceeds limit %d",
                content_length,
                _MAX_BODY_SIZE,
            )
            await send_error(client_writer, 413, "Content Too Large")
            return

        while content_length > len(body_buf):
            remaining = content_length - len(body_buf)
            extra = await client_reader.read(remaining)
            if not extra:
                break  # EOF — client disconnected before sending full body
            body_buf.extend(extra)

        body = bytes(body_buf)

    forward_start = time.monotonic()
    success = await forward_http_request(
        method=method,
        url=url,
        http_version=http_version,
        headers=headers,
        body=body,
        upstream=upstream,
        client_writer=client_writer,
    )
    forward_latency_ms = (time.monotonic() - forward_start) * 1000

    if monitor is not None:
        if success:
            await monitor.record_success(forward_latency_ms, url)
        else:
            await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, url, "HTTP forward failed")


async def _read_chunked_body(
    reader: asyncio.StreamReader,
    already_read: bytes,
    max_size: int,
) -> bytes:
    """Read and dechunk a Transfer-Encoding: chunked request body (E-RL2).

    Implements basic chunked decoding per RFC 7230 §4.1.  Chunk extensions
    and trailers are ignored (stripped).  Raises ``TunnelError`` on malformed
    chunk framing; returns the assembled body bytes on success.

    Args:
        reader: Client stream reader (positioned after headers).
        already_read: Any body bytes already read past the headers.
        max_size: Maximum total assembled body size.

    Returns:
        Assembled dechunked body bytes.

    Raises:
        TunnelError: If chunk framing is malformed or the body exceeds max_size.
    """
    buf = bytearray(already_read)
    result = bytearray()

    async def _read_line() -> bytes:
        """Read a CRLF-terminated line, pulling from buf or reader as needed."""
        while b"\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                raise TunnelError("Unexpected EOF while reading chunked body")
            buf.extend(chunk)
        idx = buf.index(b"\r\n")
        line = bytes(buf[:idx])
        del buf[: idx + 2]
        return line

    async def _read_exactly(n: int) -> bytes:
        """Read exactly n bytes, pulling from buf or reader as needed."""
        while len(buf) < n:
            chunk = await reader.read(4096)
            if not chunk:
                raise TunnelError("Unexpected EOF while reading chunk data")
            buf.extend(chunk)
        data = bytes(buf[:n])
        del buf[:n]
        return data

    while True:
        size_line = await _read_line()
        # Strip chunk extensions (e.g. "1a; ext=value" -> "1a")
        size_str = size_line.split(b";", 1)[0].strip()
        try:
            chunk_size = int(size_str, 16)
        except ValueError as exc:
            raise TunnelError(f"Malformed chunk size: {size_str!r}") from exc

        if chunk_size == 0:
            # Consume optional trailer headers + terminating CRLF
            # (RFC 7230 §4.1). _read_line() strips \r\n; an empty
            # result signals the final blank line (F-RL10).
            # Cap at 100 trailer lines to prevent unbounded reads (J-RL6).
            _MAX_TRAILER_LINES = 100
            for _ in range(_MAX_TRAILER_LINES):
                trailer = await _read_line()
                if not trailer:
                    break
            else:
                raise TunnelError(
                    f"Too many trailer lines (>{_MAX_TRAILER_LINES})"
                )
            break

        if len(result) + chunk_size > max_size:
            raise TunnelError(
                f"Chunked body exceeds maximum size ({max_size} bytes)"
            )

        data = await _read_exactly(chunk_size)
        result.extend(data)

        # Each chunk is followed by CRLF
        crlf = await _read_exactly(2)
        if crlf != b"\r\n":
            raise TunnelError(f"Expected CRLF after chunk data, got {crlf!r}")

    return bytes(result)


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


async def _handle_health(
    client_writer: asyncio.StreamWriter,
    health_callback: Callable[[], Awaitable[tuple[bool, str]]],
) -> None:
    """Handle an internal health check request.

    Invokes the server's health callback (which may rotate the upstream
    on failure) and returns a JSON response to the client.

    Args:
        client_writer: Writer to send the response back to the client.
        health_callback: Async callable returning (ok, body).
    """
    import json

    log.debug("Handling internal health check request")

    ok, body = await health_callback()
    status = 200 if ok else 503
    reason = "OK" if ok else "Service Unavailable"
    payload = json.dumps({"ok": ok, "exit_ip" if ok else "error": body})

    payload_bytes = payload.encode("utf-8")
    response_head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1")
    try:
        client_writer.write(response_head + payload_bytes)
        await client_writer.drain()
    except OSError:
        pass


