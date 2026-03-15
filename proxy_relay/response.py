"""Shared HTTP response helpers used by handler and forwarder."""
from __future__ import annotations

import asyncio

from proxy_relay.logger import get_logger

log = get_logger(__name__)


async def send_error(
    writer: asyncio.StreamWriter,
    status_code: int,
    reason: str,
) -> None:
    """Send a minimal HTTP error response to the client.

    Builds a well-formed HTTP/1.1 response with a plain-text body whose
    ``Content-Length`` is computed from the encoded bytes (not the raw
    string length) to avoid off-by-one errors with non-ASCII characters.

    Args:
        writer: Client stream writer.
        status_code: HTTP status code (e.g. 502).
        reason: HTTP reason phrase (e.g. "Bad Gateway").
    """
    body = f"{status_code} {reason}\r\n"
    body_bytes = body.encode("latin-1")
    response_head = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1")
    try:
        writer.write(response_head + body_bytes)
        await writer.drain()
    except OSError:
        pass
