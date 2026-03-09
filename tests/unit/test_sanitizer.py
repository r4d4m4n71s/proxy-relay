"""Tests for proxy_relay.sanitizer — header sanitization."""
from __future__ import annotations

import pytest


class TestSanitizeHeaders:
    """Test sanitize_headers identity-leak removal."""

    def test_strip_x_forwarded_for(self):
        from proxy_relay.sanitizer import sanitize_headers

        headers = [("X-Forwarded-For", "1.2.3.4"), ("Host", "example.com")]
        result = sanitize_headers(headers)
        names = [h[0] for h in result]
        assert "X-Forwarded-For" not in names
        assert "Host" in names

    @pytest.mark.parametrize(
        "header_name",
        [
            pytest.param("VIA", id="uppercase"),
            pytest.param("via", id="lowercase"),
            pytest.param("Via", id="titlecase"),
            pytest.param("vIa", id="mixedcase"),
        ],
    )
    def test_strip_via_case_insensitive(self, header_name):
        from proxy_relay.sanitizer import sanitize_headers

        headers = [(header_name, "proxy/1.0"), ("Host", "example.com")]
        result = sanitize_headers(headers)
        names = [h[0].lower() for h in result]
        assert "via" not in names

    def test_preserve_non_leak_headers(self):
        from proxy_relay.sanitizer import sanitize_headers

        headers = [
            ("Host", "example.com"),
            ("User-Agent", "Mozilla/5.0"),
            ("Accept", "text/html"),
        ]
        result = sanitize_headers(headers)
        assert len(result) == 3
        assert result == headers

    def test_strip_all_leak_headers(self):
        from proxy_relay.sanitizer import _STRIP_HEADERS, sanitize_headers

        headers = [(name, "value") for name in _STRIP_HEADERS]
        result = sanitize_headers(headers)
        assert result == []

    def test_empty_headers(self):
        from proxy_relay.sanitizer import sanitize_headers

        result = sanitize_headers([])
        assert result == []

    def test_strip_x_proxy_id(self):
        from proxy_relay.sanitizer import sanitize_headers

        headers = [("X-Proxy-Id", "abc123"), ("Host", "example.com")]
        result = sanitize_headers(headers)
        names = [h[0] for h in result]
        assert "X-Proxy-Id" not in names

    def test_strip_forwarded(self):
        from proxy_relay.sanitizer import sanitize_headers

        headers = [("Forwarded", "for=1.2.3.4"), ("Host", "example.com")]
        result = sanitize_headers(headers)
        names = [h[0] for h in result]
        assert "Forwarded" not in names

    def test_hop_by_hop_headers_stripped(self):
        """Hop-by-hop headers like Connection are also stripped."""
        from proxy_relay.sanitizer import sanitize_headers

        headers = [
            ("Host", "example.com"),
            ("Connection", "keep-alive"),
            ("Keep-Alive", "timeout=5"),
            ("Transfer-Encoding", "chunked"),
        ]
        result = sanitize_headers(headers)
        names = [h[0] for h in result]
        assert "Host" in names
        assert "Connection" not in names
        assert "Keep-Alive" not in names
        assert "Transfer-Encoding" not in names


class TestIsLeakyHeader:
    """Test is_leaky_header helper."""

    def test_leak_header_detected(self):
        from proxy_relay.sanitizer import is_leaky_header

        assert is_leaky_header("X-Forwarded-For") is True
        assert is_leaky_header("Via") is True
        assert is_leaky_header("Forwarded") is True

    def test_safe_header_passes(self):
        from proxy_relay.sanitizer import is_leaky_header

        assert is_leaky_header("Host") is False
        assert is_leaky_header("User-Agent") is False
        assert is_leaky_header("Accept") is False

    def test_case_insensitive(self):
        from proxy_relay.sanitizer import is_leaky_header

        assert is_leaky_header("x-forwarded-for") is True
        assert is_leaky_header("X-FORWARDED-FOR") is True
