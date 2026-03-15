"""CDP event collector — translates raw CDP events into structured telemetry rows."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from proxy_relay.capture.models import CaptureConfig
from proxy_relay.logger import get_logger

log = get_logger(__name__)


class CaptureCollector:
    """Translate raw CDP events into structured telemetry payloads.

    Handles domain filtering, header redaction, cookie/storage diffing, and
    WebSocket frame capture.  All ``on_*`` methods are called from the
    asyncio event loop (via CDP subscriber callbacks) and must not block.

    Args:
        enqueue_fn: Callable that accepts ``(event_name, payload_dict)`` and
            writes them to the background writer queue.
        config: CaptureConfig instance controlling domain filters and limits.
        profile: proxy-st profile name included in every emitted event.
    """

    def __init__(
        self,
        enqueue_fn: Callable[[str, dict[str, Any]], None],
        config: CaptureConfig,
        profile: str = "",
    ) -> None:
        self._enqueue = enqueue_fn
        self._config = config
        self._profile = profile
        # "domain|name" -> hashed/raw value for cookie diffing
        self._prev_cookies: dict[str, str] = {}
        # "origin|type" -> {key: value}
        self._prev_storage: dict[str, dict[str, str]] = {}
        # requestId -> monotonic timestamp (seconds) for response timing
        self._request_times: dict[str, float] = {}

    # ── Domain filter ─────────────────────────────────────────────────────

    def matches_domain(self, url: str) -> bool:
        """Return True if *url* hostname matches any configured capture domain.

        A configured domain ``"tidal.com"`` matches ``"api.tidal.com"``,
        ``"listen.tidal.com"``, and ``"tidal.com"`` itself, but NOT
        ``"nottidal.com"``.

        Args:
            url: Full URL string to test.

        Returns:
            True when the hostname suffix matches at least one entry in
            ``config.domains``.
        """
        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            return False

        for domain in self._config.domains:
            if hostname == domain or hostname.endswith("." + domain):
                return True
        return False

    # ── CDP event handlers ─────────────────────────────────────────────────

    def on_request(self, params: dict[str, Any]) -> None:
        """Handle ``Network.requestWillBeSent`` events.

        Filters by domain, redacts headers, records the request timestamp for
        later response-time calculation, and enqueues an ``http.request.captured``
        event.

        Args:
            params: Raw CDP event params dict.
        """
        request = params.get("request", {})
        url: str = request.get("url", "")

        if not self.matches_domain(url):
            return

        request_id: str = params.get("requestId", "")
        self._request_times[request_id] = time.monotonic()

        parsed = urlparse(url)
        domain = parsed.hostname or ""
        path = parsed.path or ""
        initiator = params.get("initiator", {})
        initiator_type: str = initiator.get("type", "") if isinstance(initiator, dict) else ""

        headers = self._redact_headers(dict(request.get("headers", {})))

        payload: dict[str, Any] = {
            "request_id": request_id,
            "url": url,
            "domain": domain,
            "path": path,
            "method": request.get("method", ""),
            "headers": _headers_to_str(headers),
            "post_data": _truncate(
                request.get("postData", "") or "",
                self._config.max_body_bytes,
            ),
            "initiator": initiator_type,
            "initiator_type": initiator_type,
            "profile": self._profile,
        }
        self._enqueue("http.request.captured", payload)

    def on_response(self, params: dict[str, Any], body: str | None = None) -> None:
        """Handle ``Network.responseReceived`` events.

        Calculates response latency from the previously stored request
        timestamp, redacts response headers, truncates body, and enqueues an
        ``http.response.captured`` event.

        Args:
            params: Raw CDP event params dict.
            body: Optional decoded response body.
        """
        response = params.get("response", {})
        url: str = response.get("url", "")

        if not self.matches_domain(url):
            return

        request_id: str = params.get("requestId", "")
        start_ts = self._request_times.pop(request_id, None)

        # Also accept timing from CDP timestamps when available
        if start_ts is not None:
            response_ms = int((time.monotonic() - start_ts) * 1000)
        else:
            # Fall back to CDP timing header if available
            cdp_ts = response.get("timing", {}).get("receiveHeadersEnd") if isinstance(
                response.get("timing"), dict
            ) else None
            response_ms = int(cdp_ts) if cdp_ts is not None else 0

        headers = self._redact_headers(dict(response.get("headers", {})))

        body_truncated: str = _truncate(body or "", self._config.max_body_bytes)

        payload: dict[str, Any] = {
            "request_id": request_id,
            "url": url,
            "status": response.get("status", 0),
            "mime_type": response.get("mimeType", ""),
            "headers": _headers_to_str(headers),
            "body": body_truncated,
            "body_preview": body_truncated,
            "response_ms": response_ms,
            "profile": self._profile,
        }
        self._enqueue("http.response.captured", payload)

    def on_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Process a cookie snapshot and emit only new or changed cookies.

        Compares the current set of cookies against the previous snapshot.
        For ``httpOnly`` cookies the actual SHA-256 hash of the value is
        stored so diff detection still works while not logging plaintext.
        The same hash is stored in the event payload.

        Args:
            cookies: List of CDP cookie objects from ``Network.getCookies``.
        """
        current: dict[str, str] = {}

        for cookie in cookies:
            name: str = cookie.get("name", "")
            domain: str = cookie.get("domain", "")
            value: str = cookie.get("value", "")
            http_only: bool = bool(cookie.get("httpOnly", False))

            key = f"{domain}|{name}"
            stored_value = self._hash_value(value) if http_only else value
            current[key] = stored_value

            if self._prev_cookies.get(key) != stored_value:
                payload: dict[str, Any] = {
                    "domain": domain,
                    "name": name,
                    # For httpOnly cookies store the hash; for others the raw value
                    "value": stored_value,
                    "http_only": int(http_only),
                    "secure": int(bool(cookie.get("secure", False))),
                    "expires": cookie.get("expires", 0),
                    "path": cookie.get("path", "/"),
                    "profile": self._profile,
                }
                self._enqueue("cookie.snapshot", payload)

        self._prev_cookies = current

    def on_storage(
        self, origin: str, storage_type: str, data: dict[str, str]
    ) -> None:
        """Process a storage snapshot and emit changed or removed keys.

        Args:
            origin: The origin URL for the storage (e.g. ``"https://tidal.com"``).
            storage_type: ``"local"`` or ``"session"``.
            data: Current ``{key: value}`` mapping for this origin + type.
        """
        state_key = f"{origin}|{storage_type}"
        previous = self._prev_storage.get(state_key, {})

        # Emit changed / added keys
        for key, value in data.items():
            if previous.get(key) != value:
                payload: dict[str, Any] = {
                    "origin": origin,
                    "storage_type": storage_type,
                    "key": key,
                    "value": _truncate(value, self._config.max_body_bytes),
                    "event_type": "changed",
                    "profile": self._profile,
                }
                self._enqueue("storage.changed", payload)

        # Emit removed keys
        for key in previous:
            if key not in data:
                payload = {
                    "origin": origin,
                    "storage_type": storage_type,
                    "key": key,
                    "value": "",
                    "event_type": "removed",
                    "profile": self._profile,
                }
                self._enqueue("storage.removed", payload)

        self._prev_storage[state_key] = dict(data)

    def on_websocket_frame(self, direction: str, params: dict[str, Any]) -> None:
        """Handle ``Network.webSocketFrameSent`` / ``Received`` events.

        Enqueues all frames regardless of URL — WebSocket frames don't always
        carry the originating URL in the event params.

        Args:
            direction: ``"sent"`` or ``"received"``.
            params: Raw CDP event params dict.
        """
        frame: dict[str, Any] = params.get("response", params.get("request", {}))
        payload_data: str = frame.get("payloadData", "") or ""

        payload: dict[str, Any] = {
            "request_id": params.get("requestId", ""),
            "url": params.get("url", ""),
            "direction": direction,
            "payload": _truncate(payload_data, self._config.max_body_bytes),
            "payload_preview": _truncate(payload_data, self._config.max_body_bytes),
            "opcode": frame.get("opcode", 0),
            "profile": self._profile,
        }
        self._enqueue("ws.frame", payload)

    # ── Instance helpers ──────────────────────────────────────────────────

    def _redact_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Redact sensitive header values, preserving the first 10 characters.

        The comparison is case-insensitive (header names are lowercased before
        matching).  Non-redacted headers are returned unchanged.

        Args:
            headers: Raw headers dict.

        Returns:
            New dict with sensitive values replaced by ``"<first10>..."``.
        """
        return CaptureCollector.redact_headers(headers, self._config.redact_headers)

    def _hash_value(self, value: str) -> str:
        """Return the SHA-256 hex digest of a value string.

        Args:
            value: Value to hash.

        Returns:
            64-character hex digest string.
        """
        return CaptureCollector.hash_cookie_value(value)

    # ── Static helpers ────────────────────────────────────────────────────

    @staticmethod
    def redact_headers(
        headers: dict[str, str], redact_names: frozenset[str]
    ) -> dict[str, str]:
        """Redact sensitive header values, preserving the first 10 characters.

        The comparison is case-insensitive (header names are lowercased before
        matching).  Non-redacted headers are returned unchanged.

        Args:
            headers: Raw headers dict.
            redact_names: Lowercase header names to redact.

        Returns:
            New dict with sensitive values replaced by ``"<first10>..."``.
        """
        result: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in redact_names:
                prefix = (value or "")[:10]
                result[name] = f"{prefix}..." if value else ""
            else:
                result[name] = value
        return result

    @staticmethod
    def hash_cookie_value(value: str) -> str:
        """Return the SHA-256 hex digest of a cookie value.

        Used to track whether httpOnly cookie values have changed without
        storing the plaintext value.

        Args:
            value: Cookie value string.

        Returns:
            64-character hex digest string.
        """
        return hashlib.sha256(value.encode()).hexdigest()


# ── Module-level helpers ──────────────────────────────────────────────────


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes.

    The truncation happens on encoded bytes to respect the storage limit, then
    the result is decoded back to a string (losslessly — the slice is on the
    encoded form so no partial multi-byte sequences remain).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _headers_to_str(headers: dict[str, str]) -> str:
    """Serialise a headers dict to a compact ``Name: Value`` multi-line string."""
    return "\n".join(f"{k}: {v}" for k, v in headers.items())
