"""PID file and status file utilities for proxy-relay daemon management.

Provides functions to write/read PID files, check whether a process is
running, send signals to a running daemon, and persist server status as
JSON for the ``status`` CLI subcommand.

Each proxy-relay instance is identified by its proxy-st profile name.
PID and status files are scoped per profile:

    ``~/.config/proxy-relay/{profile}.pid``
    ``~/.config/proxy-relay/{profile}.status.json``
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proxy_relay.config import CONFIG_DIR
from proxy_relay.logger import get_logger

log = get_logger(__name__)

# Legacy single-instance paths (kept for backward compatibility in tests).
PID_PATH: Path = CONFIG_DIR / "proxy-relay.pid"
STATUS_PATH: Path = CONFIG_DIR / "status.json"

# Default profile name when none is specified.
_DEFAULT_PROFILE: str = "browse"

# Track paths already registered with atexit to avoid duplicate registrations
# when write_pid() is called multiple times for the same path.
# Protected by _atexit_lock because write_pid() may be called from a
# thread-pool thread via asyncio.to_thread() (F-RL6).
_atexit_lock = threading.Lock()
_atexit_registered: set[Path] = set()

# F-RL25: Track status file paths registered for atexit cleanup.
_status_atexit_registered: set[Path] = set()


def _validate_profile_name(profile: str) -> None:
    """Reject profile names containing path separators or special characters.

    Raises:
        ValueError: If the profile name is unsafe for use in filenames.
    """
    if not profile or "/" in profile or "\\" in profile or ".." in profile or "\0" in profile:
        raise ValueError(
            f"Invalid profile name {profile!r}: must not contain path separators or '..'"
        )


def pid_path_for(profile: str) -> Path:
    """Return the PID file path for a given profile.

    Args:
        profile: proxy-st profile name.

    Returns:
        Path to ``~/.config/proxy-relay/{profile}.pid``.

    Raises:
        ValueError: If the profile name contains path separators.
    """
    _validate_profile_name(profile)
    return CONFIG_DIR / f"{profile}.pid"


def status_path_for(profile: str) -> Path:
    """Return the status file path for a given profile.

    Args:
        profile: proxy-st profile name.

    Returns:
        Path to ``~/.config/proxy-relay/{profile}.status.json``.

    Raises:
        ValueError: If the profile name contains path separators.
    """
    _validate_profile_name(profile)
    return CONFIG_DIR / f"{profile}.status.json"


# ------------------------------------------------------------------
# PID file operations
# ------------------------------------------------------------------


def write_pid(path: Path = PID_PATH) -> None:
    """Write the current process PID to a file.

    Creates parent directories if needed. Sets file permissions to
    0o600 (owner-only read/write).

    Args:
        path: Destination path for the PID file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="utf-8")
    os.chmod(path, 0o600)
    with _atexit_lock:
        if path not in _atexit_registered:
            atexit.register(remove_pid, path)
            _atexit_registered.add(path)
    log.debug("PID %d written to %s", os.getpid(), path)


def read_pid(path: Path = PID_PATH) -> int | None:
    """Read the PID from a PID file.

    Args:
        path: Path to the PID file.

    Returns:
        The PID as an integer, or None if the file is missing, empty,
        or contains a non-integer value.
    """
    if not path.exists():
        return None

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return int(text)
    except (OSError, ValueError):
        log.debug("Could not read PID from %s", path)
        return None


def remove_pid(path: Path = PID_PATH) -> None:
    """Remove the PID file if it exists.

    Args:
        path: Path to the PID file.
    """
    try:
        path.unlink(missing_ok=True)
        log.debug("PID file removed: %s", path)
    except OSError as exc:
        log.warning("Could not remove PID file %s: %s", path, exc)


# ------------------------------------------------------------------
# Process inspection
# ------------------------------------------------------------------


