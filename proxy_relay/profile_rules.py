"""Rule-based browser profile validation for proxy-relay.

Evaluates a set of rules against a browser profile before launch.
All reporting and remediation helpers live here to keep cli.py lean.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COOKIES_DB_PATH = "Default/Cookies"
_WARMUP_META_FILE = ".warmup-meta.json"
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01

_TIDAL_DOMAINS = frozenset({"tidal.com", "listen.tidal.com", "login.tidal.com"})

# ---------------------------------------------------------------------------
# Enums and dataclasses
# ---------------------------------------------------------------------------


class Remediation(Enum):
    NONE = "none"
    DELETE_COOKIE = "delete_cookie"
    DELETE_PROFILE = "delete_profile"
    ROTATE_IP = "rotate_ip"


@dataclass
class BrowseContext:
    """Context passed to all rules during validation."""

    profile_dir: Path
    exit_ip: str
    country: str
    lang: str | None = None
    timezone: str | None = None
    account_email: str | None = None


@dataclass
class RuleResult:
    """Result of a single rule evaluation."""

    passed: bool
    skipped: bool
    rule_name: str
    reason: str
    remediation: Remediation


# ---------------------------------------------------------------------------
# Rule Protocol
# ---------------------------------------------------------------------------


class Rule(Protocol):
    """Protocol for validation rules."""

    name: str
    remediation: Remediation

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        ...


# ---------------------------------------------------------------------------
# SQLite helpers (private)
# ---------------------------------------------------------------------------


def _open_cookies_db(profile_dir: Path) -> sqlite3.Connection | None:
    """Open Chromium's Cookies SQLite in read-only mode. Returns None on failure."""
    db_path = profile_dir / _COOKIES_DB_PATH
    if not db_path.exists():
        return None
    uri = f"file:{db_path}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        # Force WAL checkpoint is not needed in read-only mode; just verify it opens.
        conn.execute("SELECT 1 FROM cookies LIMIT 1")
        return conn
    except Exception as exc:
        log.debug("Failed to open Cookies DB at %s: %s", db_path, exc)
        return None


def _read_datadome_cookie(profile_dir: Path) -> dict | None:
    """Return datadome cookie row dict or None.

    Keys: name, value, expires_utc.
    Reads directly from Chromium's profile Cookies database.
    """
    conn = _open_cookies_db(profile_dir)
    if conn is None:
        return None
    try:
        cur = conn.execute(
            "SELECT name, value, expires_utc FROM cookies "
            "WHERE host_key LIKE '%.tidal.com%' AND name = 'datadome'"
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {"name": row[0], "value": row[1], "expires_utc": row[2]}
    except Exception as exc:
        log.debug("Failed to read datadome cookie: %s", exc)
        return None
    finally:
        conn.close()


def _chromium_expires_to_unix(expires_utc: int) -> float:
    """Convert Chromium expires_utc (microseconds since 1601-01-01) to Unix timestamp."""
    return (expires_utc / 1_000_000) - _CHROMIUM_EPOCH_OFFSET


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


class ProfileExists:
    """Rule 1: Verify the profile directory exists and is non-empty."""

    name = "profile_exists"
    remediation = Remediation.DELETE_PROFILE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        if not ctx.profile_dir.exists():
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="profile directory does not exist",
                remediation=self.remediation,
            )
        try:
            has_contents = any(ctx.profile_dir.iterdir())
        except Exception:
            has_contents = False

        if not has_contents:
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="profile directory is empty",
                remediation=self.remediation,
            )
        return RuleResult(
            passed=True,
            skipped=False,
            rule_name=self.name,
            reason="profile directory exists and is non-empty",
            remediation=Remediation.NONE,
        )


class ProfileNotCorrupted:
    """Rule 2: Verify the Cookies database opens without errors."""

    name = "profile_not_corrupted"
    remediation = Remediation.DELETE_PROFILE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        cookies_path = ctx.profile_dir / _COOKIES_DB_PATH
        if not cookies_path.exists():
            return RuleResult(
                passed=True,
                skipped=True,
                rule_name=self.name,
                reason="no Cookies DB (fresh profile — not corrupted)",
                remediation=Remediation.NONE,
            )
        conn = _open_cookies_db(ctx.profile_dir)
        if conn is None:
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="Cookies DB exists but failed to open (corrupted)",
                remediation=self.remediation,
            )
        conn.close()
        return RuleResult(
            passed=True,
            skipped=False,
            rule_name=self.name,
            reason="Cookies DB is readable",
            remediation=Remediation.NONE,
        )


