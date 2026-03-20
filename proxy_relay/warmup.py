"""DataDome trust warm-up for a proxy-relay browser profile.

Automates the following sequence:
  A. Start proxy relay server (auto-start if not running), resolve lang/tz/country
  B. Launch Chromium (no CDP) pointed directly at listen.tidal.com
  C. Poll Chromium Cookies SQLite for datadome cookie, write warmup-meta
  D. Hand the browser off to the user
  E. Wait for browser exit, then clean up

No CDP/remote-debugging-port is used — the browser runs completely clean to
avoid DataDome detecting the automation signal.

Both interactive (default) and ``--no-verify`` (scripted) modes are supported.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

from proxy_relay import browse as _browse
from proxy_relay import telemetry as _telemetry
from proxy_relay.exceptions import BrowseError
from proxy_relay.lang import get_language_for_country
from proxy_relay.logger import get_logger
from proxy_relay.pidfile import (
    is_process_running,
    pid_path_for,
    read_pid,
    read_status,
    status_path_for,
)
from proxy_relay.profile_rules import write_warmup_meta
from proxy_relay.tz import get_timezone_for_country

log = get_logger(__name__)

_DATADOME_POLL_INTERVAL = 2.0  # seconds between cookie polls
_BROWSER_POLL_INTERVAL = 1.0   # seconds between browser-alive checks


def _check_tidal_blocked(proxy_host: str, proxy_port: int) -> bool:
    """Return True if TIDAL is actively blocked by DataDome on this IP.

    Makes a direct HTTP request through the relay proxy to listen.tidal.com
    and checks for DataDome's block signature in the final URL or headers.
    """
    import requests

    proxies = {"https": f"http://{proxy_host}:{proxy_port}",
               "http":  f"http://{proxy_host}:{proxy_port}"}
    try:
        resp = requests.get(
            "https://listen.tidal.com/",
            proxies=proxies,
            timeout=15,
            allow_redirects=True,
        )
        final_url = resp.url.lower()
        blocked = (
            "datadome" in final_url
            or "captcha-delivery" in final_url
            or "interstitial" in final_url
            or resp.status_code == 403
        )
        log.debug("TIDAL block check: url=%s status=%s blocked=%s", resp.url, resp.status_code, blocked)
        return blocked
    except Exception as exc:
        log.debug("TIDAL block check failed (network error): %s", exc)
        return False


class WarmupSession:
    """Orchestrate a DataDome trust warm-up for a given proxy profile.

    Args:
        profile_name: proxy-st profile name (e.g. ``"medellin"``).
        timeout: Seconds to wait for the ``datadome`` cookie (phase C).
        host: Relay server bind address.
        port: Relay server port (0 = OS-assigned, resolved after start).
        chromium_path: Override Chromium binary path.
        lang: BCP 47 Accept-Language string for ``--lang`` flag.
        timezone: IANA timezone string for ``TZ`` env var.
        no_verify: If True, run in scripted mode (no Enter prompts, kill browser on done).
        config_path: Optional path to proxy-relay config file.
        log_level: Log level for auto-started server subprocess.
        account_email: Optional account email to record in warmup metadata.
    """

    def __init__(
        self,
        *,
        profile_name: str,
        timeout: float = 120.0,
        host: str = "127.0.0.1",
        port: int = 0,
        chromium_path: Path | None = None,
        lang: str | None = None,
        timezone: str | None = None,
        no_verify: bool = False,
        config_path: Path | None = None,
        log_level: str = "INFO",
        account_email: str | None = None,
    ) -> None:
        self.profile_name = profile_name
        self.timeout = timeout
        self.host = host
        self.port = port
        self.chromium_path = chromium_path
        self.lang = lang
        self.timezone = timezone
        self.no_verify = no_verify
        self.config_path = config_path
        self.log_level = log_level
        self.account_email = account_email
        self.country: str = ""

        self._server_proc: subprocess.Popen[bytes] | None = None
        self._auto_started: bool = False
        self._browser_handle: _browse.BrowserHandle | None = None
        self._run_id: str = _telemetry.new_run_id()

    # ── Public entry point ─────────────────────────────────────────────────

    def run(self) -> int:
        """Run the warm-up and return an exit code (0 = success, 1 = failure)."""
        try:
            return self._run_sync()
        finally:
            self._cleanup()

    # ── Phase orchestration ────────────────────────────────────────────────

    def _run_sync(self) -> int:
        import sys

        _telemetry.emit(
            "warmup.start",
            profile=self.profile_name,
            run_id=self._run_id,
            event_type="start",
            exit_ip="",
            country="",
            lang=self.lang or "",
            timezone=self.timezone or "",
            elapsed_s=0.0,
            reason="",
            account_email=self.account_email or "",
        )

        # Phase A — ensure server is running
        try:
            resolved_host, resolved_port = self._ensure_server()
        except BrowseError as exc:
            print(f"Failed to start proxy relay server: {exc}", file=sys.stderr)
            return 1

        self.host = resolved_host
        self.port = resolved_port

        # Resolve lang / timezone from server status if not already set
        status_data = read_status(status_path_for(self.profile_name)) or {}
        country = status_data.get("country", "")
        self.country = country
        if country:
            if self.lang is None:
                self.lang = get_language_for_country(country)
            if self.timezone is None:
                self.timezone = get_timezone_for_country(country)

        # Resolve Chromium binary
        try:
            if self.chromium_path is None:
                self.chromium_path = _browse.find_chromium()
        except BrowseError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        # Build profile dir key
        profile_dir_key = self.profile_name
        profile_dir = _browse.get_profile_dir(profile_dir_key, chromium_path=self.chromium_path)

        if self.lang:
            print(f"Language: --lang={self.lang}")
        if self.timezone:
            print(f"Timezone: {self.timezone}")
        print(f"Profile dir: {profile_dir}")

        # Phase B — launch Chromium WITHOUT CDP (clean browser, no automation signal)
        try:
            self._browser_handle = _browse.open_browser(
                "https://listen.tidal.com",
                proxy_host=self.host,
                proxy_port=self.port,
                profile_name=profile_dir_key,
                chromium_path=self.chromium_path,
                timezone=self.timezone,
                lang=self.lang,
            )
        except BrowseError as exc:
            print(f"Failed to launch Chromium: {exc}", file=sys.stderr)
            return 1

        print("Chromium launched — move the mouse and scroll to build DataDome trust...")
        print("Polling for datadome cookie in the background...")

        # Resolve actual exit IP (needed for warmup-meta)
        try:
            exit_ip = _browse.health_check(self.host, self.port)
            log.debug("Warmup exit IP: %s", exit_ip)
        except Exception:
            exit_ip = ""

        # Phase C — poll Cookies SQLite (no CDP needed)
        result = self._poll_for_datadome(profile_dir, exit_ip=exit_ip)
        if result != 0:
            return result

        # Phase D — browser stays open, hand off to user
        print("Warm-up complete. Browser is open on listen.tidal.com.")
        print("Log in and close the window when done.")

        if self.no_verify:
            if self._browser_handle is not None:
                _browse.close_browser(self._browser_handle)
            return 0

        return self._wait_for_browser_exit()

    # ── Cookie polling (SQLite) ────────────────────────────────────────────

    def _poll_for_datadome(self, profile_dir: Path, *, exit_ip: str = "") -> int:
        """Poll Chromium's Cookies SQLite until datadome cookie appears or timeout.

        Returns 0 on success, 1 on timeout or browser exit.
        """
        import sys

        cookies_db = profile_dir / "Default" / "Cookies"
        start = time.monotonic()
        deadline = start + self.timeout

        print(f"\nPolling for datadome cookie (timeout: {self.timeout:.0f}s)...")

        while time.monotonic() < deadline:
            # Check browser still alive
            if self._browser_handle is not None:
                if self._browser_handle.process.poll() is not None:
                    print("Browser exited before datadome cookie was found.", file=sys.stderr)
                    return 1

            if cookies_db.exists():
                try:
                    uri = f"file:{cookies_db}?mode=ro&immutable=1"
                    conn = sqlite3.connect(uri, uri=True)
                    try:
                        cur = conn.execute(
                            "SELECT 1 FROM cookies "
                            "WHERE name = 'datadome' AND host_key LIKE '%.tidal.com%' "
                            "LIMIT 1"
                        )
                        row = cur.fetchone()
                    finally:
                        conn.close()

                    if row:
                        print("datadome cookie found — DataDome trust established.")
                        self._write_meta(profile_dir, exit_ip=exit_ip)
                        _telemetry.emit(
                            "warmup.complete",
                            profile=self.profile_name,
                            run_id=self._run_id,
                            event_type="complete",
                            exit_ip=exit_ip,
                            country=self.country,
                            lang=self.lang or "",
                            timezone=self.timezone or "",
                            elapsed_s=round(time.monotonic() - start, 1),
                            reason="datadome cookie acquired",
                            account_email=self.account_email or "",
                        )
                        return 0
                except Exception as exc:
                    log.debug("Cookie poll error: %s", exc)

            time.sleep(_DATADOME_POLL_INTERVAL)

        elapsed = time.monotonic() - start
        print(
            f"\nTimeout: datadome cookie not found after {self.timeout:.0f}s.",
            file=sys.stderr,
        )
        poisoned = False
        if elapsed > 30:
            log.info("Elapsed %.0fs > 30s — checking if TIDAL is actively blocked...", elapsed)
            if _check_tidal_blocked(self.host, self.port):
                print("DataDome block confirmed — marking profile as poisoned.", file=sys.stderr)
                from proxy_relay.profile_rules import write_poisoned_marker
                write_poisoned_marker(profile_dir)
                poisoned = True
                _telemetry.emit(
                    "warmup.poisoned",
                    profile=self.profile_name,
                    run_id=self._run_id,
                    event_type="poisoned",
                    exit_ip=exit_ip,
                    country=self.country,
                    lang=self.lang or "",
                    timezone=self.timezone or "",
                    elapsed_s=round(elapsed, 1),
                    reason="DataDome block confirmed",
                    account_email=self.account_email or "",
                )
            else:
                log.info("TIDAL block check negative — likely a transient failure, not poisoning")
        if not poisoned:
            _telemetry.emit(
                "warmup.failed",
                profile=self.profile_name,
                run_id=self._run_id,
                event_type="failed",
                exit_ip=exit_ip,
                country=self.country,
                lang=self.lang or "",
                timezone=self.timezone or "",
                elapsed_s=round(elapsed, 1),
                reason="timeout" if elapsed >= self.timeout else "browser exited",
                account_email=self.account_email or "",
            )
        if not self.no_verify:
            print("Browser remains open for debugging. Close it manually.")
        return 1

    def _write_meta(self, profile_dir: Path, *, exit_ip: str = "") -> None:
        """Write .warmup-meta.json after datadome cookie confirmed."""
        try:
            write_warmup_meta(
                profile_dir,
                exit_ip or self.host,
                self.country or "",
                self.account_email,
            )
            log.info("Warmup meta written to profile %s", profile_dir)
        except Exception as exc:
            log.warning("Failed to write warmup meta: %s", exc)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _ensure_server(self) -> tuple[str, int]:
        """Return (host, port) of a running relay server, auto-starting if needed."""
        pid = read_pid(pid_path_for(self.profile_name))

        if pid is not None and is_process_running(pid):
            status_data = read_status(status_path_for(self.profile_name))
            if status_data is not None:
                host = status_data.get("host", self.host)
                port = status_data.get("port", self.port)
            else:
                host, port = self.host, self.port
            print(f"Using existing server for profile {self.profile_name!r} (PID {pid})")
            return host, port

        # Auto-start
        print(f"Starting server for profile {self.profile_name!r}...")
        server_proc = _browse.auto_start_server(
            self.profile_name,
            host=self.host,
            config_path=self.config_path,
            log_level=self.log_level,
        )
        self._server_proc = server_proc
        self._auto_started = True

        host, port = _browse.wait_for_server_ready(self.profile_name, server_proc)
        print(f"Server started (PID {server_proc.pid}, port {port})")
        return host, port

    def _wait_for_browser_exit(self) -> int:
        """Wait until browser exits or relay dies. Returns 0 on browser close."""
        relay_pid = read_pid(pid_path_for(self.profile_name))

        while True:
            if self._browser_handle is not None:
                if self._browser_handle.process.poll() is not None:
                    log.info("Chromium exited")
                    return 0

            if relay_pid is not None and not is_process_running(relay_pid):
                print("\nWarning: proxy relay stopped — browser has no proxy.")
                return 0

            time.sleep(_BROWSER_POLL_INTERVAL)

    def _cleanup(self) -> None:
        """Terminate Chromium and stop auto-started server."""
        if self._browser_handle is not None:
            try:
                if self._browser_handle.process.poll() is None:
                    _browse.close_browser(self._browser_handle)
            except Exception:
                pass

        if self._auto_started and self._server_proc is not None:
            _browse.auto_stop_server(self._server_proc, self.profile_name)


# ── Public entry point ─────────────────────────────────────────────────────


def run_warmup(
    profile_name: str,
    *,
    timeout: float = 120.0,
    browser: str | None = None,
    config_path: Path | None = None,
    lang: str | None = None,
    timezone: str | None = None,
    no_verify: bool = False,
    log_level: str = "INFO",
    account_email: str | None = None,
) -> int:
    """Run a DataDome trust warm-up session for the given proxy profile.

    Args:
        profile_name: proxy-st profile name.
        timeout: Seconds to wait for the datadome cookie.
        browser: Override Chromium binary path.
        config_path: Optional path to proxy-relay config file.
        lang: Override BCP 47 Accept-Language string.
        timezone: Override IANA timezone string.
        no_verify: Scripted mode — no Enter prompts, kill browser on done/fail.
        log_level: Log level for auto-started server subprocess.
        account_email: Optional account email to record in warmup metadata.

    Returns:
        0 on success, 1 on failure.
    """
    chromium_path: Path | None = None
    if browser:
        try:
            chromium_path = _browse.resolve_browser(browser)
        except BrowseError as exc:
            print(str(exc), file=__import__("sys").stderr)
            return 1

    session = WarmupSession(
        profile_name=profile_name,
        timeout=timeout,
        chromium_path=chromium_path,
        lang=lang,
        timezone=timezone,
        no_verify=no_verify,
        config_path=config_path,
        log_level=log_level,
        account_email=account_email,
    )
    return session.run()
