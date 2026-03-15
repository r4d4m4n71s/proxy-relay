"""Tests for proxy_relay.cli — --capture and --capture-domains flags on 'browse'."""
from __future__ import annotations

import pytest


class TestBrowseParserCaptureFlags:
    """Verify the 'browse' subcommand exposes --capture and --capture-domains."""

    @pytest.fixture
    def parser(self):
        from proxy_relay.cli import build_parser

        return build_parser()

    def test_browse_parser_has_capture_flag(self, parser):
        """parse_args(['browse', '--capture']) must succeed."""
        args = parser.parse_args(["browse", "--capture"])
        assert args.capture is True

    def test_browse_parser_capture_flag_default_false(self, parser):
        """Without --capture, the flag defaults to False."""
        args = parser.parse_args(["browse"])
        assert args.capture is False

    def test_browse_parser_has_capture_domains_flag(self, parser):
        """--capture-domains accepts a comma-separated domain string."""
        args = parser.parse_args([
            "browse", "--capture", "--capture-domains", "tidal.com,qobuz.com"
        ])
        assert args.capture_domains == "tidal.com,qobuz.com"

    def test_capture_domains_parsed_as_string(self, parser):
        """--capture-domains stores a raw string (splitting is done downstream)."""
        args = parser.parse_args([
            "browse", "--capture", "--capture-domains", "tidal.com"
        ])
        assert isinstance(args.capture_domains, str)
        assert args.capture_domains == "tidal.com"

    def test_capture_domains_default_is_none_or_empty(self, parser):
        """Without --capture-domains, the value is None or empty string."""
        args = parser.parse_args(["browse"])
        assert args.capture_domains is None or args.capture_domains == ""

    def test_capture_flag_does_not_break_other_browse_args(self, parser):
        """--capture can coexist with other browse flags like --profile."""
        args = parser.parse_args([
            "browse", "--capture", "--profile", "us-browse", "--no-rotate"
        ])
        assert args.capture is True
        assert args.profile == "us-browse"
        assert args.no_rotate is True

    def test_capture_domains_without_capture_flag(self, parser):
        """--capture-domains can be parsed even without --capture (validation is downstream)."""
        args = parser.parse_args(["browse", "--capture-domains", "tidal.com"])
        assert args.capture_domains == "tidal.com"
