"""DataDome trust warm-up for a proxy-relay browser profile.

Automates the following sequence:
  A. Start proxy relay server (auto-start if not running), resolve lang/tz/country
  B. Launch Chromium with CDP pointed directly at listen.tidal.com
  C. Enable Network, poll getAllCookies for datadome, write warmup-meta, disconnect CDP
  D. Hand the browser off to the user
  E. Wait for browser exit, then clean up

Both interactive (default) and ``--no-verify`` (scripted) modes are supported.
"""
from __future__ import annotations

import asyncio
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from proxy_relay import browse as _browse
from proxy_relay.capture import _find_free_port
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
from proxy_relay.tz import get_timezone_for_country

log = get_logger(__name__)

_DATADOME_POLL_INTERVAL = 2.0  # seconds between cookie polls
_BROWSER_POLL_INTERVAL = 1.0   # seconds between browser-alive checks


class WarmupSession:
    """Orchestrate a DataDome trust warm-up for a given proxy profile.

    Args:
        profile_name: proxy-st profile name (e.g. ``"medellin"``).
        workspace: Browser workspace identifier — combined with profile_name
            to form the browser profile directory ``{profile}+{workspace}``.
        timeout: Seconds to wait for the ``datadome`` cookie (phase C).
        host: Relay server bind address.
        port: Relay server port (0 = OS-assigned, resolved after start).
        chromium_path: Override Chromium binary path.
        lang: BCP 47 Accept-Language string for ``--lang`` flag.
        timezone: IANA timezone string for ``TZ`` env var.
        no_verify: If True, run in scripted mode (no Enter prompts, kill browser on done).
        config_path: Optional path to proxy-relay config file.
        log_level: Log level for auto-started server subprocess.
    """

    def __init__(
        self,
        *,
        profile_name: str,
        workspace: str = "default",
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
        self.workspace = workspace
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

    # ── Public entry point ─────────────────────────────────────────────────

    def run(self) -> int:
        """Run the warm-up and return an exit code (0 = success, 1 = failure)."""
        try:
            return self._run_sync()
        finally:
            self._cleanup()

    # ── Phase orchestration ────────────────────────────────────────────────

    def _run_sync(self) -> int:
        # Phase A — ensure server is running
        try:
            resolved_host, resolved_port = self._ensure_server()
        except BrowseError as exc:
            print(f"Failed to start proxy relay server: {exc}", file=__import__("sys").stderr)
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
            print(str(exc), file=__import__("sys").stderr)
            return 1

        # Allocate CDP port
        cdp_port = _find_free_port()

        # Build profile dir key
        profile_dir_key = f"{self.profile_name}+{self.workspace}"
        profile_dir = _browse.get_profile_dir(profile_dir_key, chromium_path=self.chromium_path)

        if self.lang:
            print(f"Language: --lang={self.lang}")
        if self.timezone:
            print(f"Timezone: {self.timezone}")
        print(f"Profile dir: {profile_dir}")
        print(f"CDP port: {cdp_port}")

        # Phase B — launch Chromium pointed at listen.tidal.com
        try:
            self._browser_handle = _browse.open_browser(
                "https://listen.tidal.com",
                proxy_host=self.host,
                proxy_port=self.port,
                profile_name=profile_dir_key,
                chromium_path=self.chromium_path,
                timezone=self.timezone,
                lang=self.lang,
                cdp_port=cdp_port,
            )
        except BrowseError as exc:
            print(f"Failed to launch Chromium: {exc}", file=__import__("sys").stderr)
            return 1

        print("Chromium launched — connecting CDP...")

        # Phases B+C+D run inside a single asyncio.run() call
        try:
            result = asyncio.run(self._run_cdp_phases(cdp_port))
        except Exception as exc:
            log.error("CDP phase failed: %s", exc)
            print(f"Warm-up failed: {exc}", file=__import__("sys").stderr)
            return 1

        if result != 0:
            return result

        # Phase D — CDP is disconnected; browser stays open
        print("Warm-up complete. Browser is open on listen.tidal.com.")
        print("Log in and close the window when done.")

        if self.no_verify:
            # Scripted mode — kill browser immediately
            if self._browser_handle is not None:
                _browse.close_browser(self._browser_handle)
            return 0

        # Interactive mode — wait for browser exit or relay death
        return self._wait_for_browser_exit()

    # ── CDP phases (async) ─────────────────────────────────────────────────

    async def _run_cdp_phases(self, cdp_port: int) -> int:
        """Run DataDome cookie poll (C) and write warmup-meta on success.

        Returns 0 on success, 1 on failure.
        """
        from proxy_relay.capture.cdp_client import CdpClient
        from proxy_relay.exceptions import CaptureError

        cdp = CdpClient()
        try:
            await cdp.connect(cdp_port, max_retries=15, retry_delay=1.5)
        except CaptureError as exc:
            log.error("CDP connect failed: %s", exc)
            # Kill browser — don't leave orphaned processes
            if self._browser_handle is not None:
                _browse.close_browser(self._browser_handle)
            raise

        try:
            # Phase C — poll for datadome cookie (browser already on listen.tidal.com)
            result = await self._phase_datadome(cdp)
            if result != 0:
                return result

            # Phase D — disconnect CDP (security + detection risk)
            await cdp.close()
            print("CDP disconnected.")
            return 0

        except Exception:
            await cdp.close()
            raise

    async def _phase_datadome(self, cdp: Any) -> int:
        """Enable Network and poll for the datadome cookie.

        The browser is already on listen.tidal.com (launched there in Phase B).
        Writes .warmup-meta.json after the cookie is confirmed.

        Returns 0 on success, 1 on failure/timeout.
        """
        from proxy_relay.exceptions import CaptureError

        print(f"\nPolling for datadome cookie (timeout: {self.timeout:.0f}s)...")
        try:
            await cdp.send("Network.enable")
        except CaptureError as exc:
            log.error("Network.enable failed: %s", exc)
            print(f"CDP Network.enable failed: {exc}", file=__import__("sys").stderr)
            return 1

        deadline = asyncio.get_event_loop().time() + self.timeout
        cookie_found = False

        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await cdp.send("Network.getAllCookies")
                cookies = result.get("cookies", [])
                for c in cookies:
                    if c.get("name") == "datadome" and "tidal.com" in c.get("domain", ""):
                        cookie_found = True
                        break
            except CaptureError as exc:
                log.warning("Cookie poll error: %s", exc)

            if cookie_found:
                break

            await asyncio.sleep(_DATADOME_POLL_INTERVAL)

        if not cookie_found:
            print(
                f"\nTimeout: datadome cookie not found after {self.timeout:.0f}s.",
                file=__import__("sys").stderr,
            )
            if self.no_verify:
                return 1
            # Interactive: keep browser open for debugging
            print("Browser remains open for debugging. Close it manually.")
            return 1

        print("datadome cookie found — DataDome trust established.")

        # Write warmup meta so profile_rules can validate future sessions
        if self._browser_handle is not None:
            try:
                from proxy_relay.profile_rules import write_warmup_meta

                write_warmup_meta(
                    Path(self._browser_handle.profile_dir),
                    self.host,
                    self.country or "",
                    self.account_email,
                )
                log.info("Warmup meta written to profile %s", self._browser_handle.profile_dir)
            except Exception as exc:
                log.warning("Failed to write warmup meta: %s", exc)

        return 0

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _js_eval(self, cdp: Any, expression: str) -> Any:
        """Evaluate a JS expression via Runtime.evaluate and return the value."""
        from proxy_relay.exceptions import CaptureError

        try:
            result = await cdp.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            return result.get("result", {}).get("value")
        except CaptureError as exc:
            log.debug("JS eval failed for %r: %s", expression, exc)
            return None

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
            # Browser exited?
            if self._browser_handle is not None:
                if self._browser_handle.process.poll() is not None:
                    log.info("Chromium exited")
                    return 0

            # Relay died?
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
    workspace: str = "default",
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
        workspace: Browser workspace identifier (combined with profile to form dir key).
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
        workspace=workspace,
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
