"""Tests for proxy_relay.capture.collector — CaptureCollector."""
from __future__ import annotations

import hashlib
import time

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def enqueue_fn():
    """A simple call-recording enqueue function."""
    calls: list[tuple[str, dict]] = []

    def _enqueue(event_type: str, payload: dict) -> None:
        calls.append((event_type, payload))

    _enqueue.calls = calls  # type: ignore[attr-defined]
    return _enqueue


@pytest.fixture
def default_collector(enqueue_fn):
    """CaptureCollector with default TIDAL/Qobuz domains."""
    from proxy_relay.capture.collector import CaptureCollector
    from proxy_relay.capture.models import CaptureConfig

    cfg = CaptureConfig()
    return CaptureCollector(enqueue_fn=enqueue_fn, config=cfg)


@pytest.fixture
def custom_collector(enqueue_fn):
    """CaptureCollector with a single custom domain."""
    from proxy_relay.capture.collector import CaptureCollector
    from proxy_relay.capture.models import CaptureConfig

    cfg = CaptureConfig(domains=frozenset({"example.com"}))
    return CaptureCollector(enqueue_fn=enqueue_fn, config=cfg)


def _make_request_params(url: str, method: str = "GET", headers: dict | None = None,
                          body: str | None = None) -> dict:
    """Build a minimal Network.requestWillBeSent params dict."""
    return {
        "requestId": "req-001",
        "timestamp": time.time(),
        "request": {
            "url": url,
            "method": method,
            "headers": headers or {},
            "postData": body,
            "initialPriority": "High",
        },
        "initiator": {"type": "script"},
        "type": "XHR",
    }


def _make_response_params(url: str, status: int = 200, headers: dict | None = None,
                           request_id: str = "req-001") -> dict:
    """Build a minimal Network.responseReceived params dict."""
    return {
        "requestId": request_id,
        "timestamp": time.time(),
        "response": {
            "url": url,
            "status": status,
            "headers": headers or {},
            "mimeType": "application/json",
        },
    }


# ---------------------------------------------------------------------------
# 1. Domain matching
# ---------------------------------------------------------------------------