class DatadomeCookieExists:
    """Rule 3: Verify the datadome cookie is present for .tidal.com."""

    name = "datadome_cookie_exists"
    remediation = Remediation.DELETE_PROFILE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        cookies_path = ctx.profile_dir / _COOKIES_DB_PATH
        if not cookies_path.exists():
            return RuleResult(
                passed=False,
                skipped=True,
                rule_name=self.name,
                reason="no Cookies DB (fresh profile)",
                remediation=Remediation.NONE,
            )
        cookie = _read_datadome_cookie(ctx.profile_dir)
        if cookie is None:
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="datadome cookie not found for .tidal.com",
                remediation=self.remediation,
            )
        return RuleResult(
            passed=True,
            skipped=False,
            rule_name=self.name,
            reason="datadome cookie present",
            remediation=Remediation.NONE,
        )


class DatadomeCookieNotExpired:
    """Rule 4: Verify the datadome cookie has not expired."""

    name = "datadome_cookie_not_expired"
    remediation = Remediation.DELETE_COOKIE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        cookie = _read_datadome_cookie(ctx.profile_dir)
        if cookie is None:
            return RuleResult(
                passed=False,
                skipped=True,
                rule_name=self.name,
                reason="no datadome cookie to check",
                remediation=Remediation.NONE,
            )
        expires_utc: int = cookie.get("expires_utc", 0)
        if expires_utc == 0:
            # Session cookie — never expires
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason="session cookie (no expiry)",
                remediation=Remediation.NONE,
            )
        unix_ts = _chromium_expires_to_unix(expires_utc)
        if unix_ts > time.time():
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason="cookie is valid (not expired)",
                remediation=Remediation.NONE,
            )
        from datetime import datetime, timezone as _tz

        exp_str = datetime.fromtimestamp(unix_ts, tz=_tz.utc).strftime("%Y-%m-%d %H:%M")
        return RuleResult(
            passed=False,
            skipped=False,
            rule_name=self.name,
            reason=f"expired {exp_str}",
            remediation=self.remediation,
        )


class IPMatchesCookie:
    """Rule 5: Verify the current exit IP matches the IP used during warmup.

    When this rule fails, the cookie is also invalid (issued for old IP),
    so DELETE_COOKIE remediation must also run alongside ROTATE_IP.
    """

    name = "ip_matches_cookie"
    remediation = Remediation.ROTATE_IP

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        meta_path = ctx.profile_dir / _WARMUP_META_FILE
        meta = read_warmup_meta(ctx.profile_dir)
        if meta is None:
            return RuleResult(
                passed=True,
                skipped=True,
                rule_name=self.name,
                reason="no warmup metadata",
                remediation=Remediation.NONE,
            )
        issued_ip = meta.get("issued_ip", "")
        if issued_ip == ctx.exit_ip:
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason=f"exit IP matches ({ctx.exit_ip})",
                remediation=Remediation.NONE,
            )
        return RuleResult(
            passed=False,
            skipped=False,
            rule_name=self.name,
            reason=f"was {issued_ip}, now {ctx.exit_ip}",
            remediation=self.remediation,
        )


class TIDALSessionExists:
    """Rule 6: Check whether the profile has visited TIDAL (info only).

    Failure has NONE remediation — this is informational only.
    """

    name = "tidal_session_exists"
    remediation = Remediation.NONE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        conn = _open_cookies_db(ctx.profile_dir)
        if conn is None:
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="profile has not visited TIDAL",
                remediation=Remediation.NONE,
            )
        try:
            cur = conn.execute(
                "SELECT 1 FROM cookies "
                "WHERE host_key LIKE '%.tidal.com%' AND name = 'app_lang' LIMIT 1"
            )
            row = cur.fetchone()
        except Exception:
            row = None
        finally:
            conn.close()

        if row is not None:
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason="profile has visited TIDAL",
                remediation=Remediation.NONE,
            )
        return RuleResult(
            passed=False,
            skipped=False,
            rule_name=self.name,
            reason="profile has not visited TIDAL",
            remediation=Remediation.NONE,
        )


