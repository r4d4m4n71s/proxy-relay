"""Tests for proxy_relay.capture.models — CaptureConfig frozen dataclass."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestCaptureConfigDefaults:
    """Verify CaptureConfig default values match the contract."""

    def test_default_domains_includes_tidal(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert "tidal.com" in cfg.domains

    def test_default_domains_includes_qobuz(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert "qobuz.com" in cfg.domains

    def test_default_domains_is_frozenset(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert isinstance(cfg.domains, frozenset)

    def test_default_redact_headers_includes_authorization(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert "authorization" in cfg.redact_headers

    def test_default_redact_headers_includes_cookie(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert "cookie" in cfg.redact_headers

    def test_default_redact_headers_includes_set_cookie(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert "set-cookie" in cfg.redact_headers

    def test_default_redact_headers_is_frozenset(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert isinstance(cfg.redact_headers, frozenset)

    def test_default_max_body_bytes(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.max_body_bytes == 65_536

    def test_default_cookie_poll_interval(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.cookie_poll_interval_s == pytest.approx(30.0)

    def test_default_storage_poll_interval(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.storage_poll_interval_s == pytest.approx(60.0)

    def test_default_db_path_is_none_or_path(self):
        """db_path default is None (auto-resolved later) or a Path."""
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.db_path is None or isinstance(cfg.db_path, Path)


class TestCaptureConfigFrozen:
    """Verify CaptureConfig is immutable after construction."""

    def test_cannot_set_domains(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.domains = frozenset({"example.com"})  # type: ignore[misc]

    def test_cannot_set_max_body_bytes(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.max_body_bytes = 1024  # type: ignore[misc]

    def test_cannot_set_redact_headers(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        with pytest.raises((AttributeError, TypeError)):
            cfg.redact_headers = frozenset()  # type: ignore[misc]


class TestCaptureConfigCustomValues:
    """Verify each field can be overridden at construction time."""

    def test_custom_domains(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(domains=frozenset({"example.com", "api.example.com"}))
        assert cfg.domains == frozenset({"example.com", "api.example.com"})

    def test_custom_redact_headers(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(redact_headers=frozenset({"x-custom-token"}))
        assert "x-custom-token" in cfg.redact_headers

    def test_custom_max_body_bytes(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(max_body_bytes=1024)
        assert cfg.max_body_bytes == 1024

    def test_custom_cookie_poll_interval(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(cookie_poll_interval_s=10.0)
        assert cfg.cookie_poll_interval_s == pytest.approx(10.0)

    def test_custom_storage_poll_interval(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(storage_poll_interval_s=120.0)
        assert cfg.storage_poll_interval_s == pytest.approx(120.0)

    def test_custom_db_path(self, tmp_path):
        from proxy_relay.capture.models import CaptureConfig

        db = tmp_path / "capture.db"
        cfg = CaptureConfig(db_path=db)
        assert cfg.db_path == db

    def test_domains_stays_frozenset_after_custom(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(domains=frozenset({"test.com"}))
        assert isinstance(cfg.domains, frozenset)

    def test_redact_headers_stays_frozenset_after_custom(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(redact_headers=frozenset({"x-token"}))
        assert isinstance(cfg.redact_headers, frozenset)


# ---------------------------------------------------------------------------
# is_json_mime
# ---------------------------------------------------------------------------


class TestIsJsonMime:
    """Verify JSON MIME type detection."""

    def test_application_json(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("application/json") is True

    def test_application_json_with_charset(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("application/json; charset=utf-8") is True

    def test_vendor_json(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("application/vnd.api+json") is True

    def test_hal_json(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("application/hal+json") is True

    def test_custom_vendor_plus_json(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("application/vnd.tidal.v1+json") is True

    def test_text_html_rejected(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("text/html") is False

    def test_image_png_rejected(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("image/png") is False

    def test_empty_string(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("") is False

    def test_case_insensitive(self):
        from proxy_relay.capture.models import is_json_mime

        assert is_json_mime("Application/JSON") is True


# ---------------------------------------------------------------------------
# should_capture_body
# ---------------------------------------------------------------------------


class TestShouldCaptureBody:
    """Verify body capture MIME filtering."""

    def test_empty_mime_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("") is True

    def test_json_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("application/json") is True

    def test_vendor_json_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("application/vnd.api+json") is True

    def test_text_plain_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("text/plain") is True

    def test_text_xml_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("text/xml") is True

    def test_text_html_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("text/html") is True

    def test_image_png_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("image/png") is False

    def test_image_jpeg_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("image/jpeg") is False

    def test_audio_mpeg_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("audio/mpeg") is False

    def test_video_mp4_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("video/mp4") is False

    def test_font_woff2_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("font/woff2") is False

    def test_octet_stream_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("application/octet-stream") is False

    def test_wasm_skipped(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("application/wasm") is False

    def test_mime_with_charset_captured(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("application/json; charset=utf-8") is True

    def test_case_insensitive(self):
        from proxy_relay.capture.models import should_capture_body

        assert should_capture_body("Image/PNG") is False
        assert should_capture_body("APPLICATION/JSON") is True


# ---------------------------------------------------------------------------
# F-RL18 / F-RL21 / F-RL23 new CaptureConfig fields
# ---------------------------------------------------------------------------


class TestCaptureConfigReconnectDefaults:
    """Verify CDP reconnect backoff defaults (F-RL18)."""

    def test_default_max_cdp_reconnects(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.max_cdp_reconnects == 50

    def test_default_cdp_reconnect_delay_s(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.cdp_reconnect_delay_s == pytest.approx(2.0)

    def test_default_cdp_reconnect_backoff_factor(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.cdp_reconnect_backoff_factor == pytest.approx(1.5)

    def test_default_cdp_reconnect_max_delay_s(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.cdp_reconnect_max_delay_s == pytest.approx(60.0)

    def test_custom_max_cdp_reconnects(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(max_cdp_reconnects=10)
        assert cfg.max_cdp_reconnects == 10

    def test_custom_cdp_reconnect_delay_s(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(cdp_reconnect_delay_s=5.0)
        assert cfg.cdp_reconnect_delay_s == pytest.approx(5.0)

    def test_custom_backoff_factor(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(cdp_reconnect_backoff_factor=2.0)
        assert cfg.cdp_reconnect_backoff_factor == pytest.approx(2.0)

    def test_custom_max_delay(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(cdp_reconnect_max_delay_s=120.0)
        assert cfg.cdp_reconnect_max_delay_s == pytest.approx(120.0)


class TestCaptureConfigRotationDefaults:
    """Verify DB rotation and purge defaults (F-RL21 / F-RL23)."""

    def test_default_rotate_db_is_true(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.rotate_db is True

    def test_default_max_db_size_mb(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.max_db_size_mb == 500

    def test_default_max_db_age_days(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.max_db_age_days == 30

    def test_can_disable_rotate_db(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(rotate_db=False)
        assert cfg.rotate_db is False

    def test_custom_max_db_size_mb(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(max_db_size_mb=100)
        assert cfg.max_db_size_mb == 100

    def test_custom_max_db_age_days(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(max_db_age_days=7)
        assert cfg.max_db_age_days == 7
