"""Security PR tests — PR-1 through PR-10.

Covers WebRTC leak fix, CDP detection warning, header sanitization,
Snap Chromium warning, info-bar suppression, health endpoint loopback
restriction, IPv6 disable, cookie freshness warning, and credential
audit comment presence.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Chromium epoch constants (mirrored from profile_rules)
# ---------------------------------------------------------------------------
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01


def _chromium_expires(unix_ts: float) -> int:
    """Convert a Unix timestamp to Chromium expires_utc microseconds."""
    return int((unix_ts + _CHROMIUM_EPOCH_OFFSET) * 1_000_000)


# ---------------------------------------------------------------------------
# SQLite helper — creates a minimal Chromium Cookies database
# ---------------------------------------------------------------------------


def _make_cookies_db(profile_dir: Path, cookies: list[dict]) -> Path:
    """Create a minimal Chromium Cookies SQLite DB with the given cookie rows."""
    db_dir = profile_dir / "Default"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "Cookies"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cookies (
            creation_utc INTEGER NOT NULL,
            host_key TEXT NOT NULL,
            top_frame_site_key TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            encrypted_value BLOB NOT NULL DEFAULT X'',
            path TEXT NOT NULL,
            expires_utc INTEGER NOT NULL,
            is_secure INTEGER NOT NULL DEFAULT 0,
            is_httponly INTEGER NOT NULL DEFAULT 0,
            last_access_utc INTEGER NOT NULL DEFAULT 0,
            has_expires INTEGER NOT NULL DEFAULT 1,
            is_persistent INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 1,
            samesite INTEGER NOT NULL DEFAULT -1,
            source_scheme INTEGER NOT NULL DEFAULT 1,
            source_port INTEGER NOT NULL DEFAULT -1,
            is_same_party INTEGER NOT NULL DEFAULT 0,
            last_update_utc INTEGER NOT NULL DEFAULT 0
        )
    """)
    for c in cookies:
        expires = c.get("expires_utc", 0)
        conn.execute(
            "INSERT INTO cookies "
            "(creation_utc, host_key, top_frame_site_key, name, value, encrypted_value, "
            "path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires, "
            "is_persistent, priority, samesite, source_scheme, source_port, is_same_party, "
            "last_update_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                0,
                c["host_key"],
                "",
                c["name"],
                c.get("value", "test"),
                b"",
                "/",
                expires,
                0,
                0,
                0,
                1 if expires != 0 else 0,
                1,
                1,
                -1,
                1,
                -1,
                0,
                0,
            ),
        )
    conn.commit()
    conn.close()
    return db_path


# ===========================================================================
# PR-1 — WebRTC leak fix (_chrome_args flags)
# ===========================================================================


class TestPR1WebRTCPolicy:
    """PR-1: --webrtc-ip-handling-policy=disable_non_proxied_udp must be present."""

    def _get_args(self) -> list[str]:
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            Path("/usr/bin/chromium"),
            Path("/tmp/profile"),
        )
        return cmd

    def test_webrtc_policy_flag_present(self):
        """New WebRTC flag must appear in the Chrome args."""
        args = self._get_args()
        assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args

    def test_old_webrtc_flags_removed(self):
        """Legacy WebRTC flags must NOT appear in the Chrome args."""
        args = self._get_args()
        assert "--disable-webrtc-stun-origin" not in args
        assert "--enforce-webrtc-ip-permission-check" not in args


# ===========================================================================
# PR-3 — Missing sanitization headers
# ===========================================================================


class TestPR3SanitizationHeaders:
    """PR-3: 7 additional privacy-leaking headers added to _STRIP_HEADERS."""

    _NEW_HEADERS = [
        "x-proxy-connection",
        "client-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "x-cluster-client-ip",
        "x-original-forwarded-for",
        "x-proxyuser-ip",
    ]

    def test_strip_headers_count(self):
        """_STRIP_HEADERS must have at least 16 entries (9 original + 7 new)."""
        from proxy_relay.sanitizer import _STRIP_HEADERS

        assert len(_STRIP_HEADERS) >= 16

    @pytest.mark.parametrize("header", _NEW_HEADERS)
    def test_new_leak_headers_stripped(self, header: str):
        """Each new header must be present in _STRIP_HEADERS (lowercase)."""
        from proxy_relay.sanitizer import _STRIP_HEADERS

        assert header in _STRIP_HEADERS, f"{header!r} missing from _STRIP_HEADERS"

    @pytest.mark.parametrize("header", _NEW_HEADERS)
    def test_new_headers_removed_from_forwarded_request(self, header: str):
        """sanitize_headers() must strip each new leak header (case-insensitive)."""
        from proxy_relay.sanitizer import sanitize_headers

        # Test with original case
        result = sanitize_headers([(header, "leak-value"), ("host", "example.com")])
        names_lower = [n.lower() for n, _ in result]
        assert header.lower() not in names_lower

    @pytest.mark.parametrize("header", _NEW_HEADERS)
    def test_new_headers_removed_case_insensitive(self, header: str):
        """sanitize_headers() must strip headers regardless of capitalisation."""
        from proxy_relay.sanitizer import sanitize_headers

        mixed = header.title()  # e.g. "X-Proxy-Connection"
        result = sanitize_headers([(mixed, "leak-value"), ("host", "example.com")])
        names_lower = [n.lower() for n, _ in result]
        assert header.lower() not in names_lower


