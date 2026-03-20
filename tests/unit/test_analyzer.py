"""Tests for proxy_relay.capture.analyzer — post-capture traffic analysis."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """Create a capture.db with schema tables but no data."""
    db_path = tmp_path / "capture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CREATE_TABLES_SQL)
    conn.close()
    return db_path


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """Create a capture.db with realistic test data."""
    db_path = tmp_path / "capture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CREATE_TABLES_SQL)
    _insert_test_data(conn)
    conn.close()
    return db_path


_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS http_requests (
    timestamp TEXT,
    request_id TEXT,
    url TEXT,
    method TEXT,
    headers TEXT,
    post_data TEXT,
    profile TEXT
);
CREATE TABLE IF NOT EXISTS http_responses (
    timestamp TEXT,
    request_id TEXT,
    url TEXT,
    status INTEGER,
    mime_type TEXT,
    headers TEXT,
    body TEXT,
    response_ms INTEGER,
    profile TEXT
);
CREATE TABLE IF NOT EXISTS cookies (
    timestamp TEXT,
    domain TEXT,
    name TEXT,
    value TEXT,
    http_only INTEGER,
    secure INTEGER,
    expires REAL,
    path TEXT,
    profile TEXT
);
CREATE TABLE IF NOT EXISTS storage_snapshots (
    timestamp TEXT,
    origin TEXT,
    storage_type TEXT,
    key TEXT,
    value TEXT,
    change_type TEXT,
    profile TEXT
);
CREATE TABLE IF NOT EXISTS websocket_frames (
    timestamp TEXT,
    request_id TEXT,
    url TEXT,
    direction TEXT,
    payload TEXT,
    opcode INTEGER,
    profile TEXT
);
CREATE TABLE IF NOT EXISTS page_navigations (
    timestamp TEXT,
    url TEXT,
    frame_id TEXT,
    transition_type TEXT,
    mime_type TEXT,
    profile TEXT
);
"""


def _insert_test_data(conn: sqlite3.Connection) -> None:
    """Insert realistic test data for all analysis sections."""
    # Requests — tidal.com endpoints
    conn.executemany(
        "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-03-15T14:00:01", "r1", "https://api.tidal.com/v1/tracks/123", "GET",
             "User-Agent: TIDAL/2.26.1\nAccept: application/json", "default"),
            ("2026-03-15T14:00:02", "r2", "https://api.tidal.com/v1/tracks/456", "GET",
             "User-Agent: TIDAL/2.26.1\nAccept: application/json", "default"),
            ("2026-03-15T14:00:05", "r3", "https://api.tidal.com/v1/albums/789/tracks", "GET",
             "User-Agent: TIDAL/2.26.1\nAccept: application/json", "default"),
            ("2026-03-15T14:00:10", "r4", "https://auth.tidal.com/v1/oauth2/token", "POST",
             "User-Agent: TIDAL/2.26.1\nAccept: application/json", "default"),
            ("2026-03-15T14:13:15", "r5", "https://auth.tidal.com/v1/oauth2/token", "POST",
             "User-Agent: TIDAL/2.26.1\nAccept: application/json", "default"),
            ("2026-03-15T14:00:03", "r6", "https://www.qobuz.com/api/track/get", "GET",
             "User-Agent: TIDAL/2.26.1\nAccept: text/html", "default"),
        ],
    )

    # Responses
    conn.executemany(
        "INSERT INTO http_responses (timestamp, request_id, url, status, mime_type,"
        " headers, body, response_ms, profile)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-03-15T14:00:01", "r1", "https://api.tidal.com/v1/tracks/123", 200,
             "application/json", "", '{"id": 123, "title": "Track"}', 150, "default"),
            ("2026-03-15T14:00:02", "r2", "https://api.tidal.com/v1/tracks/456", 200,
             "application/json", "", '{"id": 456, "title": "Other"}', 120, "default"),
            ("2026-03-15T14:00:05", "r3", "https://api.tidal.com/v1/albums/789/tracks", 200,
             "application/json", "", '{"items": []}', 200, "default"),
            ("2026-03-15T14:00:10", "r4", "https://auth.tidal.com/v1/oauth2/token", 200,
             "application/json", "", '{"access_token": "xxx"}', 145, "default"),
            ("2026-03-15T14:13:15", "r5", "https://auth.tidal.com/v1/oauth2/token", 200,
             "application/json", "", '{"access_token": "yyy"}', 132, "default"),
            ("2026-03-15T14:00:03", "r6", "https://www.qobuz.com/api/track/get", 403,
             "text/html", "", "", 50, "default"),
        ],
    )

    # Cookies
    conn.executemany(
        "INSERT INTO cookies (timestamp, domain, name, value, http_only, secure, profile)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-03-15T14:00:01", ".tidal.com", "_tid_session", "abc123", 1, 1, "default"),
            ("2026-03-15T14:05:00", ".tidal.com", "_tid_session", "def456", 1, 1, "default"),
            ("2026-03-15T14:00:01", ".tidal.com", "_tid_pref", "dark", 0, 0, "default"),
        ],
    )

    # Storage snapshots
    conn.executemany(
        "INSERT INTO storage_snapshots (timestamp, origin, storage_type, key, value,"
        " change_type, profile)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("2026-03-15T14:00:01", "https://listen.tidal.com", "localStorage",
             "auth_token", "tok_xxx", "changed", "default"),
            ("2026-03-15T14:05:00", "https://listen.tidal.com", "localStorage",
             "auth_token", "tok_yyy", "changed", "default"),
            ("2026-03-15T14:00:01", "https://listen.tidal.com", "localStorage",
             "_dd_s", "dd_value_1", "changed", "default"),
            ("2026-03-15T14:01:00", "https://listen.tidal.com", "localStorage",
             "_dd_s", "dd_value_2", "changed", "default"),
            ("2026-03-15T14:02:00", "https://listen.tidal.com", "localStorage",
             "_dd_s", "dd_value_3", "changed", "default"),
            ("2026-03-15T14:00:01", "https://listen.tidal.com", "localStorage",
             "user_preferences", "theme=dark", "changed", "default"),
        ],
    )

    conn.commit()


