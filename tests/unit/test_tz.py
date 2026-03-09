"""Tests for proxy_relay.tz — timezone-to-country mismatch detection."""
from __future__ import annotations

from unittest.mock import patch

import pytest


class TestCheckTimezoneMismatch:
    """Test check_timezone_mismatch for proxy country vs system TZ."""

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=-5.0)
    def test_us_with_eastern_tz_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("US") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=-6.0)
    def test_us_with_central_tz_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("US") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=-5.0)
    def test_us_with_bogota_equivalent_matches_us_range(self, _mock):
        """UTC-5 is valid for US (Eastern) so no mismatch."""
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("US") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=1.0)
    def test_us_with_cet_tz_is_mismatch(self, _mock):
        """UTC+1 (CET) is NOT valid for US — mismatch."""
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("US") is True

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=0.0)
    def test_gb_with_gmt_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("GB") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=9.0)
    def test_unknown_country_returns_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        # Unknown country code — not in lookup table, returns False
        assert check_timezone_mismatch("ZZ") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=0.0)
    def test_empty_country_returns_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=1.0)
    def test_de_with_cet_no_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("DE") is False

    @patch("proxy_relay.tz.get_local_utc_offset_hours", return_value=-5.0)
    def test_de_with_eastern_is_mismatch(self, _mock):
        from proxy_relay.tz import check_timezone_mismatch

        assert check_timezone_mismatch("DE") is True