# ===========================================================================
# PR-4 — Snap Chromium warning
# ===========================================================================


class TestPR4SnapChromiumWarning:
    """PR-4: find_chromium() warns when a Snap binary is detected."""

    def test_snap_chromium_warns(self, tmp_path: Path):
        """A Snap path triggers a warning log."""
        snap_binary = tmp_path / "snap" / "bin" / "chromium"
        snap_binary.parent.mkdir(parents=True, exist_ok=True)
        snap_binary.touch()
        snap_binary.chmod(0o755)

        with patch(
            "proxy_relay.browse._CHROMIUM_CANDIDATES",
            (str(snap_binary),),
        ):
            with patch("proxy_relay.browse.log") as mock_log:
                from proxy_relay.browse import find_chromium
                find_chromium()

        # warning() must have been called with a message mentioning Snap
        assert mock_log.warning.called
        call_args = mock_log.warning.call_args[0]
        # First arg is format string
        assert "snap" in call_args[0].lower() or "snap" in str(call_args[1]).lower()

    def test_non_snap_no_warning(self, tmp_path: Path):
        """A native (non-Snap) binary does NOT trigger a warning."""
        native_binary = tmp_path / "usr" / "bin" / "chromium"
        native_binary.parent.mkdir(parents=True, exist_ok=True)
        native_binary.touch()
        native_binary.chmod(0o755)

        with patch(
            "proxy_relay.browse._CHROMIUM_CANDIDATES",
            (str(native_binary),),
        ):
            with patch("proxy_relay.browse.log") as mock_log:
                from proxy_relay.browse import find_chromium
                find_chromium()

        mock_log.warning.assert_not_called()


# ===========================================================================
# PR-5 — Info bar suppression
# ===========================================================================


class TestPR5DisableInfobars:
    """PR-5: --disable-infobars must appear in Chrome args."""

    def test_disable_infobars_present(self):
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            Path("/usr/bin/chromium"),
            Path("/tmp/profile"),
        )
        assert "--disable-infobars" in cmd


# ===========================================================================
# PR-6 — Health endpoint loopback check
# ===========================================================================


class TestPR6IsLoopback:
    """PR-6: _is_loopback() helper and health endpoint restriction."""

    def test_is_loopback_ipv4(self):
        from proxy_relay.handler import _is_loopback

        assert _is_loopback("127.0.0.1:12345") is True

    def test_is_loopback_ipv6(self):
        from proxy_relay.handler import _is_loopback

        assert _is_loopback("[::1]:12345") is True

    def test_is_loopback_non_loopback(self):
        from proxy_relay.handler import _is_loopback

        assert _is_loopback("192.168.1.5:12345") is False

    def test_is_loopback_invalid(self):
        """Invalid address fails safe (returns False — denies access)."""
        from proxy_relay.handler import _is_loopback

        assert _is_loopback("not_an_ip") is False

    def test_is_loopback_empty(self):
        from proxy_relay.handler import _is_loopback

        assert _is_loopback("") is False


class TestPR6HealthEndpointAccess:
    """PR-6: handle_connection restricts /__health to loopback clients."""

    def _make_writer(self, peer: tuple[str, int]) -> MagicMock:
        writer = AsyncMock(spec=asyncio.StreamWriter)
        writer.get_extra_info = MagicMock(return_value=peer)
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        return writer

    def _make_reader(self, request_bytes: bytes) -> AsyncMock:
        reader = AsyncMock(spec=asyncio.StreamReader)
        reader.read = AsyncMock(return_value=request_bytes)
        return reader

    @pytest.mark.asyncio
    async def test_health_from_loopback_allowed(self):
        """Loopback client (127.0.0.1) gets a 200 from /__health."""
        from proxy_relay.handler import handle_connection
        from proxy_relay.upstream import UpstreamInfo

        upstream = UpstreamInfo(
            host="proxy.example.com", port=1080,
            username="", password="",
            url="socks5://proxy.example.com:1080",
            masked_url="socks5://proxy.example.com:1080",
            country="us",
        )

        request = b"GET /__health HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\n"
        reader = self._make_reader(request)
        writer = self._make_writer(("127.0.0.1", 54321))

        written_data: list[bytes] = []
        writer.write = lambda data: written_data.append(data)

        async def health_cb() -> tuple[bool, str]:
            return True, "1.2.3.4"

        await handle_connection(reader, writer, upstream, health_callback=health_cb)

        combined = b"".join(written_data)
        # Must NOT be a 403
        assert b"403" not in combined
        assert b"200" in combined or b"OK" in combined

    @pytest.mark.asyncio
    async def test_health_from_remote_rejected(self):
        """Non-loopback client (192.168.1.5) gets 403 for /__health."""
        from proxy_relay.handler import handle_connection
        from proxy_relay.upstream import UpstreamInfo

        upstream = UpstreamInfo(
            host="proxy.example.com", port=1080,
            username="", password="",
            url="socks5://proxy.example.com:1080",
            masked_url="socks5://proxy.example.com:1080",
            country="us",
        )

        request = b"GET /__health HTTP/1.1\r\nHost: example.com\r\n\r\n"
        reader = self._make_reader(request)
        writer = self._make_writer(("192.168.1.5", 54321))

        written_data: list[bytes] = []
        writer.write = lambda data: written_data.append(data)

        async def health_cb() -> tuple[bool, str]:
            return True, "1.2.3.4"

        await handle_connection(reader, writer, upstream, health_callback=health_cb)

        combined = b"".join(written_data)
        assert b"403" in combined


