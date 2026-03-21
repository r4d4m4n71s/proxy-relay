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
from datetime import UTC
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
_POISONED_FILE = ".poisoned"
_CHROMIUM_EPOCH_OFFSET = 11_644_473_600  # seconds between 1601-01-01 and 1970-01-01

TIDAL_DOMAINS = frozenset({"tidal.com", "listen.tidal.com", "login.tidal.com"})

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
    uri = f"file:{db_path}?mode=ro"
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


class ProfileNotPoisoned:
    """Rule 3: Verify the profile has not been flagged as DataDome-poisoned.

    Written by warmup when the datadome cookie was not acquired after timeout
    AND a direct TIDAL check confirms the IP is blocked.
    Remediation: DELETE_PROFILE — start completely fresh on a new IP.
    """

    name = "profile_not_poisoned"
    remediation = Remediation.DELETE_PROFILE

    def evaluate(self, ctx: BrowseContext) -> RuleResult:
        poisoned_path = ctx.profile_dir / _POISONED_FILE
        if poisoned_path.exists():
            return RuleResult(
                passed=False,
                skipped=False,
                rule_name=self.name,
                reason="profile flagged as DataDome-poisoned",
                remediation=Remediation.DELETE_PROFILE,
            )
        return RuleResult(
            passed=True,
            skipped=False,
            rule_name=self.name,
            reason="profile not poisoned",
            remediation=Remediation.NONE,
        )


