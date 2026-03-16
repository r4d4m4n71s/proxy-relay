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
import urllib.parse

from proxy_relay.exceptions import TunnelError
from proxy_relay.logger import get_logger
from proxy_relay.response import send_error
from proxy_relay.sanitizer import sanitize_headers
from proxy_relay.tunnel import open_tunnel
from proxy_relay.upstream import UpstreamInfo

log = get_logger(__name__)

# Chunk size for streaming the upstream response to the client (8 KiB)
_STREAM_CHUNK_SIZE: int = 8192

# Read timeout for the upstream response (seconds)
_RESPONSE_TIMEOUT: float = 60.0

# Maximum total response size for plain HTTP forwarding (100 MiB).
# CONNECT tunnel streaming is NOT capped (opaque byte relay).
_MAX_RESPONSE_SIZE: int = 100 * 1024 * 1024


async def forward_http_request(
    method: str,
    url: str,
    http_version: str,
    headers: list[tuple[str, str]],
    body: bytes,
    upstream: UpstreamInfo,
    client_writer: asyncio.StreamWriter,
) -> bool:
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

    Returns:
        True if the request was forwarded and a response received successfully,
        False if any forwarding error occurred (timeout, tunnel failure, etc.).

    Raises:
        TunnelError: If the upstream connection fails.
    """
    # Parse the absolute URL to extract host, port, and path
    host, port, path = _parse_absolute_url(url)

    log.debug("Forwarding request: method=%s host=%s port=%d", method, host, port)
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
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESPONSE_SIZE:
                log.warning(
                    "Response for %s %s exceeds %d bytes — aborting",
                    method, url, _MAX_RESPONSE_SIZE,
                )
                await send_error(client_writer, 502, "Bad Gateway")
                return False
            client_writer.write(chunk)
            await client_writer.drain()

        elapsed_ms = (time.monotonic() - start) * 1000
        log.info(
            "Forwarded %s %s -> %d bytes (%.0fms)",
            method,
            path,
            total_bytes,
            elapsed_ms,
        )
        return True
    except TunnelError:
        raise
    except asyncio.TimeoutError:
        log.warning("Response timeout for %s %s", method, url)
        await send_error(client_writer, 504, "Gateway Timeout")
        return False
    except Exception as exc:
        log.warning("Forward error for %s %s: %s", method, url, exc)
        await send_error(client_writer, 502, "Bad Gateway")
        return False
    finally:
        if remote_writer is not None:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except OSError:
                pass


def _parse_absolute_url(url: str) -> tuple[str, int, str]:
    """Parse an absolute HTTP URL into (host, port, path).

    Uses ``urllib.parse.urlparse`` to correctly handle URLs that contain
    userinfo (``user:pass@host``), IPv6 addresses (``[::1]:8080``), and
    other edge cases that confuse manual ``":"`` splitting.

    Args:
        url: Absolute URL (e.g., "http://example.com:8080/path?query").

    Returns:
        Tuple of (hostname, port, path_with_query).

    Raises:
        TunnelError: If the URL cannot be parsed or has an unsupported scheme.
    """
    parsed = urllib.parse.urlparse(url)

    scheme = parsed.scheme.lower()
    if scheme == "http":
        default_port = 80
    elif scheme == "https":
        default_port = 443
    else:
        raise TunnelError(f"Unsupported URL scheme in: {url!r}")

    host = parsed.hostname
    if not host:
        raise TunnelError(f"Empty host in URL: {url!r}")

    port = parsed.port if parsed.port is not None else default_port

    # Reconstruct the path+query+fragment as the relative path to forward
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if parsed.fragment:
        path = f"{path}#{parsed.fragment}"

    return host, port, path


