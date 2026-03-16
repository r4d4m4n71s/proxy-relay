"""Browser launch and supervision for proxy-relay.

Finds a local Chromium installation, verifies the proxy chain via a health
check, and supervises the browser process alongside the running relay daemon.
Optionally auto-rotates the upstream proxy at a configurable interval.

Public API
----------
- :func:`can_launch_browser` — headless / SSH environment check
- :func:`open_browser` — launch Chromium with anti-leak flags, return handle
- :func:`open_browser_tab` — open a URL in an existing browser session
- :func:`close_browser` — terminate a browser launched by :func:`open_browser`
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from proxy_relay.config import CONFIG_DIR
from proxy_relay.exceptions import BrowseError
from proxy_relay.logger import get_logger
from proxy_relay.pidfile import (
    _validate_profile_name,
    is_process_running,
    read_status,
    send_signal,
    status_path_for,
)

log = get_logger(__name__)

_HEALTH_CHECK_TIMEOUT: float = 60.0
_PID_POLL_INTERVAL: float = 2.0
_CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "/snap/bin/chromium",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
    "brave-browser-stable",
    "vivaldi",
    "vivaldi-stable",
    "opera",
)
BROWSER_PROFILES_DIR: Path = CONFIG_DIR / "browser-profiles"

# Snap Chromium can only write to ~/snap/chromium/common/ due to sandbox restrictions.
_SNAP_PROFILES_DIR: Path = Path.home() / "snap" / "chromium" / "common" / "proxy-relay-profiles"


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class BrowserHandle:
    """Handle to a launched browser process and its profile.

    Returned by :func:`open_browser`.  Pass to :func:`close_browser` or
    :func:`open_browser_tab` to manage the session lifecycle.
    """

    process: subprocess.Popen[bytes]
    profile_dir: Path
    chromium_path: Path


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def can_launch_browser() -> bool:
    """Check whether a graphical browser can be launched.

    Returns ``False`` in headless environments (no ``DISPLAY`` or
    ``WAYLAND_DISPLAY``), SSH sessions (``SSH_CLIENT`` set), or when no
    Chromium binary is found.
    """
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if not has_display:
        log.debug("No DISPLAY or WAYLAND_DISPLAY — cannot launch browser")
        return False

    if os.environ.get("SSH_CLIENT"):
        log.debug("SSH_CLIENT set — skipping browser launch")
        return False

    try:
        find_chromium()
    except BrowseError:
        log.debug("No supported Chromium browser found on PATH")
        return False

    return True


# ---------------------------------------------------------------------------
# Chromium argument builder (shared by open_browser + BrowseSupervisor)
# ---------------------------------------------------------------------------


def _chrome_args(
    chromium_path: Path,
    profile_dir: Path,
    *,
    proxy_host: str | None = None,
    proxy_port: int | None = None,
    timezone: str | None = None,
    cdp_port: int | None = None,
    start_url: str | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Build Chromium command-line and environment with anti-leak flags.

    Includes:
    - ``--user-data-dir`` (profile isolation)
    - ``--disable-webrtc-stun-origin`` + ``--enforce-webrtc-ip-permission-check``
    - ``--host-resolver-rules`` (DNS leak prevention, only with proxy)
    - ``--proxy-server`` (when *proxy_port* is not ``None``)
    - ``--no-first-run``, ``--disable-default-apps``, ``--disable-sync``
    - ``--start-maximized``
    - ``TZ`` environment variable (when *timezone* is not ``None``)
    - ``--remote-debugging-port`` (when *cdp_port* is not ``None``)
    - *start_url* as the initial page (positional arg, appended last)

    Args:
        chromium_path: Path to the Chromium binary.
        profile_dir: Browser profile directory.
        proxy_host: Local proxy bind address.
        proxy_port: Local proxy port. ``None`` means no proxy flags.
        timezone: IANA timezone name for ``TZ`` env override.
        cdp_port: TCP port for ``--remote-debugging-port``. ``None`` means
            no CDP flags.
        start_url: URL to open on launch. ``None`` means browser default.

    Returns:
        Tuple of (command_args, env_dict_or_None).
    """
    cmd = [
        str(chromium_path),
        f"--user-data-dir={profile_dir}",
        "--start-maximized",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-webrtc-stun-origin",
        "--enforce-webrtc-ip-permission-check",
    ]

    if proxy_port is not None:
        host = proxy_host or "127.0.0.1"
        cmd.append(f"--proxy-server=http://{host}:{proxy_port}")
        cmd.append('--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1')

    if cdp_port is not None:
        cmd.append(f"--remote-debugging-port={cdp_port}")

    # Start URL must be the last positional argument
    if start_url:
        cmd.append(start_url)

    env: dict[str, str] | None = None
    if timezone:
        env = {**os.environ, "TZ": timezone}

    return cmd, env


