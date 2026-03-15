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

    def test_non_verbose_omits_json_keys(self, populated_db: Path):
        from proxy_relay.capture.analyzer import analyze

        report = analyze(populated_db, verbose=False)
        tidal_eps = report.api_surface.get("api.tidal.com", [])
        for ep in tidal_eps:
            assert ep.json_keys == [], "non-verbose should have empty json_keys"


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
