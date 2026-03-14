"""Plain HTTP request forwarding via upstream SOCKS5 proxy.

Handles non-CONNECT HTTP requests (GET, POST, etc.) by parsing the
request, sanitizing headers, forwarding through the SOCKS5 upstream,
and relaying the response back to the client.

DNS leak prevention: target hostnames are passed as strings to the
SOCKS5 connector — NEVER resolved locally.
"""
from __future__ import annotations

import asyncio
import time

from proxy_relay.exceptions import TunnelError
from proxy_relay.logger import get_logger
from proxy_relay.sanitizer import sanitize_headers
from proxy_relay.tunnel import open_tunnel
from proxy_relay.upstream import UpstreamInfo

log = get_logger(__name__)

# Chunk size for streaming the upstream response to the client (8 KiB)
_STREAM_CHUNK_SIZE: int = 8192

# Read timeout for the upstream response (seconds)
_RESPONSE_TIMEOUT: float = 60.0


async def forward_http_request(
    method: str,
    url: str,
    http_version: str,
    headers: list[tuple[str, str]],
    body: bytes,
    upstream: UpstreamInfo,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Forward a plain HTTP request through the upstream SOCKS5 proxy.

    Parses the absolute URL to extract the target host and port, opens a
    SOCKS5 tunnel, sends the sanitized request, and relays the response
    back to the client.

    Args:
        method: HTTP method (GET, POST, etc.).
        url: Absolute URL from the request line (e.g., "http://example.com/path").
        http_version: HTTP version string (e.g., "HTTP/1.1").
        headers: Raw request headers as (name, value) tuples.
        body: Request body bytes (may be empty).
        upstream: Parsed upstream SOCKS5 connection parameters.
        client_writer: Writer to send the response back to the client.

    Raises:
        TunnelError: If the upstream connection fails.
    """
    # Parse the absolute URL to extract host, port, and path
    host, port, path = _parse_absolute_url(url)

    log.info("Forwarding %s %s via SOCKS5", method, url)

    # Sanitize headers before forwarding
    safe_headers = sanitize_headers(headers)

    # Ensure Host header is present
    has_host = any(h[0].lower() == "host" for h in safe_headers)
    if not has_host:
        host_value = host if port == 80 else f"{host}:{port}"
        safe_headers.insert(0, ("Host", host_value))

    # Build the request with a relative path (not absolute URL)
    request_line = f"{method} {path} {http_version}\r\n"
    header_lines = "".join(f"{name}: {value}\r\n" for name, value in safe_headers)
    raw_request = f"{request_line}{header_lines}\r\n".encode("latin-1") + body

    start = time.monotonic()

    # Open tunnel to the target via SOCKS5
    remote_reader: asyncio.StreamReader | None = None
    remote_writer: asyncio.StreamWriter | None = None
    try:
        result = await open_tunnel(host, port, upstream)
        remote_reader = result.reader
        remote_writer = result.writer

        # Send the request
        remote_writer.write(raw_request)
        await remote_writer.drain()

        # Stream the response back to the client in chunks
        total_bytes = 0
        while True:
            chunk = await asyncio.wait_for(
                remote_reader.read(_STREAM_CHUNK_SIZE),
                timeout=_RESPONSE_TIMEOUT,
            )
            if not chunk:
                break
            client_writer.write(chunk)
            await client_writer.drain()
            total_bytes += len(chunk)

        elapsed_ms = (time.monotonic() - start) * 1000
        log.info(
            "Forwarded %s %s -> %d bytes (%.0fms)",
            method,
            path,
            total_bytes,
            elapsed_ms,
        )
    except TunnelError:
        raise
    except asyncio.TimeoutError:
        log.warning("Response timeout for %s %s", method, url)
        await _send_error_response(client_writer, 504, "Gateway Timeout")
    except Exception as exc:
        log.warning("Forward error for %s %s: %s", method, url, exc)
        await _send_error_response(client_writer, 502, "Bad Gateway")
    finally:
        if remote_writer is not None:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except OSError:
                pass


def _parse_absolute_url(url: str) -> tuple[str, int, str]:
    """Parse an absolute HTTP URL into (host, port, path).

    Args:
        url: Absolute URL (e.g., "http://example.com:8080/path?query").

    Returns:
        Tuple of (hostname, port, path_with_query).

    Raises:
        TunnelError: If the URL cannot be parsed.
    """
    # Strip the scheme
    if url.startswith("http://"):
        rest = url[7:]
        default_port = 80
    elif url.startswith("https://"):
        rest = url[8:]
        default_port = 443
    else:
        raise TunnelError(f"Unsupported URL scheme in: {url!r}")

    # Split host from path
    slash_idx = rest.find("/")
    if slash_idx == -1:
        host_part = rest
        path = "/"
    else:
        host_part = rest[:slash_idx]
        path = rest[slash_idx:]

    # Extract port from host
    if ":" in host_part:
        host, port_str = host_part.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError as exc:
            raise TunnelError(f"Invalid port in URL: {url!r}") from exc
    else:
        host = host_part
        port = default_port

    if not host:
        raise TunnelError(f"Empty host in URL: {url!r}")

    return host, port, path


async def _send_error_response(
    writer: asyncio.StreamWriter,
    status_code: int,
    reason: str,
) -> None:
    """Send a minimal HTTP error response to the client.

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
