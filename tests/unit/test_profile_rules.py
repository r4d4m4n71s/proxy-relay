"""Tests for proxy_relay.profile_rules — rule-based browser profile validation."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Chromium epoch constants
# Chromium stores timestamps as microseconds since 1601-01-01 00:00:00 UTC.
# The offset between Unix epoch and Chromium epoch is 11,644,473,600 seconds.
# ---------------------------------------------------------------------------
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600  # seconds

# expires_utc for a cookie expiring ~1 year in the future
_VALID_EXPIRES_UTC = int((time.time() + 86400 * 365) + _CHROMIUM_EPOCH_OFFSET) * 1_000_000

# expires_utc for a cookie that expired yesterday
_EXPIRED_EXPIRES_UTC = int((time.time() - 86400) + _CHROMIUM_EPOCH_OFFSET) * 1_000_000


# ---------------------------------------------------------------------------
# SQLite helper — creates a minimal Chromium Cookies database
# ---------------------------------------------------------------------------

def _make_cookies_db(profile_dir: Path, cookies: list[dict]) -> Path:
    """Create a minimal Chromium Cookies SQLite DB with the given cookie rows.

    Each dict: {"name": str, "host_key": str, "expires_utc": int, "value": str}
    """
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


# ---------------------------------------------------------------------------
# Helpers for common cookie rows
# ---------------------------------------------------------------------------

def _datadome_cookie(expires_utc: int = _VALID_EXPIRES_UTC, value: str = "dd_val") -> dict:
    return {"name": "datadome", "host_key": ".tidal.com", "expires_utc": expires_utc, "value": value}


def _app_lang_cookie(value: str = "es") -> dict:
    return {"name": "app_lang", "host_key": ".tidal.com", "expires_utc": _VALID_EXPIRES_UTC, "value": value}


def _other_cookie() -> dict:
    return {"name": "other_cookie", "host_key": ".tidal.com", "expires_utc": _VALID_EXPIRES_UTC, "value": "x"}


def _make_meta(profile_dir: Path, exit_ip: str = "1.2.3.4", country: str = "CO") -> None:
    """Write a .warmup-meta.json via the module under test."""
    from proxy_relay.profile_rules import write_warmup_meta
    write_warmup_meta(profile_dir, exit_ip=exit_ip, country=country)


# ===========================================================================
# TestBrowseContext
# ===========================================================================


class TestBrowseContext:
    """BrowseContext dataclass field defaults and storage."""

    def test_defaults_are_correct(self):
        from proxy_relay.profile_rules import BrowseContext

        ctx = BrowseContext(profile_dir=Path("/tmp/p"), exit_ip="1.2.3.4", country="CO")
        assert ctx.lang is None
        assert ctx.timezone is None
        assert ctx.account_email is None

    def test_all_fields_stored(self):
        from proxy_relay.profile_rules import BrowseContext

        p = Path("/tmp/profile")
        ctx = BrowseContext(
            profile_dir=p,
            exit_ip="10.0.0.1",
            country="DE",
            lang="de-DE,de",
            timezone="Europe/Berlin",
            account_email="user@example.com",
        )
        assert ctx.profile_dir == p
        assert ctx.exit_ip == "10.0.0.1"
        assert ctx.country == "DE"
        assert ctx.lang == "de-DE,de"
        assert ctx.timezone == "Europe/Berlin"
        assert ctx.account_email == "user@example.com"


# ===========================================================================
# TestRuleResult
# ===========================================================================


class TestRuleResult:
    """RuleResult dataclass construction and field values."""

    def test_skipped_result(self):
        from proxy_relay.profile_rules import Remediation, RuleResult

        r = RuleResult(
            passed=False,
            skipped=True,
            rule_name="profile_exists",
            reason="skipped because parent failed",
            remediation=Remediation.NONE,
        )
        assert r.skipped is True
        assert r.passed is False
        assert r.rule_name == "profile_exists"
        assert r.remediation == Remediation.NONE

    def test_failed_result(self):
        from proxy_relay.profile_rules import Remediation, RuleResult

        r = RuleResult(
            passed=False,
            skipped=False,
            rule_name="datadome_cookie_exists",
            reason="no datadome cookie found",
            remediation=Remediation.DELETE_COOKIE,
        )
        assert r.passed is False
        assert r.skipped is False
        assert r.remediation == Remediation.DELETE_COOKIE


# ===========================================================================
# TestWarmupMeta
# ===========================================================================


class TestWarmupMeta:
    """write_warmup_meta / read_warmup_meta round-trip and error handling."""

    def test_write_and_read_roundtrip(self, tmp_path):
        from proxy_relay.profile_rules import read_warmup_meta, write_warmup_meta

        write_warmup_meta(tmp_path, exit_ip="5.6.7.8", country="CO")
        meta = read_warmup_meta(tmp_path)

        assert meta is not None
        assert meta["issued_ip"] == "5.6.7.8"
        assert meta["issued_country"] == "CO"

    def test_read_missing_returns_none(self, tmp_path):
        from proxy_relay.profile_rules import read_warmup_meta

        result = read_warmup_meta(tmp_path)
        assert result is None

    def test_write_stores_correct_fields(self, tmp_path):
        from proxy_relay.profile_rules import read_warmup_meta, write_warmup_meta

        write_warmup_meta(tmp_path, exit_ip="9.9.9.9", country="US", account_email="u@example.com")
        meta = read_warmup_meta(tmp_path)

        assert meta is not None
        assert "issued_ip" in meta
        assert "issued_country" in meta
        assert "issued_at" in meta
        assert "account_email" in meta
        assert meta["issued_ip"] == "9.9.9.9"
        assert meta["issued_country"] == "US"
        assert meta["account_email"] == "u@example.com"

    def test_account_email_none_stored(self, tmp_path):
        from proxy_relay.profile_rules import read_warmup_meta, write_warmup_meta

        write_warmup_meta(tmp_path, exit_ip="1.1.1.1", country="GB", account_email=None)
        meta = read_warmup_meta(tmp_path)

        assert meta is not None
        # None should be stored — key present, value is None
        assert meta.get("account_email") is None

    def test_read_invalid_json_returns_none(self, tmp_path):
        from proxy_relay.profile_rules import read_warmup_meta

        meta_file = tmp_path / ".warmup-meta.json"
        meta_file.write_text("not valid json {{{")

        result = read_warmup_meta(tmp_path)
        assert result is None


# ===========================================================================
# TestIsTidalUrl
# ===========================================================================


class TestIsTidalUrl:
    """is_tidal_url() URL classification."""

    def test_none_returns_false(self):
        from proxy_relay.profile_rules import is_tidal_url

        # None means "no --start-url" — skip TIDAL-specific validation
        assert is_tidal_url(None) is False

    def test_tidal_com_returns_true(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://tidal.com") is True

    def test_listen_tidal_returns_true(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://listen.tidal.com") is True

    def test_login_tidal_returns_true(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://login.tidal.com/oauth2/authorize") is True

    def test_browserleaks_returns_false(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://browserleaks.com/ip") is False

    def test_google_returns_false(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://www.google.com") is False

    def test_partial_match_tidal_in_path_returns_true(self):
        from proxy_relay.profile_rules import is_tidal_url

        assert is_tidal_url("https://tidal.com/browse") is True


# ===========================================================================
# TestProfileExistsRule
# ===========================================================================


class TestProfileExistsRule:
    """profile_exists rule — directory presence and non-emptiness."""

    def test_existing_nonempty_dir_passes(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        (tmp_path / "some_file.txt").write_text("data")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_exists"].passed is True
        assert results["profile_exists"].skipped is False

    def test_missing_dir_fails(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        missing = tmp_path / "nonexistent"
        ctx = BrowseContext(profile_dir=missing, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_exists"].passed is False
        assert results["profile_exists"].skipped is False

    def test_empty_dir_fails(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        ctx = BrowseContext(profile_dir=empty_dir, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_exists"].passed is False

    def test_remediation_is_delete_profile(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, Remediation, default_registry

        missing = tmp_path / "nonexistent"
        ctx = BrowseContext(profile_dir=missing, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_exists"].remediation == Remediation.DELETE_PROFILE


# ===========================================================================
# TestProfileNotCorruptedRule
# ===========================================================================


class TestProfileNotCorruptedRule:
    """profile_not_corrupted rule — SQLite readability of Default/Cookies."""

    def test_valid_sqlite_passes(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        _make_cookies_db(tmp_path, [])
        (tmp_path / "marker.txt").write_text("nonempty")  # keep profile_exists happy
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_not_corrupted"].passed is True
        assert results["profile_not_corrupted"].skipped is False

    def test_corrupt_file_fails(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        db_dir = tmp_path / "Default"
        db_dir.mkdir(parents=True)
        db_path = db_dir / "Cookies"
        db_path.write_bytes(b"THIS IS NOT SQLITE GARBAGE \x00\x01\x02\x03")
        (tmp_path / "marker.txt").write_text("nonempty")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_not_corrupted"].passed is False
        assert results["profile_not_corrupted"].skipped is False

    def test_missing_cookies_db_is_skipped(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # Profile dir exists and is non-empty, but no Default/Cookies file
        (tmp_path / "some_file.txt").write_text("data")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["profile_not_corrupted"].skipped is True


# ===========================================================================
# TestDatadomeCookieExistsRule
# ===========================================================================


class TestDatadomeCookieExistsRule:
    """datadome_cookie_exists rule — presence of datadome cookie in DB."""

    def test_cookie_present_passes(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        _make_cookies_db(tmp_path, [_datadome_cookie()])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_exists"].passed is True
        assert results["datadome_cookie_exists"].skipped is False

    def test_cookie_absent_fails(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        _make_cookies_db(tmp_path, [_other_cookie()])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_exists"].passed is False
        assert results["datadome_cookie_exists"].skipped is False

    def test_missing_db_skipped(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # Valid-looking profile (non-empty dir, no Default/ subdir)
        (tmp_path / "some_file.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_exists"].skipped is True


# ===========================================================================
# TestDatadomeCookieNotExpiredRule
# ===========================================================================


class TestDatadomeCookieNotExpiredRule:
    """datadome_cookie_not_expired rule — Chromium expiry timestamp checks."""

    def test_future_expiry_passes(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        _make_cookies_db(tmp_path, [_datadome_cookie(expires_utc=_VALID_EXPIRES_UTC)])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_not_expired"].passed is True

    def test_past_expiry_fails(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        _make_cookies_db(tmp_path, [_datadome_cookie(expires_utc=_EXPIRED_EXPIRES_UTC)])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_not_expired"].passed is False

    def test_zero_expiry_passes(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # expires_utc == 0 means session cookie — never expires, always valid
        _make_cookies_db(tmp_path, [_datadome_cookie(expires_utc=0)])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_not_expired"].passed is True


# ===========================================================================


class TestRuleRegistry:
    """RuleRegistry — add / remove / evaluate_all mechanics."""

    def test_default_registry_has_5_rules(self):
        from proxy_relay.profile_rules import default_registry

        registry = default_registry()
        from proxy_relay.profile_rules import BrowseContext
        ctx = BrowseContext(profile_dir=Path("/nonexistent_profile_12345"), exit_ip="1.2.3.4", country="CO")
        results = registry.evaluate_all(ctx)
        assert len(results) == 5

    def test_add_rule(self):
        from proxy_relay.profile_rules import BrowseContext, Remediation, RuleResult, default_registry

        registry = default_registry()

        class _DummyRule:
            name = "dummy_rule"

            def evaluate(self, ctx: BrowseContext) -> RuleResult:
                return RuleResult(
                    passed=True,
                    skipped=False,
                    rule_name=self.name,
                    reason="always passes",
                    remediation=Remediation.NONE,
                )

        registry.add(_DummyRule())
        ctx = BrowseContext(profile_dir=Path("/nonexistent_12345"), exit_ip="1.2.3.4", country="CO")
        results = registry.evaluate_all(ctx)
        assert len(results) == 6
        names = {r.rule_name for r in results}
        assert "dummy_rule" in names

    def test_remove_rule(self):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        registry = default_registry()
        registry.remove("profile_exists")
        ctx = BrowseContext(profile_dir=Path("/nonexistent_12345"), exit_ip="1.2.3.4", country="CO")
        results = registry.evaluate_all(ctx)
        assert len(results) == 4
        names = {r.rule_name for r in results}
        assert "profile_exists" not in names

    def test_remove_nonexistent_is_noop(self):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        registry = default_registry()
        # Must not raise
        registry.remove("this_rule_does_not_exist")
        ctx = BrowseContext(profile_dir=Path("/nonexistent_12345"), exit_ip="1.2.3.4", country="CO")
        results = registry.evaluate_all(ctx)
        assert len(results) == 5  # unchanged


# ===========================================================================
# TestSkipLogic
# ===========================================================================


class TestSkipLogic:
    """Cascade skip behaviour when prerequisite rules fail."""

    def test_profile_missing_skips_all_others(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        missing = tmp_path / "nonexistent"
        ctx = BrowseContext(profile_dir=missing, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        # profile_exists itself is not skipped — it simply fails
        assert results["profile_exists"].skipped is False
        assert results["profile_exists"].passed is False

        # All other rules must be skipped
        other_rules = [
            "profile_not_corrupted",
            "datadome_cookie_exists",
            "datadome_cookie_not_expired",
        ]
        for name in other_rules:
            assert results[name].skipped is True, f"Expected {name!r} to be skipped"

    def test_no_cookies_db_skips_cookie_rules(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # Non-empty profile dir but no Default/Cookies SQLite file
        (tmp_path / "some_file.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        # profile_not_corrupted skipped (no DB), and cookie rules downstream also skipped
        assert results["profile_not_corrupted"].skipped is True
        assert results["datadome_cookie_exists"].skipped is True
        assert results["datadome_cookie_not_expired"].skipped is True

    def test_no_datadome_cookie_skips_expiry_and_ip(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # Cookies DB exists but no datadome cookie; meta present so language rule runs
        _make_cookies_db(tmp_path, [_other_cookie()])
        _make_meta(tmp_path, exit_ip="1.2.3.4", country="CO")
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_exists"].passed is False
        assert results["datadome_cookie_exists"].skipped is False
        assert results["datadome_cookie_not_expired"].skipped is True

    def test_all_rules_pass_with_valid_cookie(self, tmp_path):
        from proxy_relay.profile_rules import BrowseContext, default_registry

        # Full valid DB with datadome, no poisoned marker
        _make_cookies_db(tmp_path, [_datadome_cookie()])
        (tmp_path / "marker.txt").write_text("x")
        ctx = BrowseContext(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        registry = default_registry()
        results = {r.rule_name: r for r in registry.evaluate_all(ctx)}

        assert results["datadome_cookie_not_expired"].passed is True
        assert results["profile_not_poisoned"].passed is True


# ===========================================================================
# TestPrintValidationReport
# ===========================================================================


class TestPrintValidationReport:
    """print_validation_report() — console output structure."""

    # ------------------------------------------------------------------
    # Helpers to build minimal contexts and result lists
    # ------------------------------------------------------------------

    def _all_pass_results(self) -> list:
        from proxy_relay.profile_rules import Remediation, RuleResult
        names = [
            "profile_exists",
            "profile_not_corrupted",
            "profile_not_poisoned",
            "datadome_cookie_exists",
            "datadome_cookie_not_expired",
        ]
        return [
            RuleResult(
                passed=True,
                skipped=False,
                rule_name=n,
                reason="ok",
                remediation=Remediation.NONE,
            )
            for n in names
        ]

    def _one_failure(self, failed_name: str, remediation=None) -> list:
        from proxy_relay.profile_rules import Remediation, RuleResult
        if remediation is None:
            remediation = Remediation.DELETE_PROFILE
        names = [
            "profile_exists",
            "profile_not_corrupted",
            "profile_not_poisoned",
            "datadome_cookie_exists",
            "datadome_cookie_not_expired",
        ]
        results = []
        for n in names:
            if n == failed_name:
                results.append(
                    RuleResult(
                        passed=False,
                        skipped=False,
                        rule_name=n,
                        reason="something went wrong",
                        remediation=remediation,
                    )
                )
            else:
                results.append(
                    RuleResult(
                        passed=True,
                        skipped=False,
                        rule_name=n,
                        reason="ok",
                        remediation=Remediation.NONE,
                    )
                )
        return results

    def _one_skipped(self, skipped_name: str) -> list:
        from proxy_relay.profile_rules import Remediation, RuleResult
        names = [
            "profile_exists",
            "profile_not_corrupted",
            "profile_not_poisoned",
            "datadome_cookie_exists",
            "datadome_cookie_not_expired",
        ]
        results = []
        for n in names:
            if n == skipped_name:
                results.append(
                    RuleResult(
                        passed=False,
                        skipped=True,
                        rule_name=n,
                        reason="skipped",
                        remediation=Remediation.NONE,
                    )
                )
            else:
                results.append(
                    RuleResult(
                        passed=True,
                        skipped=False,
                        rule_name=n,
                        reason="ok",
                        remediation=Remediation.NONE,
                    )
                )
        return results

    def _ctx(self, tmp_path: Path, **kwargs) -> object:
        from proxy_relay.profile_rules import BrowseContext
        defaults = dict(profile_dir=tmp_path, exit_ip="1.2.3.4", country="CO")
        defaults.update(kwargs)
        return BrowseContext(**defaults)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_all_pass_shows_passed_message(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        print_validation_report(ctx, self._all_pass_results(), profile_name="medellin")
        out = capsys.readouterr().out
        # Should contain some indication that all rules passed
        assert "pass" in out.lower() or "ok" in out.lower() or "valid" in out.lower()

    def test_failure_shows_rule_name_and_reason(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        results = self._one_failure("datadome_cookie_exists")
        print_validation_report(ctx, results, profile_name="medellin")
        out = capsys.readouterr().out
        assert "datadome_cookie_exists" in out
        assert "something went wrong" in out

    def test_skipped_shows_dash(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        results = self._one_skipped("datadome_cookie_not_expired")
        print_validation_report(ctx, results, profile_name="medellin")
        out = capsys.readouterr().out
        # Skipped rules are typically shown with a dash or "skip" marker
        assert "-" in out or "skip" in out.lower()

    def test_box_border_present(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        print_validation_report(ctx, self._all_pass_results(), profile_name="medellin")
        out = capsys.readouterr().out
        # Report is expected to have some box-drawing or separator characters
        assert any(ch in out for ch in ("─", "━", "═", "-", "=", "+", "|"))

    def test_tidal_visited_shown(self, tmp_path, capsys):
        from proxy_relay.profile_rules import BrowseContext, Remediation, RuleResult, print_validation_report

        # tidal_session_exists passed => "visited" indication
        ctx = self._ctx(tmp_path)
        results = self._all_pass_results()
        print_validation_report(ctx, results, profile_name="medellin")
        out = capsys.readouterr().out
        # Some word indicating TIDAL was visited (e.g. "yes", "tidal", "visited")
        assert "tidal" in out.lower() or "visited" in out.lower() or "yes" in out.lower()

    def test_account_email_shown_when_present(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path, account_email="alice@example.com")
        print_validation_report(ctx, self._all_pass_results(), profile_name="medellin")
        out = capsys.readouterr().out
        assert "alice@example.com" in out  # shown in Account line

    def test_remediation_section_shown_on_failure(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        results = self._one_failure("datadome_cookie_exists")
        print_validation_report(ctx, results, profile_name="medellin")
        out = capsys.readouterr().out
        # Remediation section or the remediation action name must appear
        assert "remediat" in out.lower() or "delete_profile" in out.lower() or "delete" in out.lower()

    def test_no_remediation_section_on_all_pass(self, tmp_path, capsys):
        from proxy_relay.profile_rules import print_validation_report

        ctx = self._ctx(tmp_path)
        print_validation_report(ctx, self._all_pass_results(), profile_name="medellin")
        out = capsys.readouterr().out
        # When all pass there should be no remediation actions listed
        # (We check that none of the destructive action strings appear)
        assert "delete_profile" not in out.lower()
        assert "delete_cookie" not in out.lower()
        assert "rotate_ip" not in out.lower()


# ===========================================================================
# J-RL9: immutable=1 removed from profile_rules.py Cookies DB open
# ===========================================================================


class TestNoImmutableInProfileRules:
    """J-RL9: _open_cookies_db uses mode=ro without immutable=1."""

    def test_no_immutable_in_source(self):
        import importlib.util
        spec = importlib.util.find_spec("proxy_relay.profile_rules")
        source = Path(spec.origin).read_text()
        assert "immutable=1" not in source

    def test_open_cookies_db_still_works(self, tmp_path):
        """Cookies DB opens successfully with mode=ro (no immutable)."""
        from proxy_relay.profile_rules import _open_cookies_db

        _make_cookies_db(tmp_path, [_datadome_cookie()])
        conn = _open_cookies_db(tmp_path)
        assert conn is not None
        conn.close()


# ===========================================================================
# TestTidalDomainsPublic: TIDAL_DOMAINS constant is public and well-formed
# ===========================================================================


class TestTidalDomainsPublic:
    """TIDAL_DOMAINS is exported as a public name (renamed from _TIDAL_DOMAINS)
    and contains the expected values."""

    def test_tidal_domains_is_importable(self):
        """``from proxy_relay.profile_rules import TIDAL_DOMAINS`` succeeds."""
        from proxy_relay.profile_rules import TIDAL_DOMAINS  # noqa: F401

    def test_tidal_domains_is_frozenset(self):
        """TIDAL_DOMAINS is a frozenset (immutable, hashable)."""
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        assert isinstance(TIDAL_DOMAINS, frozenset), (
            f"Expected frozenset, got {type(TIDAL_DOMAINS).__name__}"
        )

    def test_tidal_domains_contains_tidal_com(self):
        """TIDAL_DOMAINS contains the root domain 'tidal.com'."""
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        assert "tidal.com" in TIDAL_DOMAINS, (
            f"'tidal.com' not found in TIDAL_DOMAINS: {TIDAL_DOMAINS!r}"
        )
