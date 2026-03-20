"""HTTP header sanitization rules for plain HTTP forwarding.

Strips headers that could leak the client's identity or reveal the
presence of a proxy in the request chain.
"""
from __future__ import annotations

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# Headers that reveal proxy presence or client identity.
# All comparisons are case-insensitive (HTTP headers are case-insensitive per RFC 7230).
_STRIP_HEADERS: frozenset[str] = frozenset({
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "x-proxy-id",
    "forwarded",
    "via",
    "proxy-authorization",
    "proxy-connection",
    # PR-3: additional headers that can leak client identity through CDN/proxy chains
    "x-proxy-connection",
    "client-ip",
    "true-client-ip",
    "cf-connecting-ip",
    "x-cluster-client-ip",
    "x-original-forwarded-for",
    "x-proxyuser-ip",
})

# Headers that should be present but may need normalisation.
_HOP_BY_HOP: frozenset[str] = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


def sanitize_headers(raw_headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove privacy-leaking and hop-by-hop headers from an HTTP request.

    Processes a list of (name, value) header tuples and returns a filtered
    list with dangerous headers removed. Hop-by-hop headers are also stripped
    since they are meant for a single transport-level connection.

    Args:
        raw_headers: List of (header_name, header_value) tuples from the
            parsed HTTP request.

    Returns:
        Filtered list of (header_name, header_value) tuples safe for
        forwarding to the upstream proxy.
    """
    result: list[tuple[str, str]] = []
    stripped_names: list[str] = []

    for name, value in raw_headers:
        lower_name = name.lower()
        if lower_name in _STRIP_HEADERS or lower_name in _HOP_BY_HOP:
            stripped_names.append(name)
            continue
        result.append((name, value))

    if stripped_names:
        log.debug("Stripped %d headers: %s", len(stripped_names), ", ".join(stripped_names))

    return result


def is_leaky_header(name: str) -> bool:
    """Check whether a header name is on the strip list.

    Args:
        name: HTTP header name (case-insensitive).

    Returns:
        True if the header should be stripped from forwarded requests.
    """
    return name.lower() in _STRIP_HEADERS or name.lower() in _HOP_BY_HOP
