"""Connection quality monitor with rolling-window error tracking.

Tracks connection outcomes (success, error, timeout, reset) in a
bounded deque, computes latency statistics, and triggers upstream
rotation when the error threshold is breached.
"""
from __future__ import annotations

import enum
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from proxy_relay.config import MonitorConfig
from proxy_relay.logger import get_logger

log = get_logger(__name__)


class ConnectionOutcome(enum.Enum):
    """Outcome of a single proxied connection attempt."""

    SUCCESS = "success"
    TUNNEL_ERROR = "tunnel_error"
    TIMEOUT = "timeout"
    RESET = "reset"


@dataclass(frozen=True)
class ConnectionRecord:
    """Immutable record of a single connection attempt.

    Attributes:
        timestamp: Monotonic timestamp (seconds) of the event.
        outcome: Result of the connection attempt.
        latency_ms: Connection latency in milliseconds (0.0 for errors).
        target: Target host:port string.
        error_message: Human-readable error description (empty on success).
    """

    timestamp: float
    outcome: ConnectionOutcome
    latency_ms: float
    target: str
    error_message: str = ""


@dataclass
class MonitorStats:
    """Aggregated snapshot of connection quality metrics.

    Attributes:
        total_connections: Lifetime connection count.
        total_errors: Lifetime error count.
        total_rotations: Number of upstream rotations triggered.
        window_size: Maximum rolling window capacity.
        window_error_count: Errors in the current rolling window.
        avg_latency_ms: Average latency of successful connections in the window.
        p95_latency_ms: 95th percentile latency of successful connections in the window.
        last_rotation_time: Monotonic timestamp of the last rotation (0.0 if never).
    """

    total_connections: int = 0
    total_errors: int = 0
    total_rotations: int = 0
    window_size: int = 100
    window_error_count: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    last_rotation_time: float = 0.0


# Type alias for the async rotation callback.
RotateCallback = Callable[[], Coroutine[Any, Any, None]]