# ---------------------------------------------------------------------------
# Public browser lifecycle API
# ---------------------------------------------------------------------------


def open_browser(
    url: str,
    *,
    proxy_host: str = "127.0.0.1",
    proxy_port: int | None = None,
    profile_name: str = "default",
    chromium_path: Path | None = None,
    timezone: str | None = None,
) -> BrowserHandle:
    """Launch Chromium configured for proxied browsing.

    Lower-level API — caller controls the lifecycle via :func:`close_browser`.

    Args:
        url: URL to open.
        proxy_host: Local proxy bind address.
        proxy_port: Local proxy port.  ``None`` means launch without proxy
            flags (direct connection).
        profile_name: Browser profile name (persistent, Snap-aware).
        chromium_path: Explicit Chromium binary.  Auto-detected if ``None``.
        timezone: IANA timezone for ``TZ`` env var spoofing.

    Returns:
        :class:`BrowserHandle` for lifecycle management.

    Raises:
        BrowseError: If no Chromium is found or launch fails.
    """
    if chromium_path is None:
        chromium_path = find_chromium()

    profile_dir = get_profile_dir(profile_name, chromium_path=chromium_path)

    cmd, env = _chrome_args(
        chromium_path,
        profile_dir,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        timezone=timezone,
    )
    cmd.append(url)

    if timezone:
        log.info("Setting browser timezone: TZ=%s", timezone)
    log.debug("Launching Chromium: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # noqa: S603
    except OSError as exc:
        raise BrowseError(f"Failed to launch Chromium at {chromium_path}: {exc}") from exc

    return BrowserHandle(process=proc, profile_dir=profile_dir, chromium_path=chromium_path)


def open_browser_tab(handle: BrowserHandle, url: str) -> None:
    """Open a new URL in an existing Chromium session (new tab).

    Re-invoking Chromium with the same ``--user-data-dir`` opens a new tab
    in the running instance rather than starting a second window.

    Args:
        handle: The running browser session.
        url: The URL to open.
    """
    cmd = [
        str(handle.chromium_path),
        f"--user-data-dir={handle.profile_dir}",
        url,
    ]
    log.debug("Opening tab in existing session: %s", " ".join(cmd))
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # noqa: S603
    except OSError as exc:
        log.warning("Failed to open browser tab for %s: %s", url, exc)


def close_browser(handle: BrowserHandle) -> None:
    """Terminate the browser process.

    Sends SIGTERM, waits up to 5 seconds, then SIGKILL if needed.

    .. note::

       Snap Chromium's AppArmor confinement blocks ``os.kill()`` from the
       parent process.  In that case, ``terminate()`` / ``kill()`` may
       silently fail and child processes (zygote, GPU, renderer) will
       linger until the Snap sandbox cleans them up (typically seconds).
       This is a known Snap limitation, not a proxy-relay bug.

    Does NOT remove the profile directory (profiles are persistent).
    Safe to call multiple times.  Never raises.

    Args:
        handle: The browser session to close.
    """
    try:
        if handle.process.poll() is not None:
            return
        handle.process.terminate()
        handle.process.wait(timeout=5)
        log.debug("Chromium terminated gracefully")
    except subprocess.TimeoutExpired:
        log.warning("Chromium did not exit in 5s — force killing")
        try:
            handle.process.kill()
            handle.process.wait(timeout=5)
        except OSError:
            log.warning("Failed to kill Chromium process %d", handle.process.pid)
    except OSError:
        log.debug("Chromium process already exited")


def find_chromium() -> Path:
    """Locate a Chromium or Chrome executable on the system.

    Checks each candidate in ``_CHROMIUM_CANDIDATES``. Absolute paths are
    tested via ``Path.exists()``, bare names via ``shutil.which()``.

    Returns:
        Path to the first found browser executable.

    Raises:
        BrowseError: If no Chromium/Chrome binary is found.
    """
    for candidate in _CHROMIUM_CANDIDATES:
        candidate_path = Path(candidate)
        if candidate_path.is_absolute():
            if candidate_path.exists():
                log.debug("Found Chromium at absolute path: %s", candidate_path)
                return candidate_path
        else:
            resolved = shutil.which(candidate)
            if resolved is not None:
                log.debug("Found Chromium via PATH: %s", resolved)
                return Path(resolved)

    raise BrowseError(
        "Chromium not found. Install chromium or google-chrome and ensure it is on PATH."
    )


def resolve_browser(name_or_path: str) -> Path:
    """Resolve an explicit browser name or path to an absolute path.

    If *name_or_path* is an absolute path, it is checked for existence.
    Otherwise it is looked up via ``shutil.which()``.

    Args:
        name_or_path: Browser binary name (e.g., ``"brave-browser"``) or
            absolute path (e.g., ``"/usr/bin/brave-browser"``).

    Returns:
        Absolute path to the browser binary.

    Raises:
        BrowseError: If the binary is not found.
    """
    candidate = Path(name_or_path)
    if candidate.is_absolute():
        if candidate.exists():
            log.debug("Resolved browser at absolute path: %s", candidate)
            return candidate
        raise BrowseError(f"Browser not found at {candidate}")

    resolved = shutil.which(name_or_path)
    if resolved is not None:
        log.debug("Resolved browser via PATH: %s -> %s", name_or_path, resolved)
        return Path(resolved)

    raise BrowseError(
        f"Browser {name_or_path!r} not found on PATH. "
        f"Provide the full path or install the browser."
    )


def health_check(proxy_host: str, proxy_port: int) -> str:
    """Verify the proxy chain by calling the server's internal health endpoint.

    Sends a plain HTTP GET to ``http://proxy_host:proxy_port/__health``.
    The server handles upstream verification internally — including
    automatic rotation and retry when the upstream is unreachable.

    Args:
        proxy_host: Local proxy bind address.
        proxy_port: Local proxy bind port.

    Returns:
        The exit IP address reported by the server.

    Raises:
        BrowseError: If the health endpoint returns an error or is unreachable.
    """
    import json

    health_url = f"http://{proxy_host}:{proxy_port}/__health"

    # Explicitly disable env proxy vars — this request goes to the local
    # proxy-relay server, not through any upstream proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    try:
        with opener.open(health_url, timeout=_HEALTH_CHECK_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
            exit_ip = data.get("exit_ip", "")
            log.debug("Health check returned exit IP: %s", exit_ip)
            return exit_ip
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
            error_msg = data.get("error", str(exc))
        except Exception:
            error_msg = f"HTTP {exc.code}: {exc.reason}"
        raise BrowseError(error_msg) from exc
    except urllib.error.URLError as exc:
        raise BrowseError(
            f"proxy-relay server unreachable at {health_url}: {exc.reason}"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise BrowseError(f"proxy-relay health endpoint error: {exc}") from exc


def _is_snap_chromium(chromium_path: Path) -> bool:
    """Return True if the Chromium binary is a Snap package.

    Snap Chromium lives under ``/snap/`` and its sandbox restricts
    filesystem writes to ``~/snap/chromium/common/``.

    Args:
        chromium_path: Path to the Chromium binary.

    Returns:
        True if the binary path is under ``/snap/``.
    """
    return str(chromium_path).startswith("/snap/")


def get_profile_dir(profile_name: str, chromium_path: Path | None = None) -> Path:
    """Return (and create) a browser profile directory.

    When Chromium is a Snap package, the profile is placed under
    ``~/snap/chromium/common/proxy-relay-profiles/`` because the Snap
    sandbox prevents writing to ``~/.config/proxy-relay/``.

    If a Snap redirect occurs and an empty ghost directory exists at the
    non-Snap location, it is removed automatically.

    Args:
        profile_name: Name used as the subdirectory.
        chromium_path: Path to the Chromium binary.  Used to detect Snap
            and select the appropriate base directory.

    Returns:
        Path to the profile directory (guaranteed to exist).
    """
    _validate_profile_name(profile_name)

    if chromium_path is not None and _is_snap_chromium(chromium_path):
        base = _SNAP_PROFILES_DIR
        log.debug("Snap Chromium detected — using %s for profiles", base)
        _cleanup_ghost_profile(profile_name)
    else:
        base = BROWSER_PROFILES_DIR

    profile_dir = base / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Browser profile directory: %s", profile_dir)

    # Create a convenience symlink at the non-Snap location so users can
    # find profiles under ~/.config/proxy-relay/browser-profiles/<name>.
    if base == _SNAP_PROFILES_DIR:
        _create_profile_symlink(profile_name, profile_dir)

    return profile_dir


def _cleanup_ghost_profile(profile_name: str) -> None:
    """Remove empty ghost directory left at the non-Snap location.

    When Snap Chromium is detected, earlier runs may have created empty
    subdirectories under ``~/.config/proxy-relay/browser-profiles/``.
    This removes them (only if empty) to avoid user confusion.
    """
    ghost = BROWSER_PROFILES_DIR / profile_name
    try:
        if ghost.is_symlink():
            return  # preserve convenience symlinks
        if ghost.is_dir():
            ghost.rmdir()  # only succeeds if empty
            log.debug("Removed empty ghost profile dir: %s", ghost)
    except OSError:
        pass  # not empty or permission issue — leave it


def _create_profile_symlink(profile_name: str, target: Path) -> None:
    """Create a symlink at the non-Snap location pointing to the Snap profile.

    ``~/.config/proxy-relay/browser-profiles/<name>`` → Snap profile dir.
    Existing symlinks are updated if the target changed; real directories
    are left untouched.
    """
    BROWSER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    link = BROWSER_PROFILES_DIR / profile_name
    try:
        if link.is_symlink():
            if link.resolve() == target.resolve():
                return  # already correct
            link.unlink()
        elif link.exists():
            log.debug("Skipping symlink — real directory exists: %s", link)
            return

        link.symlink_to(target)
        log.debug("Created profile symlink: %s -> %s", link, target)
    except OSError as exc:
        log.warning("Could not create profile symlink %s -> %s: %s", link, target, exc)


def list_profiles() -> list[str]:
    """Return the names of all existing browser profiles.

    Scans both the Snap and non-Snap profile directories and returns a
    deduplicated, sorted list of profile names.
    """
    names: set[str] = set()
    for base in (BROWSER_PROFILES_DIR, _SNAP_PROFILES_DIR):
        if base.is_dir():
            for child in base.iterdir():
                if child.is_dir() or child.is_symlink():
                    names.add(child.name)
    return sorted(names)


def delete_profile(profile_name: str) -> list[str]:
    """Delete a browser profile and its convenience symlink.

    Removes the profile data directory from both the Snap location and the
    non-Snap location (real directory or symlink).

    Args:
        profile_name: Name of the profile to delete.

    Returns:
        List of paths that were removed (for user feedback).

    Raises:
        BrowseError: If the profile does not exist in any location.
    """
    _validate_profile_name(profile_name)

    removed: list[str] = []
    snap_profile = _SNAP_PROFILES_DIR / profile_name
    default_profile = BROWSER_PROFILES_DIR / profile_name

    # Remove symlink at default location first (before removing target)
    if default_profile.is_symlink():
        default_profile.unlink()
        removed.append(str(default_profile) + " (symlink)")
        log.debug("Removed profile symlink: %s", default_profile)
    elif default_profile.is_dir():
        shutil.rmtree(default_profile)
        removed.append(str(default_profile))
        log.debug("Removed profile directory: %s", default_profile)

    # Remove actual data at Snap location
    if snap_profile.is_dir():
        shutil.rmtree(snap_profile)
        removed.append(str(snap_profile))
        log.debug("Removed Snap profile directory: %s", snap_profile)

    if not removed:
        raise BrowseError(f"Profile {profile_name!r} not found")

    return removed


_SERVER_READY_POLL_INTERVAL: float = 0.5
_SERVER_READY_TIMEOUT: float = 30.0


def auto_start_server(
    profile_name: str,
    host: str = "127.0.0.1",
    config_path: Path | None = None,
    log_level: str = "INFO",
) -> subprocess.Popen[bytes]:
    """Start a proxy-relay server subprocess for the given profile.

    Launches ``proxy-relay start --profile <name> --port 0 --host <host>``
    as a child process. The server binds to an OS-assigned free port and
    writes the actual port to ``{profile_name}.status.json``.

    Args:
        profile_name: proxy-st profile name.
        host: Local bind address.
        config_path: Optional path to config file.
        log_level: Log level for the server subprocess.

    Returns:
        The Popen handle for the server process.

    Raises:
        BrowseError: If the subprocess cannot be started.
    """
    cmd = [
        sys.executable, "-m", "proxy_relay",
        "start",
        "--profile", profile_name,
        "--port", "0",
        "--host", host,
        "--log-level", log_level,
    ]
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])

    # Remove stale status file so wait_for_server_ready doesn't read
    # leftover data from a previous run.
    stale_status = status_path_for(profile_name)
    try:
        stale_status.unlink(missing_ok=True)
    except OSError:
        pass

    log.info("Auto-starting server for profile %r: %s", profile_name, " ".join(cmd))

    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )  # noqa: S603
    except OSError as exc:
        raise BrowseError(
            f"Failed to start server subprocess for profile {profile_name!r}: {exc}"
        ) from exc