class TestDomainMatching:
    """Verify the domain allowlist filter — subdomain matching, exact matches, rejections."""

    def test_matches_tidal_com(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1

    def test_matches_listen_tidal(self, default_collector, enqueue_fn):
        params = _make_request_params("https://listen.tidal.com/album/123")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1

    def test_matches_qobuz(self, default_collector, enqueue_fn):
        params = _make_request_params("https://www.qobuz.com/api")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1

    def test_rejects_unrelated_domain(self, default_collector, enqueue_fn):
        params = _make_request_params("https://google.com/search")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 0

    def test_rejects_suffix_trick(self, default_collector, enqueue_fn):
        """'nottidal.com' must not match the 'tidal.com' allowlist entry."""
        params = _make_request_params("https://nottidal.com/path")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 0

    def test_matches_exact_domain(self, default_collector, enqueue_fn):
        params = _make_request_params("https://tidal.com/")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1

    def test_empty_url_does_not_enqueue(self, default_collector, enqueue_fn):
        params = _make_request_params("")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 0

    def test_custom_domains_matches_subdomain(self, custom_collector, enqueue_fn):
        """Custom domain 'example.com' should match 'api.example.com'."""
        params = _make_request_params("https://api.example.com/data")
        custom_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1

    def test_custom_domains_rejects_default_domains(self, custom_collector, enqueue_fn):
        """Collector with custom domains should NOT match default tidal.com."""
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        custom_collector.on_request(params)
        assert len(enqueue_fn.calls) == 0


# ---------------------------------------------------------------------------
# 2. Header redaction
# ---------------------------------------------------------------------------


class TestHeaderRedaction:
    """Verify sensitive headers are redacted; safe headers are preserved."""

    def test_redact_authorization_header(self, default_collector):
        raw = "Bearer abc123xyz_long_token_value"
        result = default_collector._redact_headers({"Authorization": raw})
        redacted_value = result["Authorization"]
        assert "abc123xyz" not in redacted_value, "Token must be truncated"
        assert "..." in redacted_value or len(redacted_value) < len(raw)

    def test_redact_authorization_case_insensitive_upper(self, default_collector):
        raw = "Bearer somesecrettoken"
        result = default_collector._redact_headers({"AUTHORIZATION": raw})
        # Must find a redacted variant under some key
        values = list(result.values())
        assert any("somesecrettoken" not in v for v in values), (
            "AUTHORIZATION header must be redacted regardless of case"
        )

    def test_redact_authorization_case_insensitive_mixed(self, default_collector):
        raw = "Bearer somesecrettoken"
        result = default_collector._redact_headers({"Authorization": raw})
        values = list(result.values())
        assert not any(v == raw for v in values), "Authorization must be redacted"

    def test_redact_preserves_content_type(self, default_collector):
        headers = {"Content-Type": "application/json", "Accept": "text/html"}
        result = default_collector._redact_headers(headers)
        assert result.get("Content-Type") == "application/json"
        assert result.get("Accept") == "text/html"

    def test_redact_short_value_gets_ellipsis(self, default_collector):
        """Values shorter than the truncation threshold still get '...' appended."""
        raw = "short"
        result = default_collector._redact_headers({"Authorization": raw})
        value = list(result.values())[0]
        assert "..." in value

    def test_redact_cookie_header(self, default_collector):
        raw = "session=abc123; tracker=xyz789"
        result = default_collector._redact_headers({"Cookie": raw})
        assert result.get("Cookie") != raw, "Cookie header must be redacted"

    def test_redact_empty_headers(self, default_collector):
        result = default_collector._redact_headers({})
        assert result == {}


# ---------------------------------------------------------------------------
# 3. Cookie hashing
# ---------------------------------------------------------------------------


class TestCookieHashing:
    """Verify deterministic SHA-256 hashing for sensitive cookie values."""

    def test_hash_cookie_value_is_deterministic(self, default_collector):
        h1 = default_collector._hash_value("session_token_abc123")
        h2 = default_collector._hash_value("session_token_abc123")
        assert h1 == h2

    def test_hash_different_values_produce_different_hashes(self, default_collector):
        h1 = default_collector._hash_value("value_one")
        h2 = default_collector._hash_value("value_two")
        assert h1 != h2

    def test_hash_matches_sha256(self, default_collector):
        value = "test_cookie_value"
        expected = hashlib.sha256(value.encode()).hexdigest()
        result = default_collector._hash_value(value)
        assert result == expected


# ---------------------------------------------------------------------------
# 4. on_request
# ---------------------------------------------------------------------------


class TestOnRequest:
    """Verify on_request filters, transforms, and enqueues correctly."""

    def test_on_request_enqueues_for_matching_domain(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 1
        event_type, payload = enqueue_fn.calls[0]
        assert event_type.startswith("http.request.")

    def test_on_request_skips_non_matching_domain(self, default_collector, enqueue_fn):
        params = _make_request_params("https://ads.doubleclick.net/pixel")
        default_collector.on_request(params)
        assert len(enqueue_fn.calls) == 0

    def test_on_request_redacts_authorization_header(self, default_collector, enqueue_fn):
        headers = {"Authorization": "Bearer secret_token_12345"}
        params = _make_request_params(
            "https://api.tidal.com/v1/tracks", headers=headers
        )
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        headers_stored = payload.get("headers") or payload.get("headers_json", "")
        if isinstance(headers_stored, dict):
            for v in headers_stored.values():
                assert "secret_token_12345" not in str(v), "Auth token must be redacted"
        else:
            assert "secret_token_12345" not in str(headers_stored), "Auth token must be redacted"

    def test_on_request_extracts_url(self, default_collector, enqueue_fn):
        url = "https://api.tidal.com/v1/albums/12345"
        params = _make_request_params(url)
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        assert payload.get("url") == url

    def test_on_request_extracts_method(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks", method="POST")
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        assert payload.get("method") == "POST"

    def test_on_request_extracts_domain(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        assert "tidal.com" in str(payload.get("domain", ""))

    def test_on_request_extracts_path(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks/9876")
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        assert "/v1/tracks/9876" in str(payload.get("path", ""))

    def test_on_request_extracts_initiator(self, default_collector, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        params["initiator"] = {"type": "script"}
        default_collector.on_request(params)
        _, payload = enqueue_fn.calls[0]
        assert "initiator" in payload or "initiator_type" in payload

    def test_on_request_stores_timestamp_for_timing(self, default_collector):
        """on_request must store the request timestamp so on_response can compute response_ms."""
        request_id = "timing-req-001"
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        params["requestId"] = request_id
        params["timestamp"] = 1000.0

        default_collector.on_request(params)

        # Internal state must remember the timestamp
        assert default_collector._request_times.get(request_id) is not None


# ---------------------------------------------------------------------------
# 5. on_response
# ---------------------------------------------------------------------------


class TestOnResponse:
    """Verify on_response filters, truncates, and calculates timing."""

    def test_on_response_enqueues_for_matching_domain(self, default_collector, enqueue_fn):
        # Seed request timestamp first
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = "resp-req-001"
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        params = _make_response_params("https://api.tidal.com/v1/tracks", request_id="resp-req-001")
        default_collector.on_response(params, body='{"data": []}')
        assert len(enqueue_fn.calls) == 1
        event_type, _ = enqueue_fn.calls[0]
        assert event_type.startswith("http.response.")

    def test_on_response_truncates_large_body(self, default_collector, enqueue_fn):
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = "trunc-req"
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        large_body = "x" * 200_000  # 200KB — well over 64KB limit
        params = _make_response_params("https://api.tidal.com/v1/tracks", request_id="trunc-req")
        default_collector.on_response(params, body=large_body)

        _, payload = enqueue_fn.calls[0]
        body_stored = payload.get("body_preview") or payload.get("body", "")
        assert body_stored is None or len(str(body_stored)) <= 65_536 + 100, (
            "Body must be truncated to max_body_bytes (64KB)"
        )

    def test_on_response_body_under_limit_unchanged(self, default_collector, enqueue_fn):
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = "small-req"
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        small_body = '{"id": 123, "title": "Test Track"}'
        params = _make_response_params("https://api.tidal.com/v1/tracks", request_id="small-req")
        default_collector.on_response(params, body=small_body)

        _, payload = enqueue_fn.calls[0]
        body_stored = payload.get("body_preview") or payload.get("body", "")
        assert body_stored == small_body or small_body in str(body_stored)

    def test_on_response_calculates_response_ms(self, default_collector, enqueue_fn):
        """response_ms must be derived from request timestamp."""
        request_id = "timing-resp-001"
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = request_id
        params_req["timestamp"] = 1000.0
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        params = _make_response_params("https://api.tidal.com/v1/tracks", request_id=request_id)
        params["timestamp"] = 1000.250  # 250ms later (CDP timestamps are in seconds)
        default_collector.on_response(params, body=None)

        _, payload = enqueue_fn.calls[0]
        response_ms = payload.get("response_ms")
        assert response_ms is not None, "response_ms must be present in payload"
        assert response_ms >= 0, "response_ms must be non-negative"

    def test_on_response_none_body_does_not_crash(self, default_collector, enqueue_fn):
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = "null-body-req"
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        params = _make_response_params(
            "https://api.tidal.com/v1/tracks", request_id="null-body-req"
        )
        # Must not raise
        default_collector.on_response(params, body=None)
        assert len(enqueue_fn.calls) == 1

    def test_on_response_redacts_set_cookie(self, default_collector, enqueue_fn):
        params_req = _make_request_params("https://api.tidal.com/v1/tracks")
        params_req["requestId"] = "sc-req"
        default_collector.on_request(params_req)
        enqueue_fn.calls.clear()

        resp_headers = {"Set-Cookie": "session=abc123_secret; HttpOnly; Secure"}
        params = _make_response_params(
            "https://api.tidal.com/v1/tracks",
            headers=resp_headers,
            request_id="sc-req",
        )
        default_collector.on_response(params, body=None)

        _, payload = enqueue_fn.calls[0]
        headers_stored = payload.get("headers") or payload.get("headers_json", "")
        assert "abc123_secret" not in str(headers_stored), "Set-Cookie value must be redacted"


# ---------------------------------------------------------------------------
# 6. on_cookies — diff detection
# ---------------------------------------------------------------------------


class TestOnCookies:
    """Verify on_cookies diffs snapshots and enqueues only changes."""

    def _make_cookie(
        self, name: str, value: str, domain: str = ".tidal.com", http_only: bool = False
    ) -> dict:
        return {
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "expires": -1.0,
            "httpOnly": http_only,
            "secure": True,
            "sameSite": "None",
        }

    def test_on_cookies_first_snapshot_enqueues_all(self, default_collector, enqueue_fn):
        cookies = [
            self._make_cookie("_tid_session", "session_value_abc"),
            self._make_cookie("_tid_user", "user_value_xyz"),
        ]
        default_collector.on_cookies(cookies)
        assert len(enqueue_fn.calls) == 2

    def test_on_cookies_identical_snapshot_enqueues_nothing(self, default_collector, enqueue_fn):
        cookies = [self._make_cookie("_tid_session", "same_value")]
        default_collector.on_cookies(cookies)  # first call
        enqueue_fn.calls.clear()
        default_collector.on_cookies(cookies)  # identical second call
        assert len(enqueue_fn.calls) == 0, "No events for unchanged cookies"

    def test_on_cookies_changed_value_enqueues_change(self, default_collector, enqueue_fn):
        cookies_v1 = [self._make_cookie("_tid_session", "old_value")]
        cookies_v2 = [self._make_cookie("_tid_session", "new_value")]
        default_collector.on_cookies(cookies_v1)
        enqueue_fn.calls.clear()
        default_collector.on_cookies(cookies_v2)
        assert len(enqueue_fn.calls) == 1

    def test_on_cookies_new_cookie_enqueued(self, default_collector, enqueue_fn):
        cookies_v1 = [self._make_cookie("_tid_session", "value1")]
        cookies_v2 = [
            self._make_cookie("_tid_session", "value1"),
            self._make_cookie("_tid_new", "new_value"),
        ]
        default_collector.on_cookies(cookies_v1)
        enqueue_fn.calls.clear()
        default_collector.on_cookies(cookies_v2)
        assert len(enqueue_fn.calls) == 1

    def test_on_cookies_httponly_value_is_hashed(self, default_collector, enqueue_fn):
        """httpOnly=True cookies must store SHA-256 hash, not raw value."""
        raw_value = "secret_httponly_session_token"
        cookies = [self._make_cookie("_tid_session", raw_value, http_only=True)]
        default_collector.on_cookies(cookies)
        _, payload = enqueue_fn.calls[0]
        stored_value = payload.get("value", "")
        assert stored_value != raw_value, "HttpOnly cookie value must not be stored in plaintext"
        expected_hash = hashlib.sha256(raw_value.encode()).hexdigest()
        assert stored_value == expected_hash, "HttpOnly cookie value must be SHA-256 hashed"

    def test_on_cookies_non_httponly_value_preserved(self, default_collector, enqueue_fn):
        """httpOnly=False cookies preserve the raw value."""
        raw_value = "non_sensitive_pref_value"
        cookies = [self._make_cookie("_tid_pref", raw_value, http_only=False)]
        default_collector.on_cookies(cookies)
        _, payload = enqueue_fn.calls[0]
        stored_value = payload.get("value", "")
        assert stored_value == raw_value, "Non-httpOnly cookie value must be stored as-is"

    def test_on_cookies_empty_list(self, default_collector, enqueue_fn):
        default_collector.on_cookies([])
        assert len(enqueue_fn.calls) == 0


# ---------------------------------------------------------------------------
# 7. on_storage — diff detection
# ---------------------------------------------------------------------------


class TestOnStorage:
    """Verify on_storage diffs localStorage/sessionStorage snapshots."""

    def test_on_storage_first_poll_enqueues_all_keys(self, default_collector, enqueue_fn):
        data = {"key1": "value1", "key2": "value2", "key3": "value3"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data)
        assert len(enqueue_fn.calls) == 3

    def test_on_storage_unchanged_enqueues_nothing(self, default_collector, enqueue_fn):
        data = {"key1": "value1"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data)
        enqueue_fn.calls.clear()
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data)
        assert len(enqueue_fn.calls) == 0

    def test_on_storage_changed_key_enqueued(self, default_collector, enqueue_fn):
        data_v1 = {"key1": "old_value"}
        data_v2 = {"key1": "new_value"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v1)
        enqueue_fn.calls.clear()
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v2)
        assert len(enqueue_fn.calls) == 1

    def test_on_storage_new_key_enqueued(self, default_collector, enqueue_fn):
        data_v1 = {"existing_key": "value"}
        data_v2 = {"existing_key": "value", "new_key": "new_value"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v1)
        enqueue_fn.calls.clear()
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v2)
        assert len(enqueue_fn.calls) == 1

    def test_on_storage_removed_key_enqueued(self, default_collector, enqueue_fn):
        """Key present in first snapshot but absent in second must be enqueued as removed."""
        data_v1 = {"old_key": "value", "kept_key": "value"}
        data_v2 = {"kept_key": "value"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v1)
        enqueue_fn.calls.clear()
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data_v2)
        assert len(enqueue_fn.calls) == 1
        _, payload = enqueue_fn.calls[0]
        assert payload.get("key") == "old_key"
        change_type_str = str(payload.get("change_type", ""))
        assert "removed" in change_type_str.lower() or "changed" in change_type_str.lower()

    def test_on_storage_separate_origins_tracked_independently(self, default_collector, enqueue_fn):
        """Storage from different origins does not interfere."""
        data = {"key": "value"}
        default_collector.on_storage("https://listen.tidal.com", "localStorage", data)
        enqueue_fn.calls.clear()
        # Same key from different origin should be treated as a first poll
        default_collector.on_storage("https://qobuz.com", "localStorage", data)
        assert len(enqueue_fn.calls) == 1

    def test_on_storage_empty_data(self, default_collector, enqueue_fn):
        default_collector.on_storage("https://listen.tidal.com", "localStorage", {})
        assert len(enqueue_fn.calls) == 0


# ---------------------------------------------------------------------------
# 8. on_websocket_frame
# ---------------------------------------------------------------------------


class TestOnWebSocketFrame:
    """Verify on_websocket_frame enqueues frame data with correct fields."""

    def _make_ws_params(self, payload: str, request_id: str = "ws-001") -> dict:
        return {
            "requestId": request_id,
            "timestamp": time.time(),
            "response": {
                "opcode": 1,  # text frame
                "payloadData": payload,
                "mask": False,
            },
        }

    def test_websocket_frame_enqueued(self, default_collector, enqueue_fn):
        params = self._make_ws_params('{"type": "heartbeat"}')
        default_collector.on_websocket_frame("received", params)
        assert len(enqueue_fn.calls) == 1
        event_type, _ = enqueue_fn.calls[0]
        assert event_type.startswith("ws.")

    def test_websocket_frame_payload_truncated(self, default_collector, enqueue_fn):
        large_payload = "x" * 200_000
        params = self._make_ws_params(large_payload)
        default_collector.on_websocket_frame("received", params)
        _, payload = enqueue_fn.calls[0]
        preview = payload.get("payload_preview", "")
        assert preview is None or len(str(preview)) <= 65_536 + 100

    def test_websocket_frame_direction_sent(self, default_collector, enqueue_fn):
        params = self._make_ws_params('{"action": "subscribe"}')
        default_collector.on_websocket_frame("sent", params)
        _, payload = enqueue_fn.calls[0]
        assert payload.get("direction") == "sent"

    def test_websocket_frame_direction_received(self, default_collector, enqueue_fn):
        params = self._make_ws_params('{"event": "playback_started"}')
        default_collector.on_websocket_frame("received", params)
        _, payload = enqueue_fn.calls[0]
        assert payload.get("direction") == "received"


# ---------------------------------------------------------------------------
# 9. on_navigation
# ---------------------------------------------------------------------------


class TestOnNavigation:
    """Verify on_navigation filters and enqueues page navigation events."""

    def _make_nav_params(self, url, frame_id="frame-1", transition_type="Navigation"):
        return {
            "frame": {
                "id": frame_id,
                "url": url,
                "mimeType": "text/html",
            },
            "type": transition_type,
        }

    def test_navigation_enqueued_for_matching_domain(self, default_collector, enqueue_fn):
        params = self._make_nav_params("https://listen.tidal.com/album/123")
        default_collector.on_navigation(params)
        assert len(enqueue_fn.calls) == 1
        event_type, _ = enqueue_fn.calls[0]
        assert event_type == "page.navigated"

    def test_navigation_skips_non_matching_domain(self, default_collector, enqueue_fn):
        params = self._make_nav_params("https://google.com/search")
        default_collector.on_navigation(params)
        assert len(enqueue_fn.calls) == 0

    def test_navigation_extracts_frame_id(self, default_collector, enqueue_fn):
        params = self._make_nav_params("https://tidal.com/", frame_id="main-frame")
        default_collector.on_navigation(params)
        _, payload = enqueue_fn.calls[0]
        assert payload["frame_id"] == "main-frame"

    def test_navigation_extracts_transition_type(self, default_collector, enqueue_fn):
        params = self._make_nav_params("https://tidal.com/", transition_type="link")
        default_collector.on_navigation(params)
        _, payload = enqueue_fn.calls[0]
        assert payload["transition_type"] == "link"

    def test_navigation_includes_profile(self, default_collector, enqueue_fn):
        params = self._make_nav_params("https://tidal.com/")
        default_collector.on_navigation(params)
        _, payload = enqueue_fn.calls[0]
        assert "profile" in payload


# ---------------------------------------------------------------------------
# F-RL20 — session_id propagation
# ---------------------------------------------------------------------------


class TestCollectorSessionId:
    """Verify session_id is threaded through all on_* event payloads (F-RL20)."""

    @pytest.fixture
    def enqueue_fn(self):
        calls: list[tuple[str, dict]] = []

        def _enqueue(event_type: str, payload: dict) -> None:
            calls.append((event_type, payload))

        _enqueue.calls = calls  # type: ignore[attr-defined]
        return _enqueue

    @pytest.fixture
    def collector_with_session(self, enqueue_fn):
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(domains=frozenset({"tidal.com", "qobuz.com"}))
        return CaptureCollector(
            enqueue_fn=enqueue_fn,
            config=cfg,
            session_id="test-session-uuid",
        )

    def test_session_id_stored_on_init(self):
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        collector = CaptureCollector(
            enqueue_fn=lambda *a: None,
            config=CaptureConfig(),
            session_id="my-session-id",
        )
        assert collector._session_id == "my-session-id"

    def test_default_session_id_is_empty_string(self):
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        collector = CaptureCollector(
            enqueue_fn=lambda *a: None,
            config=CaptureConfig(),
        )
        assert collector._session_id == ""

    def test_on_request_includes_session_id(self, collector_with_session, enqueue_fn):
        params = _make_request_params("https://api.tidal.com/v1/tracks")
        collector_with_session.on_request(params)
        assert len(enqueue_fn.calls) == 1
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"

    def test_on_response_includes_session_id(self, collector_with_session, enqueue_fn):
        # Seed request timestamp
        req = _make_request_params("https://api.tidal.com/v1/tracks")
        req["requestId"] = "sid-resp-001"
        collector_with_session.on_request(req)
        enqueue_fn.calls.clear()

        resp = _make_response_params("https://api.tidal.com/v1/tracks", request_id="sid-resp-001")
        collector_with_session.on_response(resp, body=None)
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"

    def test_on_cookies_includes_session_id(self, collector_with_session, enqueue_fn):
        cookies = [
            {"name": "session", "domain": "tidal.com", "value": "abc",
             "httpOnly": False, "secure": True, "expires": 0, "path": "/"},
        ]
        collector_with_session.on_cookies(cookies)
        assert len(enqueue_fn.calls) == 1
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"

    def test_on_storage_includes_session_id(self, collector_with_session, enqueue_fn):
        collector_with_session.on_storage("https://tidal.com", "local", {"key1": "val1"})
        assert len(enqueue_fn.calls) == 1
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"

    def test_on_websocket_frame_includes_session_id(self, collector_with_session, enqueue_fn):
        params = {
            "requestId": "ws-001",
            "url": "wss://tidal.com/ws",
            "response": {"payloadData": "hello", "opcode": 1},
        }
        collector_with_session.on_websocket_frame("sent", params)
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"

    def test_on_navigation_includes_session_id(self, collector_with_session, enqueue_fn):
        params = {
            "frame": {"url": "https://tidal.com/", "id": "f1", "mimeType": "text/html"},
            "type": "Navigation",
        }
        collector_with_session.on_navigation(params)
        assert len(enqueue_fn.calls) == 1
        _, payload = enqueue_fn.calls[0]
        assert payload["session_id"] == "test-session-uuid"


# ---------------------------------------------------------------------------
# F-RL28 — POST body redaction
# ---------------------------------------------------------------------------


class TestRedactPostBody:
    """Verify _redact_post_body scrubs sensitive fields (F-RL28)."""

    @pytest.fixture
    def collector(self):
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        return CaptureCollector(enqueue_fn=lambda *a: None, config=cfg)

    # JSON bodies

    def test_json_password_field_redacted(self, collector):
        body = '{"username": "alice", "password": "s3cr3t"}'
        result = collector._redact_post_body(body)
        import json
        data = json.loads(result)
        assert data["password"] == "[REDACTED]"
        assert data["username"] == "alice"

    def test_json_g_recaptcha_redacted(self, collector):
        body = '{"email": "a@b.com", "g-recaptcha-response": "03AGdBq..."}'
        result = collector._redact_post_body(body)
        import json
        data = json.loads(result)
        assert data["g-recaptcha-response"] == "[REDACTED]"

    def test_json_client_secret_redacted(self, collector):
        body = '{"client_id": "abc", "client_secret": "xyz"}'
        result = collector._redact_post_body(body)
        import json
        data = json.loads(result)
        assert data["client_secret"] == "[REDACTED]"
        assert data["client_id"] == "abc"

    def test_json_case_insensitive(self, collector):
        body = '{"Password": "hunter2"}'
        result = collector._redact_post_body(body)
        import json
        data = json.loads(result)
        assert data["Password"] == "[REDACTED]"

    def test_json_no_sensitive_fields_unchanged(self, collector):
        body = '{"track_id": 123, "quality": "HIGH"}'
        assert collector._redact_post_body(body) == body

    def test_json_non_object_unchanged(self, collector):
        body = '["a", "b"]'
        assert collector._redact_post_body(body) == body

    # URL-encoded bodies

    def test_urlencoded_password_redacted(self, collector):
        body = "username=alice&password=s3cr3t&remember=1"
        result = collector._redact_post_body(body)
        from urllib.parse import parse_qs
        params = parse_qs(result)
        assert params["password"] == ["[REDACTED]"]
        assert params["username"] == ["alice"]

    def test_urlencoded_passwd_redacted(self, collector):
        body = "user=bob&passwd=abc123"
        result = collector._redact_post_body(body)
        from urllib.parse import parse_qs
        params = parse_qs(result)
        assert params["passwd"] == ["[REDACTED]"]

    def test_urlencoded_no_sensitive_fields_unchanged(self, collector):
        body = "track_id=99&format=flac"
        assert collector._redact_post_body(body) == body

    # Edge cases

    def test_empty_string_unchanged(self, collector):
        assert collector._redact_post_body("") == ""

    def test_non_parseable_body_unchanged(self, collector):
        body = "binary\x00data\xff"
        assert collector._redact_post_body(body) == body

    def test_custom_redact_fields(self):
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(redact_post_fields=frozenset({"api_key"}))
        c = CaptureCollector(enqueue_fn=lambda *a: None, config=cfg)
        body = '{"api_key": "secret123", "query": "hello"}'
        import json
        result = json.loads(c._redact_post_body(body))
        assert result["api_key"] == "[REDACTED]"
        assert result["query"] == "hello"

    def test_on_request_redacts_password_in_payload(self):
        """on_request() must store a redacted post_data in the emitted payload."""
        calls: list[tuple] = []
        from proxy_relay.capture.collector import CaptureCollector
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(domains=frozenset({"tidal.com"}))
        c = CaptureCollector(
            enqueue_fn=lambda e, p: calls.append((e, p)),
            config=cfg,
        )
        params = {
            "requestId": "r1",
            "request": {
                "url": "https://auth.tidal.com/v1/oauth2/token",
                "method": "POST",
                "headers": {},
                "postData": "username=alice&password=hunter2",
            },
            "initiator": {"type": "script"},
        }
        c.on_request(params)
        assert len(calls) == 1
        _, payload = calls[0]
        import json as _json
        from urllib.parse import parse_qs
        stored = payload["post_data"]
        params_stored = parse_qs(stored)
        assert params_stored.get("password") == ["[REDACTED]"]
        assert params_stored.get("username") == ["alice"]


class TestCaptureConfigRedactPostFieldsDefaults:
    """Verify default redact_post_fields set (F-RL28)."""

    def test_default_includes_password(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig()
        assert "password" in cfg.redact_post_fields

    def test_default_includes_passwd(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig()
        assert "passwd" in cfg.redact_post_fields

    def test_default_includes_g_recaptcha_response(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig()
        assert "g-recaptcha-response" in cfg.redact_post_fields

    def test_default_includes_client_secret(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig()
        assert "client_secret" in cfg.redact_post_fields

    def test_default_is_frozenset(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig()
        assert isinstance(cfg.redact_post_fields, frozenset)

    def test_custom_redact_post_fields(self):
        from proxy_relay.capture.models import CaptureConfig
        cfg = CaptureConfig(redact_post_fields=frozenset({"api_key"}))
        assert cfg.redact_post_fields == frozenset({"api_key"})


# ---------------------------------------------------------------------------
# J-RL14 — UTF-8 boundary-aware truncation
# ---------------------------------------------------------------------------


class TestTruncateUtf8Boundary:
    """Verify _truncate never splits a multi-byte UTF-8 sequence (J-RL14)."""

    def _truncate(self, text: str, max_bytes: int) -> str:
        from proxy_relay.capture.collector import _truncate
        return _truncate(text, max_bytes)

    def test_ascii_short_string_unchanged(self):
        """Short ASCII strings are returned unchanged."""
        assert self._truncate("hello", 100) == "hello"

    def test_ascii_at_limit_unchanged(self):
        """String whose UTF-8 length equals max_bytes is returned unchanged."""
        text = "a" * 10
        assert self._truncate(text, 10) == text

    def test_ascii_truncation(self):
        """ASCII string truncated at exact byte boundary."""
        result = self._truncate("abcdefgh", 5)
        assert result == "abcde"
        assert len(result.encode("utf-8")) == 5

    def test_empty_string_unchanged(self):
        """Empty string is returned unchanged regardless of max_bytes."""
        assert self._truncate("", 10) == ""
        assert self._truncate("", 0) == ""

    def test_two_byte_char_not_split(self):
        """A 2-byte character straddling the cut point is excluded, not split.

        'é' encodes to ``\\xc3\\xa9`` (2 bytes).  With max_bytes=3 and input
        'aé' (3 bytes total: a + 2), the result should be 'a' (1 byte), not
        a partial 'é' that would be invalid UTF-8.
        """
        # "aé" = 3 bytes: b'a' + b'\xc3\xa9'
        text = "aé"
        assert len(text.encode("utf-8")) == 3
        result = self._truncate(text, 2)
        # Must not contain a broken 'é'
        assert result == "a"
        result.encode("utf-8")  # must not raise

    def test_four_byte_emoji_not_split(self):
        """A 4-byte emoji is dropped entirely rather than split.

        U+1F600 (😀) encodes to 4 bytes.  With max_bytes=5 and input 'ab😀'
        (2 + 4 = 6 bytes), the result should be 'ab' (2 bytes).
        """
        text = "ab\U0001f600"  # 'ab😀'
        assert len(text.encode("utf-8")) == 6
        result = self._truncate(text, 5)
        assert result == "ab"
        result.encode("utf-8")  # must not raise

    def test_result_fits_within_max_bytes(self):
        """The result's UTF-8 encoding never exceeds max_bytes."""
        text = "こんにちは世界"  # Each char is 3 bytes in UTF-8
        for limit in range(1, len(text.encode("utf-8")) + 1):
            result = self._truncate(text, limit)
            assert len(result.encode("utf-8")) <= limit, (
                f"Result exceeds {limit} bytes for limit={limit}"
            )

    def test_multibyte_only_string_truncated_to_empty(self):
        """Truncating to fewer bytes than one character yields an empty string."""
        text = "é"  # 2 bytes
        result = self._truncate(text, 1)
        assert result == ""
        result.encode("utf-8")  # must not raise

    def test_result_is_valid_utf8(self):
        """Result of truncating an emoji-heavy string is always valid UTF-8."""
        text = "🎵🎶🎸🎻🥁" * 10  # Each emoji is 4 bytes
        result = self._truncate(text, 17)
        encoded = result.encode("utf-8")
        # Must decode cleanly without errors
        assert encoded.decode("utf-8") == result