# ---------------------------------------------------------------------------
# 1. analyze — basic
# ---------------------------------------------------------------------------


class TestAnalyze:
    """Verify the top-level analyze function."""

    def test_analyze_empty_db(self, empty_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(empty_db)
        assert report.total_requests == 0
        assert report.total_responses == 0
        assert report.session_duration_s == 0.0
        assert report.api_surface == {}
        assert report.auth_events == []

    def test_analyze_populated_db(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        assert report.total_requests == 6
        assert report.total_responses == 6
        assert report.session_duration_s > 0

    def test_analyze_missing_db_raises(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze

        with pytest.raises(FileNotFoundError):
            analyze(tmp_path / "nonexistent.db")


# ---------------------------------------------------------------------------
# 2. API Surface
# ---------------------------------------------------------------------------


class TestApiSurface:
    """Verify API surface analysis groups endpoints by domain."""

    def test_groups_by_domain(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        assert "api.tidal.com" in report.api_surface
        assert "www.qobuz.com" in report.api_surface

    def test_counts_calls_per_endpoint(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        tidal_eps = report.api_surface["api.tidal.com"]
        tracks_ep = [e for e in tidal_eps if "/v1/tracks/" in e.path]
        # Two separate track requests (r1 + r2) have different paths
        assert len(tracks_ep) == 2

    def test_verbose_includes_json_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db, verbose=True)
        tidal_eps = report.api_surface.get("api.tidal.com", [])
        # At least one endpoint should have json_keys populated
        has_keys = any(ep.json_keys for ep in tidal_eps)
        assert has_keys, "verbose mode should populate json_keys"

    def test_non_verbose_extracts_sample_json_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db, verbose=False)
        tidal_eps = report.api_surface.get("api.tidal.com", [])
        # F-RL14: non-verbose now extracts keys from one sample body per endpoint
        has_keys = any(ep.json_keys for ep in tidal_eps)
        assert has_keys, "non-verbose should extract sample json_keys (F-RL14)"


# ---------------------------------------------------------------------------
# 3. Auth Flow
# ---------------------------------------------------------------------------


class TestAuthFlow:
    """Verify auth flow detection."""

    def test_detects_oauth_token_requests(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        assert len(report.auth_events) >= 2
        urls = [e.url for e in report.auth_events]
        assert any("oauth2/token" in u for u in urls)

    def test_auth_events_have_timing(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        for evt in report.auth_events:
            assert evt.response_ms >= 0


# ---------------------------------------------------------------------------
# 4. Fingerprint Audit
# ---------------------------------------------------------------------------


class TestFingerprintAudit:
    """Verify header fingerprint analysis."""

    def test_detects_user_agent_consistency(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        ua_vectors = [v for v in report.fingerprint_vectors if v.header_name == "user-agent"]
        assert len(ua_vectors) == 1
        assert ua_vectors[0].consistency == 1.0  # all same UA

    def test_detects_inconsistent_header(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        accept_vectors = [v for v in report.fingerprint_vectors if v.header_name == "accept"]
        assert len(accept_vectors) == 1
        # 5 requests have "application/json", 1 has "text/html"
        assert accept_vectors[0].consistency < 1.0


# ---------------------------------------------------------------------------
# 5. Rate Limits
# ---------------------------------------------------------------------------


class TestRateLimits:
    """Verify rate limit detection."""

    def test_detects_403_response(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        assert len(report.rate_limit_events) >= 1
        statuses = [e.status for e in report.rate_limit_events]
        assert 403 in statuses

    def test_preceding_request_count(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        for evt in report.rate_limit_events:
            assert evt.preceding_request_count >= 0


# ---------------------------------------------------------------------------
# 6. Behavioral Baseline
# ---------------------------------------------------------------------------


class TestBehavioralBaseline:
    """Verify timing statistics."""

    def test_computes_gap_statistics(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        baseline = report.behavioral_baseline
        assert "gap_p50" in baseline
        assert "gap_p95" in baseline
        assert baseline["gap_p50"] >= 0

    def test_computes_request_rate(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        baseline = report.behavioral_baseline
        assert "requests_per_second_avg" in baseline
        assert baseline["requests_per_second_avg"] > 0

    def test_empty_db_returns_empty_baseline(self, empty_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(empty_db)
        assert report.behavioral_baseline == {}


# ---------------------------------------------------------------------------
# 7. Storage Intelligence
# ---------------------------------------------------------------------------


class TestStorageIntelligence:
    """Verify storage key classification."""

    def test_classifies_auth_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        auth_keys = [s for s in report.storage_intelligence if s["classification"] == "auth"]
        assert len(auth_keys) >= 1
        assert any(s["key"] == "auth_token" for s in auth_keys)

    def test_classifies_tracking_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        tracking = [s for s in report.storage_intelligence if s["classification"] == "tracking"]
        assert len(tracking) >= 1
        assert any(s["key"] == "_dd_s" for s in tracking)

    def test_classifies_pref_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        prefs = [s for s in report.storage_intelligence if s["classification"] == "prefs"]
        assert len(prefs) >= 1

    def test_mutation_counts(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db)
        dd_key = [s for s in report.storage_intelligence if s["key"] == "_dd_s"]
        assert len(dd_key) == 1
        assert dd_key[0]["mutations"] == 3


# ---------------------------------------------------------------------------
# 8. Output
# ---------------------------------------------------------------------------


class TestOutput:
    """Verify report output functions."""

    def test_print_report_no_crash(self, populated_db: Path, capsys):
        from proxy_relay.capture.analyzer import analyze, print_report

        report = analyze(populated_db)
        print_report(report)  # must not raise
        captured = capsys.readouterr()
        assert "Capture Analysis" in captured.out
        assert "API Surface" in captured.out

    def test_print_report_empty_db(self, empty_db: Path, capsys):
        from proxy_relay.capture.analyzer import analyze, print_report

        report = analyze(empty_db)
        print_report(report)  # must not raise
        captured = capsys.readouterr()
        assert "Capture Analysis" in captured.out

    def test_write_report_creates_file(self, populated_db: Path, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze, write_report

        report = analyze(populated_db)
        report_path = write_report(report, output_dir=tmp_path)
        assert report_path.exists()
        assert report_path.suffix == ".md"
        content = report_path.read_text()
        assert "# Capture Analysis Report" in content
        assert "API Surface Map" in content

    def test_write_report_uses_default_report_dir(self, populated_db: Path, tmp_path: Path):
        from unittest.mock import patch as _patch

        from proxy_relay.capture.analyzer import analyze, write_report

        report = analyze(populated_db)
        # Redirect DEFAULT_REPORT_DIR to tmp_path so we don't write to real config dir
        with _patch("proxy_relay.capture.models.DEFAULT_REPORT_DIR", tmp_path):
            path = write_report(report)

        assert path.parent == tmp_path
        assert path.exists()


# ---------------------------------------------------------------------------
# 9. Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    """Verify internal helper functions."""

    def test_classify_storage_key_auth(self):
        from proxy_relay.capture.analyzer import _classify_storage_key

        assert _classify_storage_key("auth_token") == "auth"
        assert _classify_storage_key("oauth_session") == "auth"

    def test_classify_storage_key_tracking(self):
        from proxy_relay.capture.analyzer import _classify_storage_key

        assert _classify_storage_key("_dd_s") == "tracking"
        assert _classify_storage_key("analytics_id") == "tracking"

    def test_classify_storage_key_prefs(self):
        from proxy_relay.capture.analyzer import _classify_storage_key

        assert _classify_storage_key("user_preferences") == "prefs"
        assert _classify_storage_key("theme_setting") == "prefs"

    def test_classify_storage_key_other(self):
        from proxy_relay.capture.analyzer import _classify_storage_key

        assert _classify_storage_key("some_random_key") == "other"

    def test_format_duration(self):
        from proxy_relay.capture.analyzer import _format_duration

        assert _format_duration(30) == "30s"
        assert _format_duration(90) == "1m 30s"
        assert _format_duration(3661) == "1h 1m"

    def test_parse_headers(self):
        from proxy_relay.capture.analyzer import _parse_headers

        result = _parse_headers("User-Agent: Mozilla\nAccept: text/html")
        assert result["User-Agent"] == "Mozilla"
        assert result["Accept"] == "text/html"

    def test_extract_json_keys(self):
        from proxy_relay.capture.analyzer import _extract_json_keys

        keys = _extract_json_keys('{"id": 1, "title": "Test", "artist": "A"}')
        assert keys == {"id", "title", "artist"}

    def test_extract_json_keys_invalid(self):
        from proxy_relay.capture.analyzer import _extract_json_keys

        assert _extract_json_keys("not json") == set()
        assert _extract_json_keys("") == set()

    def test_parse_headers_no_space_after_colon(self):
        """F-RL17: headers stored as ``Name:Value`` (no space) should parse."""
        from proxy_relay.capture.analyzer import _parse_headers

        result = _parse_headers("Accept-Encoding:gzip\nAccept-Language:en-US")
        assert result["Accept-Encoding"] == "gzip"
        assert result["Accept-Language"] == "en-US"

    def test_parse_headers_mixed_format(self):
        """F-RL17: mix of ``Name: Value`` and ``Name:Value`` both parse."""
        from proxy_relay.capture.analyzer import _parse_headers

        result = _parse_headers("User-Agent: Mozilla\nAccept-Encoding:gzip")
        assert result["User-Agent"] == "Mozilla"
        assert result["Accept-Encoding"] == "gzip"

    def test_parse_headers_value_with_colon(self):
        """Ensure values containing colons are preserved."""
        from proxy_relay.capture.analyzer import _parse_headers

        result = _parse_headers("Date: Mon, 01 Jan 2026 00:00:00 GMT")
        assert result["Date"] == "Mon, 01 Jan 2026 00:00:00 GMT"

    def test_format_gap_sub_second(self):
        """F-RL16: sub-second gaps display in milliseconds."""
        from proxy_relay.capture.analyzer import _format_gap

        assert _format_gap(0.150) == "150ms"
        assert _format_gap(0.0) == "0ms"
        assert _format_gap(0.999) == "999ms"

    def test_format_gap_seconds(self):
        """F-RL16: gaps >= 1s display as seconds with 2 decimal places."""
        from proxy_relay.capture.analyzer import _format_gap

        assert _format_gap(1.0) == "1.00s"
        assert _format_gap(2.5) == "2.50s"

    def test_normalize_path_image_urls(self):
        """F-RL13: CDN image URLs are collapsed by pattern."""
        from proxy_relay.capture.analyzer import _normalize_path

        result = _normalize_path(
            "resources.tidal.com", "/images/abc123/320x320.jpg", ""
        )
        assert result == "/images/abc123/{WxH}.jpg"

    def test_normalize_path_segment_urls(self):
        """F-RL13: CDN segment URLs are collapsed."""
        from proxy_relay.capture.analyzer import _normalize_path

        result = _normalize_path(
            "cdn.tidal.com",
            "/aabbccddeeff00112233/segment-42.m4s",
            "",
        )
        assert result == "/aabbccddeeff00112233/{segments}"

    def test_normalize_path_passthrough(self):
        """F-RL13: non-CDN paths are returned unchanged."""
        from proxy_relay.capture.analyzer import _normalize_path

        result = _normalize_path("api.tidal.com", "/v1/tracks/123", "")
        assert result == "/v1/tracks/123"


# ---------------------------------------------------------------------------
# 10. F-RL13: URL collapsing in API surface
# ---------------------------------------------------------------------------


class TestUrlCollapsing:
    """Verify CDN/image URL collapsing in API surface analysis."""

    def test_image_urls_collapsed(self, tmp_path: Path):
        """Multiple image size variants collapse to one endpoint."""
        from proxy_relay.capture.analyzer import analyze

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_SQL)
        conn.executemany(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "i1",
                 "https://resources.tidal.com/images/abc123/320x320.jpg", "GET", "", "default"),
                ("2026-03-15T14:00:02", "i2",
                 "https://resources.tidal.com/images/abc123/640x640.jpg", "GET", "", "default"),
                ("2026-03-15T14:00:03", "i3",
                 "https://resources.tidal.com/images/abc123/1280x1280.jpg", "GET", "", "default"),
            ],
        )
        conn.executemany(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "i1",
                 "https://resources.tidal.com/images/abc123/320x320.jpg", 200, "", 10, "default"),
                ("2026-03-15T14:00:02", "i2",
                 "https://resources.tidal.com/images/abc123/640x640.jpg", 200, "", 10, "default"),
                ("2026-03-15T14:00:03", "i3",
                 "https://resources.tidal.com/images/abc123/1280x1280.jpg", 200, "", 10, "default"),
            ],
        )
        conn.commit()
        conn.close()

        report = analyze(db_path)
        eps = report.api_surface.get("resources.tidal.com", [])
        assert len(eps) == 1, f"Expected 1 collapsed endpoint, got {len(eps)}"
        assert eps[0].call_count == 3
        assert "{WxH}" in eps[0].path


# ---------------------------------------------------------------------------
# 11. F-RL14: JSON keys in non-verbose mode
# ---------------------------------------------------------------------------


class TestJsonKeysNonVerbose:
    """Verify JSON key extraction works even in non-verbose mode."""

    def test_sample_keys_extracted(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db, verbose=False)
        all_eps = [ep for eps in report.api_surface.values() for ep in eps]
        eps_with_keys = [ep for ep in all_eps if ep.json_keys]
        assert len(eps_with_keys) >= 1, "Should extract sample keys in non-verbose"

    def test_verbose_has_all_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db, verbose=True)
        tidal_eps = report.api_surface.get("api.tidal.com", [])
        has_keys = any(ep.json_keys for ep in tidal_eps)
        assert has_keys


# ---------------------------------------------------------------------------
# 12. F-RL15: OPTIONS preflight filtering
# ---------------------------------------------------------------------------


class TestOptionsFiltering:
    """Verify OPTIONS requests are filtered from API surface."""

    def test_options_filtered_from_surface(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_SQL)
        conn.executemany(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "o1", "https://api.tidal.com/v1/tracks/1", "OPTIONS",
                 "", "default"),
                ("2026-03-15T14:00:02", "o2", "https://api.tidal.com/v1/tracks/1", "OPTIONS",
                 "", "default"),
                ("2026-03-15T14:00:03", "g1", "https://api.tidal.com/v1/tracks/1", "GET",
                 "", "default"),
            ],
        )
        conn.executemany(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "o1", "https://api.tidal.com/v1/tracks/1", 204, "", 5, "default"),
                ("2026-03-15T14:00:02", "o2", "https://api.tidal.com/v1/tracks/1", 204, "", 5, "default"),
                ("2026-03-15T14:00:03", "g1", "https://api.tidal.com/v1/tracks/1", 200, "{}", 50, "default"),
            ],
        )
        conn.commit()
        conn.close()

        report = analyze(db_path)
        assert report.options_filtered == 2
        # Only the GET endpoint should be in the surface
        tidal_eps = report.api_surface.get("api.tidal.com", [])
        methods = [ep.method for ep in tidal_eps]
        assert "OPTIONS" not in methods
        assert "GET" in methods

    def test_options_filtered_in_print_report(self, tmp_path: Path, capsys):
        from proxy_relay.capture.analyzer import analyze, print_report

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_SQL)
        conn.execute(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-03-15T14:00:01", "o1", "https://api.tidal.com/v1/x", "OPTIONS", "", "default"),
        )
        conn.execute(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-03-15T14:00:01", "o1", "https://api.tidal.com/v1/x", 204, "", 5, "default"),
        )
        conn.commit()
        conn.close()

        report = analyze(db_path)
        print_report(report)
        captured = capsys.readouterr()
        assert "1 OPTIONS filtered" in captured.out

    def test_options_filtered_in_write_report(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import AnalysisReport, write_report

        report = AnalysisReport(db_path="test.db", analyzed_at="now", options_filtered=5)
        path = write_report(report, output_dir=tmp_path)
        content = path.read_text()
        assert "OPTIONS filtered" in content


# ---------------------------------------------------------------------------
# 13. F-RL16: Millisecond timing in reports
# ---------------------------------------------------------------------------


class TestMillisecondTiming:
    """Verify sub-second gaps are shown in milliseconds."""

    def test_sub_second_gaps_in_print(self, tmp_path: Path, capsys):
        from proxy_relay.capture.analyzer import analyze, print_report

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_SQL)
        # Insert 3 requests 100ms apart
        conn.executemany(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:00.000", "t1", "https://api.tidal.com/v1/a", "GET", "", "default"),
                ("2026-03-15T14:00:00.100", "t2", "https://api.tidal.com/v1/b", "GET", "", "default"),
                ("2026-03-15T14:00:00.200", "t3", "https://api.tidal.com/v1/c", "GET", "", "default"),
            ],
        )
        conn.executemany(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:00.000", "t1", "https://api.tidal.com/v1/a", 200, "", 10, "default"),
                ("2026-03-15T14:00:00.100", "t2", "https://api.tidal.com/v1/b", 200, "", 10, "default"),
                ("2026-03-15T14:00:00.200", "t3", "https://api.tidal.com/v1/c", 200, "", 10, "default"),
            ],
        )
        conn.commit()
        conn.close()

        report = analyze(db_path)
        print_report(report)
        captured = capsys.readouterr()
        assert "ms" in captured.out, "Sub-second gaps should display in ms"