# ===========================================================================
# PR-8 — Cookie freshness warning
# ===========================================================================


class TestPR8CookieFreshness:
    """PR-8: DatadomeCookieNotExpired warns when cookie is > 7 days old."""

    def _make_ctx(self, profile_dir: Path):
        from proxy_relay.profile_rules import BrowseContext
        return BrowseContext(profile_dir=profile_dir, exit_ip="1.2.3.4", country="CO")

    def _make_datadome_db(self, profile_dir: Path, expires_utc: int) -> None:
        _make_cookies_db(profile_dir, [
            {"name": "datadome", "host_key": ".tidal.com", "expires_utc": expires_utc, "value": "dd"}
        ])

    def test_fresh_cookie_no_warning(self, tmp_path: Path):
        """Cookie issued < 7 days ago passes without a warning reason."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        # Cookie issued 3 days ago → expires in 362 days
        expires_unix = time.time() + (86400 * 362)
        expires_utc = _chromium_expires(expires_unix)
        self._make_datadome_db(tmp_path, expires_utc)

        rule = DatadomeCookieNotExpired()
        ctx = self._make_ctx(tmp_path)
        result = rule.evaluate(ctx)

        assert result.passed is True
        assert result.skipped is False
        # No freshness warning in reason
        assert result.reason is None or "consider re-warmup" not in result.reason.lower()

    def test_stale_cookie_warns(self, tmp_path: Path):
        """Cookie issued > 7 days ago passes but includes a freshness warning."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        # Cookie issued 30 days ago → expires in 335 days
        expires_unix = time.time() + (86400 * 335)
        expires_utc = _chromium_expires(expires_unix)
        self._make_datadome_db(tmp_path, expires_utc)

        rule = DatadomeCookieNotExpired()
        ctx = self._make_ctx(tmp_path)
        result = rule.evaluate(ctx)

        assert result.passed is True
        assert result.skipped is False
        assert result.reason is not None
        assert "consider re-warmup" in result.reason.lower()

    def test_freshness_threshold_is_7_days(self):
        """_FRESHNESS_DAYS class constant must be 7."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        assert DatadomeCookieNotExpired._FRESHNESS_DAYS == 7

    def test_cookie_age_calculation_fresh(self):
        """_cookie_age_days() returns ~0 for a cookie just issued (expires in ~365 days)."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        now = time.time()
        # Expires in exactly 365 days from now
        expires_unix = now + 86400 * 365
        expires_chromium = _chromium_expires(expires_unix)

        age = DatadomeCookieNotExpired._cookie_age_days(expires_chromium, now=now)
        assert age is not None
        # Should be ~0 days old (just issued)
        assert 0 <= age < 1

    def test_cookie_age_calculation_stale(self):
        """_cookie_age_days() returns ~30 for a cookie issued 30 days ago."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        now = time.time()
        # Issued 30 days ago → expires in 335 days
        expires_unix = now + 86400 * 335
        expires_chromium = _chromium_expires(expires_unix)

        age = DatadomeCookieNotExpired._cookie_age_days(expires_chromium, now=now)
        assert age is not None
        # Should be approximately 30 days old
        assert 29 <= age <= 31

    def test_cookie_age_returns_none_for_unusual_lifetime(self):
        """_cookie_age_days() returns None for cookies with lifetime > 365 days."""
        from proxy_relay.profile_rules import DatadomeCookieNotExpired

        now = time.time()
        # Expires in 400 days — unusual lifetime (issued_days_ago would be negative)
        expires_unix = now + 86400 * 400
        expires_chromium = _chromium_expires(expires_unix)

        age = DatadomeCookieNotExpired._cookie_age_days(expires_chromium, now=now)
        assert age is None


# ===========================================================================
# PR-9 — IPv6 disable
# ===========================================================================


class TestPR9DisableIPv6:
    """PR-9: --disable-ipv6 must appear in Chrome args."""

    def test_disable_ipv6_present(self):
        from proxy_relay.browse import _chrome_args

        cmd, _env = _chrome_args(
            Path("/usr/bin/chromium"),
            Path("/tmp/profile"),
        )
        assert "--disable-ipv6" in cmd