def _read_stderr(proc: subprocess.Popen[bytes], max_bytes: int = 4096) -> str:
    """Read and decode stderr from a finished subprocess.

    Safe to call only after the process has exited (``poll() is not None``).
    Reads up to *max_bytes* to avoid blocking on an unexpectedly large stream.

    Args:
        proc: A subprocess whose stderr was opened as ``subprocess.PIPE``.
        max_bytes: Maximum bytes to read (default 4096).

    Returns:
        Decoded stderr text, stripped of surrounding whitespace.
        Empty string if stderr is ``None`` or unreadable.
    """
    if proc.stderr is None:
        return ""
    try:
        raw = proc.stderr.read(max_bytes)
        return raw.decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def wait_for_server_ready(
    profile_name: str,
    server_proc: subprocess.Popen[bytes],
    timeout: float = _SERVER_READY_TIMEOUT,
) -> tuple[str, int]:
    """Wait for the auto-started server to write its status file.

    Polls for the profile's status.json to appear and contain a valid
    host and port.  Also checks that the server process has not exited
    unexpectedly.

    Args:
        profile_name: proxy-st profile name.
        server_proc: The server subprocess handle.
        timeout: Maximum seconds to wait.

    Returns:
        Tuple of (host, port) read from the status file.

    Raises:
        BrowseError: If the server exits early, times out, or the
            status file is invalid.
    """
    status_path = status_path_for(profile_name)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        # Check if server died
        if server_proc.poll() is not None:
            stderr_output = _read_stderr(server_proc)
            detail = f"\n  stderr: {stderr_output}" if stderr_output else ""
            raise BrowseError(
                f"Server process exited with code {server_proc.returncode} "
                f"before becoming ready (profile: {profile_name!r}){detail}"
            )

        # Check for status file
        status = read_status(status_path)
        if status is not None:
            port = status.get("port", 0)
            host = status.get("host", "127.0.0.1")
            if isinstance(port, int) and port > 0:
                log.info(
                    "Server ready for profile %r at %s:%d",
                    profile_name, host, port,
                )
                return host, port

        time.sleep(_SERVER_READY_POLL_INTERVAL)

    # Timeout — kill the server and raise
    server_proc.terminate()
    try:
        server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc.kill()
    stderr_output = _read_stderr(server_proc)
    detail = f"\n  stderr: {stderr_output}" if stderr_output else ""
    raise BrowseError(
        f"Server for profile {profile_name!r} did not become ready "
        f"within {timeout:.0f}s{detail}"
    )