def is_process_running(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Uses ``os.kill(pid, 0)`` which checks for process existence without
    actually sending a signal.

    Args:
        pid: Process ID to check.

    Returns:
        True if the process exists and is reachable, False otherwise.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it
        return True
    return True


def send_signal(pid: int, sig: signal.Signals) -> bool:
    """Send a signal to a process.

    Args:
        pid: Target process ID.
        sig: Signal to send (e.g., ``signal.SIGTERM``, ``signal.SIGUSR1``).

    Returns:
        True if the signal was sent successfully, False if the process
        does not exist or cannot be signalled.
    """
    try:
        os.kill(pid, sig)
        log.debug("Sent %s to PID %d", sig.name, pid)
        return True
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("Could not send %s to PID %d: %s", sig.name, pid, exc)
        return False


# ------------------------------------------------------------------
# Status file operations
# ------------------------------------------------------------------


def _remove_status_file(path: Path) -> None:
    """Remove a status file if it exists (F-RL25 atexit handler)."""
    try:
        path.unlink(missing_ok=True)
        log.debug("Status file removed (atexit): %s", path)
    except OSError as exc:
        log.warning("Could not remove status file %s: %s", path, exc)


def write_status(
    *,
    host: str,
    port: int,
    upstream_url: str,
    country: str,
    active_connections: int,
    total_connections: int,
    profile: str = "",
    pid: int | None = None,
    started_at: str | None = None,
    stats: dict[str, Any] | None = None,
    path: Path = STATUS_PATH,
) -> None:
    """Write server status to a JSON file.

    Creates parent directories if needed. Sets file permissions to
    0o600 (owner-only read/write).

    Args:
        host: Local bind address.
        port: Local bind port.
        upstream_url: Masked upstream SOCKS5 URL.
        country: Upstream exit country code.
        active_connections: Current active connection count.
        total_connections: Lifetime connection count.
        profile: proxy-st profile name the server is using.
        pid: Server process ID (default: current process).
        started_at: ISO timestamp of when the server started.
        stats: Optional monitor stats dict to include.
        path: Destination path for the status file.
    """
    data: dict[str, Any] = {
        "host": host,
        "port": port,
        "upstream_url": upstream_url,
        "country": country,
        "profile": profile,
        "pid": pid if pid is not None else os.getpid(),
        "started_at": started_at or "",
        "last_updated": datetime.now(UTC).isoformat(),
        "active_connections": active_connections,
        "total_connections": total_connections,
    }
    if stats is not None:
        data["monitor"] = stats

    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file in the same directory, then atomically replace
    # the destination so readers never observe a partially-written file.
    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp")
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning("Could not write status file %s: %s", path, exc)
        return

    # F-RL25: Register atexit cleanup so status file is removed even on crash.
    with _atexit_lock:
        if path not in _status_atexit_registered:
            atexit.register(_remove_status_file, path)
            _status_atexit_registered.add(path)

    log.debug("Status written to %s", path)


def read_status(path: Path = STATUS_PATH) -> dict[str, Any] | None:
    """Read server status from a JSON file.

    Args:
        path: Path to the status JSON file.

    Returns:
        Parsed status dict, or None if the file is missing, empty,
        or contains invalid JSON.
    """
    if not path.exists():
        return None

    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Could not read status from %s: %s", path, exc)
        return None


# ------------------------------------------------------------------
# Liveness-checked status reading (F-RL4)
# ------------------------------------------------------------------


def _try_remove(path: Path) -> None:
    """Best-effort removal of a path. Logs failures at DEBUG level."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not remove %s: %s", path, exc)


def read_status_if_alive(profile: str) -> tuple[bool, int | None, dict[str, Any] | None]:
    """Read the status file only if the associated PID is alive.

    When the PID file refers to a dead process (crash, OOM, SIGKILL),
    both the stale ``.pid`` and ``.status.json`` files are cleaned up
    automatically.

    Args:
        profile: proxy-st profile name.

    Returns:
        Tuple of (is_running, pid, status_data).
        ``status_data`` is ``None`` when the process is not running.
    """
    pid_p = pid_path_for(profile)
    status_p = status_path_for(profile)

    pid = read_pid(pid_p)
    running = pid is not None and is_process_running(pid)

    if not running:
        # Clean up stale files left behind by a crashed process.
        if pid is not None:
            _try_remove(pid_p)
        if status_p.exists():
            _try_remove(status_p)
            log.debug("Removed stale status file: %s", status_p)
        return False, pid, None

    return True, pid, read_status(status_p)


# ------------------------------------------------------------------
# F-RL26: Multi-profile status scanning
# ------------------------------------------------------------------


def scan_all_status(config_dir: Path = CONFIG_DIR) -> list[dict[str, Any]]:
    """Scan all status files and return info for live profiles.

    Globs ``config_dir/*.status.json``, extracts the profile name from
    each filename, checks PID liveness, and removes stale files.

    Args:
        config_dir: Directory to scan (default: ``CONFIG_DIR``).

    Returns:
        List of dicts, each containing ``profile``, ``running``,
        ``pid``, and any status data for live profiles.
    """
    results: list[dict[str, Any]] = []
    for status_file in sorted(config_dir.glob("*.status.json")):
        profile = status_file.name.removesuffix(".status.json")
        if not profile:
            continue

        running, pid, data = read_status_if_alive(profile)
        entry: dict[str, Any] = {}
        if data is not None:
            entry.update(data)
        # Override with authoritative values (filename-derived profile, liveness)
        entry["profile"] = profile
        entry["running"] = running
        entry["pid"] = pid
        results.append(entry)

    return results


# ------------------------------------------------------------------
# F-RL27: Simplified live status helper
# ------------------------------------------------------------------


def read_live_status(profile: str) -> dict[str, Any] | None:
    """Return combined status + liveness for a profile, or None.

    A thin wrapper over :func:`read_status_if_alive` that returns a
    single dict augmented with ``running`` and ``pid`` keys when the
    process is alive, or ``None`` when it is not running.

    Args:
        profile: proxy-st profile name.

    Returns:
        Status dict with ``running=True`` and ``pid`` added, or
        ``None`` if the profile is not running.
    """
    running, pid, data = read_status_if_alive(profile)
    if not running:
        return None

    result: dict[str, Any] = {}
    if data is not None:
        result.update(data)
    # Override with authoritative values
    result["profile"] = profile
    result["running"] = True
    result["pid"] = pid
    return result