class LanguageConsistentWithCountry:
    """Rule 7: Verify the warmup country matches the current proxy country."""

    name = "language_consistent"
    remediation = Remediation.DELETE_PROFILE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        meta = read_warmup_meta(ctx.profile_dir)
        if meta is None:
            return RuleResult(
                passed=True,
                skipped=True,
                rule_name=self.name,
                reason="no warmup metadata",
                remediation=Remediation.NONE,
            )
        issued_country = meta.get("issued_country", "").lower()
        current_country = ctx.country.lower()
        if issued_country == current_country:
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason=f"country consistent ({current_country.upper()})",
                remediation=Remediation.NONE,
            )
        return RuleResult(
            passed=False,
            skipped=False,
            rule_name=self.name,
            reason=f"issued for {issued_country.upper()}, now {current_country.upper()}",
            remediation=self.remediation,
        )


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


def _skip(rule: Rule) -> RuleResult:
    """Build a skipped RuleResult for a rule."""
    return RuleResult(
        passed=False,
        skipped=True,
        rule_name=rule.name,
        reason="skipped",
        remediation=Remediation.NONE,
    )


@dataclass
class RuleRegistry:
    """Ordered collection of rules with skip-dependency logic."""

    _rules: list[Rule] = field(default_factory=list)

    def add(self, rule: Rule) -> "RuleRegistry":
        self._rules.append(rule)
        return self

    def remove(self, rule_name: str) -> "RuleRegistry":
        """Remove a rule by name. No-op if the rule is not present.

        Args:
            rule_name: The ``name`` attribute of the rule to remove.

        Returns:
            Self (for chaining).
        """
        self._rules = [r for r in self._rules if r.name != rule_name]
        return self

    def evaluate_all(self, ctx: BrowseContext) -> list[RuleResult]:
        """Evaluate all rules in order, respecting skip dependencies.

        Args:
            ctx: BrowseContext with profile and proxy info.

        Returns:
            List of RuleResult in evaluation order.
        """
        results: list[RuleResult] = []
        profile_ok = True
        cookies_db_ok = True
        cookies_db_present = True  # False when DB simply doesn't exist (fresh profile)
        datadome_ok = True
        meta_exists = (ctx.profile_dir / _WARMUP_META_FILE).exists()

        for rule in self._rules:
            # Determine skip conditions per rule
            if rule.name == "profile_not_corrupted" and not profile_ok:
                results.append(_skip(rule))
                continue
            if rule.name == "datadome_cookie_exists" and not profile_ok:
                results.append(_skip(rule))
                continue
            # tidal_session_exists: skip if profile missing OR no Cookies DB at all
            if rule.name == "tidal_session_exists" and (
                not profile_ok or not cookies_db_present
            ):
                results.append(_skip(rule))
                continue
            if rule.name == "datadome_cookie_not_expired" and (
                not datadome_ok or not cookies_db_ok
            ):
                results.append(_skip(rule))
                continue
            if rule.name == "ip_matches_cookie" and (not datadome_ok or not meta_exists):
                results.append(_skip(rule))
                continue
            if rule.name == "language_consistent" and not meta_exists:
                results.append(_skip(rule))
                continue

            result = rule.evaluate(ctx)
            results.append(result)

            # Track state for skip logic
            if rule.name == "profile_exists" and not result.passed:
                profile_ok = False
            if rule.name == "profile_not_corrupted":
                if result.skipped:
                    # DB doesn't exist — no Cookies file at all
                    cookies_db_present = False
                elif not result.passed:
                    cookies_db_ok = False
            if rule.name == "datadome_cookie_exists" and not result.passed and not result.skipped:
                datadome_ok = False

        return results


def default_registry() -> RuleRegistry:
    """Build and return the standard rule registry with all 7 rules."""
    reg = RuleRegistry()
    reg.add(ProfileExists())
    reg.add(ProfileNotCorrupted())
    reg.add(DatadomeCookieExists())
    reg.add(DatadomeCookieNotExpired())
    reg.add(IPMatchesCookie())
    reg.add(TIDALSessionExists())
    reg.add(LanguageConsistentWithCountry())
    return reg


# ---------------------------------------------------------------------------
# Warmup meta helpers
# ---------------------------------------------------------------------------


