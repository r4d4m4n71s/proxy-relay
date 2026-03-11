"""Browser launch and supervision for proxy-relay.

Finds a local Chromium installation, verifies the proxy chain via a health
check, and supervises the browser process alongside the running relay daemon.
Optionally auto-rotates the upstream proxy at a configurable interval.
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
from pathlib import Path

from proxy_relay.config import CONFIG_DIR
from proxy_relay.exceptions import BrowseError
from proxy_relay.logger import get_logger
from proxy_relay.pidfile import is_process_running, read_status, send_signal, status_path_for

log = get_logger(__name__)

_HEALTH_CHECK_TIMEOUT: float = 60.0
_PID_POLL_INTERVAL: float = 2.0
_CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "/snap/bin/chromium",
    "chromium",
    "chromium-browser",
    "google-chrome",
)
BROWSER_PROFILES_DIR: Path = CONFIG_DIR / "browser-profiles"

# Snap Chromium can only write to ~/snap/chromium/common/ due to sandbox restrictions.
_SNAP_PROFILES_DIR: Path = Path.home() / "snap" / "chromium" / "common" / "proxy-relay-profiles"


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

    try:
        with urllib.request.urlopen(health_url, timeout=_HEALTH_CHECK_TIMEOUT) as response:
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
    if chromium_path is not None and _is_snap_chromium(chromium_path):
        base = _SNAP_PROFILES_DIR
        log.debug("Snap Chromium detected — using %s for profiles", base)
        _cleanup_ghost_profile(profile_name)
    else:
        base = BROWSER_PROFILES_DIR

    profile_dir = base / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Browser profile directory: %s", profile_dir)
    return profile_dir


def _cleanup_ghost_profile(profile_name: str) -> None:
    """Remove empty ghost directory left at the non-Snap location.

    When Snap Chromium is detected, earlier runs may have created empty
    subdirectories under ``~/.config/proxy-relay/browser-profiles/``.
    This removes them (only if empty) to avoid user confusion.
    """
    ghost = BROWSER_PROFILES_DIR / profile_name
    try:
        if ghost.is_dir():
            ghost.rmdir()  # only succeeds if empty
            log.debug("Removed empty ghost profile dir: %s", ghost)
            # Also remove parent if empty
            if BROWSER_PROFILES_DIR.is_dir() and not any(BROWSER_PROFILES_DIR.iterdir()):
                BROWSER_PROFILES_DIR.rmdir()
                log.debug("Removed empty browser-profiles dir: %s", BROWSER_PROFILES_DIR)
    except OSError:
        pass  # not empty or permission issue — leave it


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

    log.info("Auto-starting server for profile %r: %s", profile_name, " ".join(cmd))

    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )  # noqa: S603
    except OSError as exc:
        raise BrowseError(
            f"Failed to start server subprocess for profile {profile_name!r}: {exc}"
        ) from exc


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
            raise BrowseError(
                f"Server process exited with code {server_proc.returncode} "
                f"before becoming ready (profile: {profile_name!r})"
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
    raise BrowseError(
        f"Server for profile {profile_name!r} did not become ready "
        f"within {timeout:.0f}s"
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
    ) -> None:
        self._chromium_path = chromium_path
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._profile_dir = profile_dir
        self._relay_pid = relay_pid
        self._rotate_interval_min = rotate_interval_min
        self._timezone = timezone

        self._stop_event = threading.Event()
        self._chromium_proc: subprocess.Popen[bytes] | None = None
        self._relay_died: bool = False

    def run(self) -> int:
        """Launch Chromium and supervise until exit.

        Returns:
            Exit code: 0 if browser closed normally, 1 if relay died,
            130 if interrupted by Ctrl-C.
        """
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

            # Main loop: wait for browser exit or stop event
            while True:
                if self._stop_event.wait(timeout=1.0):
                    # Relay died — kill the browser
                    log.warning("Stopping browser because relay is gone")
                    self._cleanup_chromium()
                    return 1

                if self._chromium_proc.poll() is not None:
                    rc = self._chromium_proc.returncode
                    log.info("Chromium exited with code %d", rc)
                    self._stop_event.set()
                    return 0

        except KeyboardInterrupt:
            log.info("Interrupted by user — shutting down browser")
            self._cleanup_chromium()
            return 130

    def _start_chromium(self) -> subprocess.Popen[bytes]:
        """Launch the Chromium process.

        Returns:
            The Popen handle for the browser process.

        Raises:
            BrowseError: If the binary cannot be executed.
        """
        cmd = [
            str(self._chromium_path),
            f"--proxy-server=http://{self._proxy_host}:{self._proxy_port}",
            f"--user-data-dir={self._profile_dir}",
            "--start-maximized",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-sync",
        ]
        env: dict[str, str] | None = None
        if self._timezone:
            env = {**os.environ, "TZ": self._timezone}
            log.info("Setting browser timezone: TZ=%s", self._timezone)

        log.debug("Launching Chromium: %s", " ".join(cmd))

        try:
            return subprocess.Popen(cmd, env=env)  # noqa: S603
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
