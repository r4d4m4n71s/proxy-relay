"""Browser launch and supervision for proxy-relay.

Finds a local Chromium installation, verifies the proxy chain via a health
check, and supervises the browser process alongside the running relay daemon.
Optionally auto-rotates the upstream proxy at a configurable interval.
"""
from __future__ import annotations

import shutil
import signal
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

from proxy_relay.config import CONFIG_DIR
from proxy_relay.exceptions import BrowseError
from proxy_relay.logger import get_logger
from proxy_relay.pidfile import is_process_running, send_signal

log = get_logger(__name__)

_HEALTH_CHECK_URL: str = "http://icanhazip.com"
_HEALTH_CHECK_TIMEOUT: float = 15.0
_PID_POLL_INTERVAL: float = 2.0
_CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "/snap/bin/chromium",
    "chromium",
    "chromium-browser",
    "google-chrome",
)
BROWSER_PROFILES_DIR: Path = CONFIG_DIR / "browser-profiles"


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
    """Verify the proxy chain is working by fetching the exit IP.

    Uses ``urllib.request`` with a proxy handler to route the request
    through the local relay.

    Args:
        proxy_host: Local proxy bind address.
        proxy_port: Local proxy bind port.

    Returns:
        The exit IP address (response body, stripped).

    Raises:
        BrowseError: If the health check request fails for any reason.
    """
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    proxy_handler = urllib.request.ProxyHandler(
        {"http": proxy_url, "https": proxy_url}
    )
    opener = urllib.request.build_opener(proxy_handler)

    try:
        with opener.open(_HEALTH_CHECK_URL, timeout=_HEALTH_CHECK_TIMEOUT) as response:
            body = response.read().decode("utf-8").strip()
            log.debug("Health check returned exit IP: %s", body)
            return body
    except urllib.error.HTTPError as exc:
        raise BrowseError(
            f"Health check failed with HTTP {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise BrowseError(f"Health check failed — proxy unreachable: {exc.reason}") from exc
    except (OSError, TimeoutError) as exc:
        raise BrowseError(f"Health check failed: {exc}") from exc


def get_profile_dir(profile_name: str) -> Path:
    """Return (and create) a browser profile directory.

    Args:
        profile_name: Name used as the subdirectory under
            ``BROWSER_PROFILES_DIR``.

    Returns:
        Path to the profile directory (guaranteed to exist).
    """
    profile_dir = BROWSER_PROFILES_DIR / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.debug("Browser profile directory: %s", profile_dir)
    return profile_dir


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
    ) -> None:
        self._chromium_path = chromium_path
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._profile_dir = profile_dir
        self._relay_pid = relay_pid
        self._rotate_interval_min = rotate_interval_min

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
        log.debug("Launching Chromium: %s", " ".join(cmd))

        try:
            return subprocess.Popen(cmd)  # noqa: S603
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