class DatadomeCookieExists:
    """Rule 3: Verify the datadome cookie is present for .tidal.com."""

    name = "datadome_cookie_exists"
    remediation = Remediation.DELETE_COOKIE

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

    # PR-8: warn when cookie is older than this many days (server-side revocation risk)
    _FRESHNESS_DAYS: int = 7

    @staticmethod
    def _cookie_age_days(expires_chromium: int, now: float | None = None) -> float | None:
        """Estimate cookie age in days from its expiry timestamp.

        Assumes DataDome cookies have ~365-day lifetime.
        Returns None if estimation is not possible.

        Args:
            expires_chromium: Chromium expires_utc value (microseconds since 1601-01-01).
            now: Current Unix timestamp override (for testing). Defaults to time.time().

        Returns:
            Estimated age in days, or None if the cookie has an unusual lifetime.
        """
        if now is None:
            now = time.time()
        expires_unix = (expires_chromium / 1_000_000) - _CHROMIUM_EPOCH_OFFSET
        remaining_days = (expires_unix - now) / 86400
        # DataDome default lifetime is ~365 days
        issued_days_ago = 365 - remaining_days
        if issued_days_ago < 0:
            return None  # cookie has unusual lifetime
        return issued_days_ago

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
            # PR-8: cookie is valid — check freshness and warn if stale
            age_days = self._cookie_age_days(expires_utc)
            if age_days is not None and age_days > self._FRESHNESS_DAYS:
                return RuleResult(
                    passed=True,
                    skipped=False,
                    rule_name=self.name,
                    remediation=Remediation.NONE,
                    reason=(
                        f"Cookie valid but {age_days:.0f} days old — consider re-warmup "
                        f"(server-side revocation possible after {self._FRESHNESS_DAYS} days)"
                    ),
                )
            return RuleResult(
                passed=True,
                skipped=False,
                rule_name=self.name,
                reason="cookie is valid (not expired)",
                remediation=Remediation.NONE,
            )
        from datetime import datetime

        exp_str = datetime.fromtimestamp(unix_ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        return RuleResult(
            passed=False,
            skipped=False,
            rule_name=self.name,
            reason=f"expired {exp_str}",
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

    def add(self, rule: Rule) -> RuleRegistry:
        self._rules.append(rule)
        return self

    def remove(self, rule_name: str) -> RuleRegistry:
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
        datadome_ok = True
        poisoned = False

        for rule in self._rules:
            # Determine skip conditions per rule
            if rule.name == "profile_not_corrupted" and not profile_ok:
                results.append(_skip(rule))
                continue
            if rule.name == "profile_not_poisoned" and not profile_ok:
                results.append(_skip(rule))
                continue
            if rule.name == "datadome_cookie_exists" and (not profile_ok or poisoned):
                results.append(_skip(rule))
                continue
            if rule.name == "datadome_cookie_not_expired" and (
                not datadome_ok or not cookies_db_ok or poisoned
            ):
                results.append(_skip(rule))
                continue

            result = rule.evaluate(ctx)
            results.append(result)

            # Track state for skip logic
            if rule.name == "profile_exists" and not result.passed:
                profile_ok = False
            if rule.name == "profile_not_poisoned" and not result.passed:
                poisoned = True
            if rule.name == "profile_not_corrupted" and not result.passed and not result.skipped:
                cookies_db_ok = False
            if rule.name == "datadome_cookie_exists" and not result.passed and not result.skipped:
                datadome_ok = False

        return results


def default_registry() -> RuleRegistry:
    """Build and return the standard rule registry with all 5 rules."""
    reg = RuleRegistry()
    reg.add(ProfileExists())
    reg.add(ProfileNotCorrupted())
    reg.add(ProfileNotPoisoned())
    reg.add(DatadomeCookieExists())
    reg.add(DatadomeCookieNotExpired())
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
    from datetime import UTC, datetime

    meta = {
        "issued_ip": exit_ip,
        "issued_country": country,
        "issued_at": datetime.now(UTC).isoformat(),
        "account_email": account_email,
    }
    path = profile_dir / _WARMUP_META_FILE
    path.write_text(json.dumps(meta, indent=2))
    log.debug("Wrote warmup meta to %s", path)


def write_poisoned_marker(profile_dir: Path) -> None:
    """Write .poisoned sentinel file to profile_dir.

    Called by warmup when the datadome cookie was not acquired and a direct
    TIDAL check confirms the IP is blocked.
    """
    try:
        (profile_dir / _POISONED_FILE).touch()
        log.info("Poisoned marker written to %s", profile_dir)
    except Exception as exc:
        log.warning("Failed to write poisoned marker: %s", exc)


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
    """Return True if *url* explicitly targets a TIDAL domain.

    Returns False when *url* is None (no ``--start-url`` given), so
    TIDAL-specific validation is skipped for general browsing sessions.

    Args:
        url: URL string to check, or None.

    Returns:
        True only if the URL contains a known TIDAL domain.
    """
    if not url:
        return False
    return any(domain in url for domain in TIDAL_DOMAINS)


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
    if ctx.account_email:
        print(f"  Account   : {ctx.account_email}")
    print()
    print("  Rules:")

    failed_results = []
    for r in results:
        if r.skipped:
            print(f"    -  {r.rule_name:<40s}  \u2014 skipped")
        elif r.passed:
            # PR-8: display a warning indicator for stale-cookie advisory results
            if r.reason and "consider re-warmup" in r.reason.lower():
                print(f"    \u2713 {r.rule_name:<40s}  \u2014 \u26a0 {r.reason}")
            else:
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
        # Poisoned profile also needs IP rotation
        if r.rule_name == "profile_not_poisoned" and Remediation.ROTATE_IP not in seen:
            seen.add(Remediation.ROTATE_IP)
            ordered_remeds.append(Remediation.ROTATE_IP)

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
        print("      (move the mouse and scroll when the browser opens)")


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
        # Poisoned profile also needs IP rotation
        if r.rule_name == "profile_not_poisoned" and Remediation.ROTATE_IP not in seen:
            seen.add(Remediation.ROTATE_IP)
            ordered.append(Remediation.ROTATE_IP)

    from proxy_relay import browse as _browse

    if Remediation.ROTATE_IP in seen:
        log.info("Rotating proxy IP (sending SIGUSR1 to PID %s)...", relay_pid)
        if relay_pid is not None:
            try:
                os.kill(relay_pid, signal.SIGUSR1)
            except ProcessLookupError:
                log.warning("Relay process %d not found — cannot send SIGUSR1", relay_pid)

        time.sleep(2)
        # Poll until IP changes (30s max, 2s interval).
        # J-RL13: show a progress indicator so the user knows we are waiting
        # (the poll blocks the CLI for up to 30 seconds).
        old_ip = ctx.exit_ip
        deadline = time.time() + 30
        new_ip = old_ip
        print("  Waiting for IP rotation", end="", flush=True)
        while time.time() < deadline:
            try:
                new_ip = _browse.health_check(host, port)
                if new_ip != old_ip:
                    print()  # newline after dots on success
                    log.info("IP rotated: %s -> %s", old_ip, new_ip)
                    break
            except Exception as exc:
                log.debug("Health check during rotation poll: %s", exc)
            print(".", end="", flush=True)
            time.sleep(2)
        else:
            print()  # newline after dots on timeout

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