class ConnectionMonitor:
    """Rolling-window connection quality monitor.

    Maintains a bounded deque of recent connection records and triggers
    upstream rotation when the error count in the window exceeds the
    configured threshold.

    Thread-safety contract (J-RL5)
    --------------------------------
    All public methods — ``record_success``, ``record_error``, ``get_stats``,
    ``reset``, ``shutdown``, and the properties — **must be called from the
    same asyncio event loop**.  The class contains no internal locking: it
    relies on the single-threaded cooperative scheduling guarantee of asyncio
    for mutual exclusion.  Calling any method from a different thread or a
    different event loop without external synchronisation is undefined
    behaviour and may produce data corruption or missed rotation triggers.

    Args:
        config: Monitor configuration (thresholds and enabled flag).
        rotate_callback: Async callable invoked to trigger upstream rotation.
            May be None if rotation is not wired up.
        window_size: Maximum number of records in the rolling window.
    """

    def __init__(
        self,
        config: MonitorConfig,
        rotate_callback: RotateCallback | None = None,
        window_size: int | None = None,
    ) -> None:
        self._config = config
        self._rotate_callback = rotate_callback
        effective_window = window_size if window_size is not None else config.window_size
        self._window: deque[ConnectionRecord] = deque(maxlen=max(effective_window, 1))
        self._total_connections: int = 0
        self._total_errors: int = 0
        self._total_rotations: int = 0
        self._last_rotation_time: float = 0.0
        self._shutdown: bool = False

    # ------------------------------------------------------------------
    # Public recording API
    # ------------------------------------------------------------------

    async def record_success(self, latency_ms: float, target: str) -> None:
        """Record a successful connection and check for slow threshold.

        Args:
            latency_ms: Tunnel establishment latency in milliseconds.
            target: Target host:port string.
        """
        if not self._config.enabled:
            return

        record = ConnectionRecord(
            timestamp=time.monotonic(),
            outcome=ConnectionOutcome.SUCCESS,
            latency_ms=latency_ms,
            target=target,
        )
        self._window.append(record)
        self._total_connections += 1

        if latency_ms > self._config.slow_threshold_ms:
            log.warning(
                "Slow connection to %s: %.0fms (threshold %.0fms)",
                target,
                latency_ms,
                self._config.slow_threshold_ms,
            )

    async def record_error(
        self,
        outcome: ConnectionOutcome,
        target: str,
        error_message: str = "",
    ) -> None:
        """Record a connection error and check the rotation threshold.

        If the number of errors in the rolling window reaches or exceeds
        ``error_threshold_count``, triggers an upstream rotation and clears
        the window to prevent re-triggering on stale data.

        Args:
            outcome: The error outcome type.
            target: Target host:port string.
            error_message: Human-readable error description.
        """
        if not self._config.enabled:
            return

        record = ConnectionRecord(
            timestamp=time.monotonic(),
            outcome=outcome,
            latency_ms=0.0,
            target=target,
            error_message=error_message,
        )
        self._window.append(record)
        self._total_connections += 1
        self._total_errors += 1

        log.debug(
            "Connection error recorded: outcome=%s target=%s error=%s",
            outcome.value,
            target,
            error_message,
        )

        # Check threshold — threshold=0 means trigger on every error
        error_count = self.window_error_count
        threshold = self._config.error_threshold_count

        if error_count >= threshold:
            log.warning(
                "Error threshold reached: %d errors in window (threshold=%d), "
                "triggering rotation",
                error_count,
                threshold,
            )
            await self._trigger_rotation()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> MonitorStats:
        """Return an aggregated snapshot of connection quality metrics.

        Returns:
            MonitorStats with current totals and window statistics.
        """
        window_errors = self.window_error_count
        successes = [
            r.latency_ms
            for r in self._window
            if r.outcome is ConnectionOutcome.SUCCESS
        ]

        avg_latency = 0.0
        p95_latency = 0.0
        if successes:
            avg_latency = sum(successes) / len(successes)
            sorted_latencies = sorted(successes)
            idx = int(len(sorted_latencies) * 0.95)
            idx = min(idx, len(sorted_latencies) - 1)
            p95_latency = sorted_latencies[idx]

        return MonitorStats(
            total_connections=self._total_connections,
            total_errors=self._total_errors,
            total_rotations=self._total_rotations,
            window_size=self._window.maxlen or 100,
            window_error_count=window_errors,
            avg_latency_ms=avg_latency,
            p95_latency_ms=p95_latency,
            last_rotation_time=self._last_rotation_time,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Return True if monitoring is enabled."""
        return self._config.enabled

    @property
    def window_error_count(self) -> int:
        """Return the number of errors in the current rolling window."""
        return sum(
            1
            for r in self._window
            if r.outcome is not ConnectionOutcome.SUCCESS
        )

    def reset(self) -> None:
        """Reset all counters and the rolling window to initial state.

        This monitor holds only in-memory state — no file handles or
        network connections — so no external cleanup is required.
        """
        self._window.clear()
        self._total_connections = 0
        self._total_errors = 0
        self._total_rotations = 0
        self._last_rotation_time = 0.0
        log.debug("ConnectionMonitor reset")

    def shutdown(self) -> None:
        """Signal that the server is shutting down.

        Once called, rotation callbacks will no longer be invoked even if
        the error threshold is reached.  This prevents spurious rotation
        attempts during graceful shutdown when connection errors are expected.
        """
        self._shutdown = True
        log.debug("ConnectionMonitor shutdown signalled")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _trigger_rotation(self) -> None:
        """Invoke the rotation callback and clear the window.

        Clears the rolling window after rotation to prevent immediate
        re-triggering on stale error records.  Does nothing if
        ``shutdown()`` has been called — connection errors during graceful
        shutdown are expected and should not trigger a rotation.
        """
        if self._shutdown:
            log.debug("Skipping rotation — monitor is in shutdown state")
            self._window.clear()
            return

        self._total_rotations += 1
        self._last_rotation_time = time.monotonic()

        if self._rotate_callback is not None:
            try:
                await self._rotate_callback()
                log.info("Upstream rotation triggered successfully (rotation #%d)", self._total_rotations)
            except Exception as exc:
                log.error("Upstream rotation failed: %s", exc)
        else:
            log.warning("Rotation threshold reached but no rotate_callback configured")

        # Clear window to act as cooldown — prevents re-trigger on stale data
        self._window.clear()