# ---------------------------------------------------------------------------
# 14. F-RL17: Fingerprint header parsing
# ---------------------------------------------------------------------------


class TestFingerprintHeaderParsing:
    """Verify accept-language and accept-encoding are detected."""

    def test_accept_encoding_detected(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_SQL)
        # Headers with no space after colon
        conn.execute(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-03-15T14:00:01", "h1", "https://api.tidal.com/v1/x", "GET",
             "User-Agent: TIDAL/2.26.1\nAccept-Encoding:gzip\nAccept-Language:en-US", "default"),
        )
        conn.execute(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-03-15T14:00:01", "h1", "https://api.tidal.com/v1/x", 200, "", 10, "default"),
        )
        conn.commit()
        conn.close()

        report = analyze(db_path)
        header_names = [v.header_name for v in report.fingerprint_vectors]
        assert "accept-encoding" in header_names, "accept-encoding should be detected"
        assert "accept-language" in header_names, "accept-language should be detected"


# ---------------------------------------------------------------------------
# 15. F-RL22: Session ID filtering
# ---------------------------------------------------------------------------


_CREATE_TABLES_WITH_SESSION_SQL = _CREATE_TABLES_SQL.replace(
    "profile TEXT\n);",
    "profile TEXT,\n    session_id TEXT\n);",
)