def write_warmup_meta(
    profile_dir: Path,
    exit_ip: str,
    country: str,
    account_email: str | None = None,
) -> None:
    """Write .warmup-meta.json to profile_dir after successful warm-up.

    Args:
        profile_dir: Browser profile directory.
        exit_ip: Proxy exit IP at time of warmup.
        country: ISO alpha-2 country code of the proxy.
        account_email: Optional account email to record.
    """
    from datetime import datetime, UTC

    meta = {
        "issued_ip": exit_ip,
        "issued_country": country,
        "issued_at": datetime.now(UTC).isoformat(),
        "account_email": account_email,
    }
    path = profile_dir / _WARMUP_META_FILE
    path.write_text(json.dumps(meta, indent=2))
    log.debug("Wrote warmup meta to %s", path)


def read_warmup_meta(profile_dir: Path) -> dict | None:
    """Read .warmup-meta.json from profile_dir.

    Args:
        profile_dir: Browser profile directory.

    Returns:
        Parsed dict, or None if missing or invalid JSON.
    """
    path = profile_dir / _WARMUP_META_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.debug("Failed to read warmup meta at %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# TIDAL URL detection
# ---------------------------------------------------------------------------


def is_tidal_url(url: str | None) -> bool:
    """Return True if url targets a TIDAL domain, or if url is None (default session).

    Args:
        url: URL string to check, or None.

    Returns:
        True if the URL is TIDAL-related or None (default browse session).
    """
    if not url:
        return True
    return any(domain in url for domain in _TIDAL_DOMAINS)


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------


def print_validation_report(
    ctx: BrowseContext,
    results: list[RuleResult],
    profile_name: str,
) -> None:
    """Print a formatted profile validation report to stdout.

    Always shown — both all-pass and fail cases.

    Args:
        ctx: BrowseContext with profile and proxy info.
        results: List of RuleResult from RuleRegistry.evaluate_all().
        profile_name: Human-readable proxy profile name.
    """
    home = Path.home()
    profile_str = str(ctx.profile_dir)
    if profile_str.startswith(str(home)):
        profile_str = "~" + profile_str[len(str(home)):]

    # Find TIDAL session result
    tidal_result = next((r for r in results if r.rule_name == "tidal_session_exists"), None)
    if tidal_result is None:
        tidal_line = "TIDAL     : - unknown"
    elif tidal_result.skipped:
        tidal_line = "TIDAL     : - unknown"
    elif tidal_result.passed:
        email = ctx.account_email or "(account unknown)"
        tidal_line = f"TIDAL     : \u2713 visited \u2014 {email}"
    else:
        tidal_line = "TIDAL     : \u2717 never visited"

    print("\u2554" + "\u2550" * 62 + "\u2557")
    print("\u2551  proxy-relay browse \u2014 profile validation" + " " * 21 + "\u2551")
    print("\u255a" + "\u2550" * 62 + "\u255d")
    print()
    print(f"  Profile   : {profile_name}")
    print(f"  Exit IP   : {ctx.exit_ip}")
    if ctx.lang:
        print(f"  Language  : {ctx.lang}")
    if ctx.timezone:
        print(f"  Timezone  : {ctx.timezone}")
    print(f"  Directory : {profile_str}/")
    print(f"  {tidal_line}")
    print()
    print("  Rules:")

    failed_results = []
    for r in results:
        if r.skipped:
            print(f"    -  {r.rule_name:<40s}  \u2014 skipped")
        elif r.passed:
            print(f"    \u2713 {r.rule_name}")
        else:
            print(f"    \u2717 {r.rule_name:<40s}  \u2014 {r.reason}")
            failed_results.append(r)

    if not failed_results:
        print()
        print("  All rules passed \u2014 launching browser.")
        return

    # Collect unique remediations (preserving insertion order)
    seen: set[Remediation] = set()
    ordered_remeds: list[Remediation] = []
    needs_warmup = False

    for r in failed_results:
        if r.remediation not in seen and r.remediation != Remediation.NONE:
            seen.add(r.remediation)
            ordered_remeds.append(r.remediation)
            needs_warmup = True
        # IP mismatch also invalidates cookie
        if r.rule_name == "ip_matches_cookie" and Remediation.DELETE_COOKIE not in seen:
            seen.add(Remediation.DELETE_COOKIE)
            ordered_remeds.append(Remediation.DELETE_COOKIE)

    print()
    print("  Remediations:")
    for rem in ordered_remeds:
        if rem == Remediation.DELETE_COOKIE:
            print("    \u2192 Delete expired datadome cookie from profile")
        elif rem == Remediation.DELETE_PROFILE:
            print("    \u2192 Delete browser profile (will be recreated by warm-up)")
        elif rem == Remediation.ROTATE_IP:
            print("    \u2192 Rotate proxy IP")

    if needs_warmup:
        print("    \u2192 Run warm-up on listen.tidal.com to acquire new datadome cookie")


# ---------------------------------------------------------------------------
# Remediation executor
# ---------------------------------------------------------------------------


def execute_remediations(
    failed: list[RuleResult],
    ctx: BrowseContext,
    relay_pid: int | None,
    profile_name: str,
    host: str,
    port: int,
) -> BrowseContext:
    """Execute remediation actions for failed rules. Returns updated BrowseContext.

    Args:
        failed: List of failed (non-skipped) RuleResults.
        ctx: Current BrowseContext.
        relay_pid: PID of the running proxy-relay server, or None.
        profile_name: Proxy-st profile name.
        host: Relay server host.
        port: Relay server port.

    Returns:
        Updated BrowseContext (exit_ip may be updated after IP rotation).
    """
    # Collect unique remediations
    seen: set[Remediation] = set()
    ordered: list[Remediation] = []
    for r in failed:
        if r.remediation not in seen and r.remediation != Remediation.NONE:
            seen.add(r.remediation)
            ordered.append(r.remediation)
        # IP mismatch also invalidates cookie
        if r.rule_name == "ip_matches_cookie" and Remediation.DELETE_COOKIE not in seen:
            seen.add(Remediation.DELETE_COOKIE)
            ordered.append(Remediation.DELETE_COOKIE)

    from proxy_relay import browse as _browse

    if Remediation.ROTATE_IP in seen:
        log.info("Rotating proxy IP (sending SIGUSR1 to PID %s)...", relay_pid)
        if relay_pid is not None:
            try:
                os.kill(relay_pid, signal.SIGUSR1)
            except ProcessLookupError:
                log.warning("Relay process %d not found — cannot send SIGUSR1", relay_pid)

        time.sleep(2)
        # Poll until IP changes (30s max, 2s interval)
        old_ip = ctx.exit_ip
        deadline = time.time() + 30
        new_ip = old_ip
        while time.time() < deadline:
            try:
                new_ip = _browse.health_check(host, port)
                if new_ip != old_ip:
                    log.info("IP rotated: %s -> %s", old_ip, new_ip)
                    break
            except Exception as exc:
                log.debug("Health check during rotation poll: %s", exc)
            time.sleep(2)

        if new_ip == old_ip:
            log.warning("IP did not change after rotation (still %s)", old_ip)

        ctx = BrowseContext(
            profile_dir=ctx.profile_dir,
            exit_ip=new_ip,
            country=ctx.country,
            lang=ctx.lang,
            timezone=ctx.timezone,
            account_email=ctx.account_email,
        )
        # Rotated IP invalidates cookie — ensure DELETE_COOKIE also runs
        if Remediation.DELETE_COOKIE not in seen:
            seen.add(Remediation.DELETE_COOKIE)
            ordered.append(Remediation.DELETE_COOKIE)

    if Remediation.DELETE_COOKIE in seen:
        db_path = ctx.profile_dir / _COOKIES_DB_PATH
        if db_path.exists():
            log.info("Deleting datadome cookie from profile %s", profile_name)
            try:
                conn = sqlite3.connect(str(db_path))
                try:
                    conn.execute(
                        "DELETE FROM cookies WHERE name='datadome' AND host_key LIKE '%.tidal.com%'"
                    )
                    conn.commit()
                    log.debug("datadome cookie deleted")
                finally:
                    conn.close()
            except Exception as exc:
                log.warning("Failed to delete datadome cookie: %s", exc)

    if Remediation.DELETE_PROFILE in seen:
        log.info("Deleting browser profile at %s", ctx.profile_dir)
        shutil.rmtree(ctx.profile_dir, ignore_errors=True)

    return ctx