def auto_stop_server(
    server_proc: subprocess.Popen[bytes],
    profile_name: str,
) -> None:
    """Stop an auto-started server subprocess gracefully.

    Sends SIGTERM, waits up to 5 seconds, then SIGKILL if needed.

    Args:
        server_proc: The server subprocess handle.
        profile_name: proxy-st profile name (for log messages).
    """
    if server_proc.poll() is not None:
        log.debug(
            "Server for profile %r already exited (code %d)",
            profile_name, server_proc.returncode,
        )
        return

    log.info("Stopping auto-started server for profile %r (PID %d)", profile_name, server_proc.pid)
    try:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        log.debug("Server terminated gracefully")
    except subprocess.TimeoutExpired:
        log.warning("Server did not exit in 5s — force killing")
        server_proc.kill()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("Server did not respond to SIGKILL within 5s")


class BrowseSupervisor:
    """Supervise a Chromium process running through the proxy relay.

    Monitors both the browser and the relay daemon. If the relay dies,
    the browser is terminated. Optionally sends ``SIGUSR1`` to the relay
    at a fixed interval to trigger upstream proxy rotation.

    Args:
        chromium_path: Path to the Chromium/Chrome binary.
        proxy_host: Local proxy bind address.
        proxy_port: Local proxy bind port.
        profile_dir: Path to the browser user-data directory.
        relay_pid: PID of the running proxy-relay daemon.
        rotate_interval_min: Minutes between auto-rotations (0 = disabled).
        timezone: IANA timezone name to set on the Chromium process
            (e.g., ``"Europe/Berlin"``).  When set, Chromium's ``TZ``
            environment variable is overridden so JavaScript reports a
            timezone consistent with the proxy exit country.  None means
            inherit the system timezone.
        capture_session: Optional :class:`~proxy_relay.capture.CaptureSession`
            instance.  When set, a CDP remote debugging port is allocated,
            Chromium is launched with ``--remote-debugging-port``, and the
            capture session is run in a daemon thread alongside the browser.
    """

    def __init__(
        self,
        *,
        chromium_path: Path,
        proxy_host: str,
        proxy_port: int,
        profile_dir: Path,
        relay_pid: int,
        rotate_interval_min: int = 30,
        timezone: str | None = None,
        capture_session: object | None = None,
        start_url: str | None = None,
    ) -> None:
        self._chromium_path = chromium_path
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._profile_dir = profile_dir
        self._relay_pid = relay_pid
        self._rotate_interval_min = rotate_interval_min
        self._timezone = timezone
        self._capture = capture_session
        self._start_url = start_url
        self._cdp_port: int | None = (
            capture_session.cdp_port if capture_session is not None else None  # type: ignore[union-attr]
        )

        self._stop_event = threading.Event()
        self._chromium_proc: subprocess.Popen[bytes] | None = None
        self._relay_died: bool = False
        self._capture_thread: threading.Thread | None = None

    def run(self) -> int:
        """Launch Chromium and supervise until exit.

        Returns:
            Exit code: 0 if browser closed normally, 1 if relay died,
            130 if interrupted by Ctrl-C.
        """
        exit_code: int = 0
        try:
            self._chromium_proc = self._start_chromium()

            relay_thread = threading.Thread(
                target=self._poll_relay, name="relay-poll", daemon=True
            )
            relay_thread.start()

            if self._rotate_interval_min > 0:
                rotate_thread = threading.Thread(
                    target=self._rotation_loop, name="auto-rotate", daemon=True
                )
                rotate_thread.start()

            # Start capture thread if a capture session is configured
            if self._capture is not None and self._cdp_port is not None:
                self._capture_thread = threading.Thread(
                    target=self._capture.run_in_thread,  # type: ignore[union-attr]
                    args=(self._cdp_port,),
                    name="cdp-capture",
                    daemon=True,
                )
                self._capture_thread.start()
                log.info("Capture thread started (CDP port %d)", self._cdp_port)

            # Main loop: wait for browser exit or stop event
            while True:
                if self._stop_event.wait(timeout=1.0):
                    # Relay died — kill the browser
                    log.warning("Stopping browser because relay is gone")
                    self._cleanup_chromium()
                    exit_code = 1
                    break

                if self._chromium_proc.poll() is not None:
                    rc = self._chromium_proc.returncode
                    log.info("Chromium exited with code %d", rc)
                    self._stop_event.set()
                    exit_code = 0
                    break

        except KeyboardInterrupt:
            log.info("Interrupted by user — shutting down browser")
            self._cleanup_chromium()
            exit_code = 130

        finally:
            # Stop capture session if running
            if self._capture is not None:
                try:
                    self._capture.request_stop()  # type: ignore[union-attr]
                except Exception:
                    log.debug("Error requesting capture stop", exc_info=True)
            if self._capture_thread is not None:
                self._capture_thread.join(timeout=10)
                if self._capture_thread.is_alive():
                    log.warning("Capture thread did not exit within 10s")

        return exit_code

    def _start_chromium(self) -> subprocess.Popen[bytes]:
        """Launch the Chromium process.

        Returns:
            The Popen handle for the browser process.

        Raises:
            BrowseError: If the binary cannot be executed.
        """
        cmd, env = _chrome_args(
            self._chromium_path,
            self._profile_dir,
            proxy_host=self._proxy_host,
            proxy_port=self._proxy_port,
            timezone=self._timezone,
            cdp_port=self._cdp_port,
            start_url=self._start_url,
        )

        if self._timezone:
            log.info("Setting browser timezone: TZ=%s", self._timezone)
        log.debug("Launching Chromium: %s", " ".join(cmd))

        try:
            return subprocess.Popen(  # noqa: S603
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BrowseError(f"Failed to launch Chromium at {self._chromium_path}: {exc}") from exc

    def _poll_relay(self) -> None:
        """Background thread: check that the relay daemon is still alive."""
        while not self._stop_event.wait(timeout=_PID_POLL_INTERVAL):
            if not is_process_running(self._relay_pid):
                log.error("proxy-relay (PID %d) is no longer running", self._relay_pid)
                self._relay_died = True
                self._stop_event.set()
                return

    def _rotation_loop(self) -> None:
        """Background thread: send SIGUSR1 to the relay at fixed intervals."""
        if self._rotate_interval_min <= 0:
            return
        interval_sec = self._rotate_interval_min * 60
        while not self._stop_event.wait(timeout=interval_sec):
            log.info("Auto-rotating upstream proxy (every %d min)", self._rotate_interval_min)
            send_signal(self._relay_pid, signal.SIGUSR1)

    def _cleanup_chromium(self, proc: subprocess.Popen[bytes] | None = None) -> None:
        """Terminate the Chromium process gracefully, force-kill if needed.

        Args:
            proc: Process to terminate. Defaults to ``self._chromium_proc``.
        """
        target = proc if proc is not None else self._chromium_proc
        if target is None or target.poll() is not None:
            return

        log.info("Terminating Chromium (PID %d)", target.pid)
        try:
            target.terminate()
            target.wait(timeout=5)
            log.debug("Chromium terminated gracefully")
        except subprocess.TimeoutExpired:
            log.warning("Chromium did not exit in 5s — force killing")
            target.kill()
            try:
                target.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("Chromium did not respond to SIGKILL within 5s")
            log.debug("Chromium force-killed")
