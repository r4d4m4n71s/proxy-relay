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

import json
import os
import signal
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


def pid_path_for(profile: str) -> Path:
    """Return the PID file path for a given profile.

    Args:
        profile: proxy-st profile name.

    Returns:
        Path to ``~/.config/proxy-relay/{profile}.pid``.
    """
    return CONFIG_DIR / f"{profile}.pid"


def status_path_for(profile: str) -> Path:
    """Return the status file path for a given profile.

    Args:
        profile: proxy-st profile name.

    Returns:
        Path to ``~/.config/proxy-relay/{profile}.status.json``.
    """
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


def write_status(
    *,
    host: str,
    port: int,
    upstream_url: str,
    country: str,
    active_connections: int,
    total_connections: int,
    profile: str = "",
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
        stats: Optional monitor stats dict to include.
        path: Destination path for the status file.
    """
    data: dict[str, Any] = {
        "host": host,
        "port": port,
        "upstream_url": upstream_url,
        "country": country,
        "profile": profile,
        "active_connections": active_connections,
        "total_connections": total_connections,
    }
    if stats is not None:
        data["monitor"] = stats

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
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