class TestSessionFilter:
    """Verify session_id filtering in analyze()."""

    def test_session_filter_narrows_results(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_WITH_SESSION_SQL)
        conn.executemany(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "s1", "https://api.tidal.com/v1/tracks/1", "GET",
                 "", "default", "sess-A"),
                ("2026-03-15T14:00:02", "s2", "https://api.tidal.com/v1/tracks/2", "GET",
                 "", "default", "sess-A"),
                ("2026-03-15T14:00:03", "s3", "https://api.tidal.com/v1/tracks/3", "GET",
                 "", "default", "sess-B"),
            ],
        )
        conn.executemany(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "s1", "https://api.tidal.com/v1/tracks/1", 200, "{}", 10, "default", "sess-A"),
                ("2026-03-15T14:00:02", "s2", "https://api.tidal.com/v1/tracks/2", 200, "{}", 10, "default", "sess-A"),
                ("2026-03-15T14:00:03", "s3", "https://api.tidal.com/v1/tracks/3", 200, "{}", 10, "default", "sess-B"),
            ],
        )
        conn.commit()
        conn.close()

        # Filter to session A only
        report = analyze(db_path, session_id="sess-A")
        assert report.total_requests == 2
        assert report.total_responses == 2

    def test_session_filter_no_session_column(self, populated_db: Path):
        """Gracefully handle old DB without session_id column."""
        from proxy_relay.capture.analyzer import analyze

        # populated_db uses the old schema without session_id
        report = analyze(populated_db, session_id="any-session")
        # Should still work — filter is skipped, returns all data
        assert report.total_requests == 6

    def test_session_filter_none_returns_all(self, tmp_path: Path):
        from proxy_relay.capture.analyzer import analyze

        db_path = tmp_path / "capture.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_CREATE_TABLES_WITH_SESSION_SQL)
        conn.executemany(
            "INSERT INTO http_requests (timestamp, request_id, url, method, headers, profile, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "s1", "https://api.tidal.com/v1/x", "GET", "", "default", "sess-A"),
                ("2026-03-15T14:00:02", "s2", "https://api.tidal.com/v1/y", "GET", "", "default", "sess-B"),
            ],
        )
        conn.executemany(
            "INSERT INTO http_responses (timestamp, request_id, url, status, body, response_ms, profile, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-03-15T14:00:01", "s1", "https://api.tidal.com/v1/x", 200, "", 10, "default", "sess-A"),
                ("2026-03-15T14:00:02", "s2", "https://api.tidal.com/v1/y", 200, "", 10, "default", "sess-B"),
            ],
        )
        conn.commit()
        conn.close()

        report = analyze(db_path, session_id=None)
        assert report.total_requests == 2


# ---------------------------------------------------------------------------
# 16. J-RL15: _count validates table names
# ---------------------------------------------------------------------------


class TestCountTableValidation:
    """J-RL15: _count rejects unknown table names."""

    def test_allowed_tables_pass(self, populated_db: Path):
        from proxy_relay.capture.analyzer import _count

        conn = sqlite3.connect(f"file:{populated_db}?mode=ro", uri=True)
        try:
            assert _count(conn, "http_requests") >= 0
            assert _count(conn, "http_responses") >= 0
        finally:
            conn.close()

    def test_unknown_table_raises_valueerror(self, populated_db: Path):
        from proxy_relay.capture.analyzer import _count

        conn = sqlite3.connect(f"file:{populated_db}?mode=ro", uri=True)
        try:
            with pytest.raises(ValueError, match="Unknown table"):
                _count(conn, "users; DROP TABLE http_requests")
        finally:
            conn.close()

    def test_allowed_tables_constant_exists(self):
        from proxy_relay.capture.analyzer import _ALLOWED_TABLES

        assert "http_requests" in _ALLOWED_TABLES
        assert "http_responses" in _ALLOWED_TABLES
