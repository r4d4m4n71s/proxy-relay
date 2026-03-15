"""Async TCP proxy server: accept loop and lifecycle management."""
from __future__ import annotations

import asyncio
import ipaddress
import signal
import time
from collections.abc import Callable
from dataclasses import asdict

from proxy_relay.config import MonitorConfig
from proxy_relay.handler import handle_connection
from proxy_relay.logger import get_logger
from proxy_relay.monitor import ConnectionMonitor, MonitorStats
from proxy_relay.pidfile import pid_path_for, remove_pid, status_path_for, write_pid, write_status
from proxy_relay.response import send_error
from proxy_relay.tunnel import open_tunnel
from proxy_relay.upstream import UpstreamInfo, UpstreamManager

log = get_logger(__name__)

# Health check target — plain HTTP endpoint that returns the caller's IP.
_HEALTH_TARGET_HOST: str = "icanhazip.com"
_HEALTH_TARGET_PORT: int = 80
_HEALTH_MAX_RETRIES: int = 3
_HEALTH_READ_TIMEOUT: float = 15.0

# Minimum interval between status-file writes triggered by connection close (E-RL5).
_STATUS_DEBOUNCE_SECS: float = 5.0


class ProxyServer:
    """Local HTTP CONNECT proxy server.

    Binds to a local address and accepts incoming HTTP/CONNECT requests,
    forwarding them through the upstream SOCKS5 proxy resolved via proxy-st.

    Args:
        host: Local bind address (default: 127.0.0.1).
        port: Local bind port (default: 8080).
        upstream_manager: Upstream proxy manager instance.
        monitor_config: Optional monitor configuration. When provided and
            enabled, a ConnectionMonitor is created to track connection
            quality and trigger automatic upstream rotation.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        upstream_manager: UpstreamManager | None = None,
        monitor_config: MonitorConfig | None = None,
        profile_name: str = "browse",
    ) -> None:
        self._host = host
        self._port = port
        self._upstream_manager = upstream_manager
        self._monitor_config = monitor_config
        self._profile_name = profile_name
        self._pid_path = pid_path_for(profile_name)
        self._status_path = status_path_for(profile_name)
        self._server: asyncio.Server | None = None
        self._upstream: UpstreamInfo | None = None
        self._monitor: ConnectionMonitor | None = None
        self._active_connections: int = 0
        self._total_connections: int = 0
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._last_status_write: float = 0.0

    async def start(self) -> None:
        """Start the proxy server.

        Resolves the upstream SOCKS5 proxy, binds the TCP server, writes
        the PID file, and begins accepting connections. Installs signal
        handlers for graceful shutdown (SIGINT, SIGTERM) and rotation
        (SIGUSR1).

        Raises:
            UpstreamError: If the upstream proxy cannot be resolved.
            OSError: If the bind address/port is unavailable.
        """
        if self._upstream_manager is None:
            log.error("No upstream manager configured")
            return

        # Resolve upstream before accepting connections
        self._upstream = await asyncio.to_thread(self._upstream_manager.get_upstream)
        log.info(
            "Upstream resolved: %s (country=%s)",
            self._upstream.url,
            self._upstream.country or "any",
        )

        # Create connection monitor if configured and enabled
        if self._monitor_config is not None and self._monitor_config.enabled:
            self._monitor = ConnectionMonitor(
                config=self._monitor_config,
                rotate_callback=self._do_rotate,
                window_size=self._monitor_config.window_size,
            )
            log.info(
                "Connection monitor enabled: window=%d, error_threshold=%d, "
                "slow_threshold=%.0fms",
                self._monitor_config.window_size,
                self._monitor_config.error_threshold_count,
                self._monitor_config.slow_threshold_ms,
            )

        # Start the TCP server
        self._server = await asyncio.start_server(
            self._on_connection,
            host=self._host,
            port=self._port,
        )

        # Capture actual port when OS-assigned (port=0)
        if self._port == 0 and self._server.sockets:
            self._port = self._server.sockets[0].getsockname()[1]

        # Write PID file (profile-scoped)
        await asyncio.to_thread(write_pid, self._pid_path)

        # Write initial status so browse can discover host:port
        await self._update_status_file_async()

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_shutdown)

        # Install SIGUSR1 handler for manual rotation
        loop.add_signal_handler(signal.SIGUSR1, self._signal_rotate)

        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("Proxy server listening on %s", addrs)
        log.info("Forwarding via upstream SOCKS5 at %s", self._upstream.url)

    async def serve_forever(self) -> None:
        """Run the server until a shutdown signal is received.

        Blocks until SIGINT or SIGTERM, then performs graceful shutdown.
        """
        if self._server is None:
            log.error("Server not started — call start() first")
            return

        async with self._server:
            await self._server.start_serving()
            log.info("Server ready — press Ctrl+C to stop")
            await self._shutdown_event.wait()

        log.info(
            "Server shut down. Total connections served: %d",
            self._total_connections,
        )

    async def stop(self) -> None:
        """Gracefully stop the server.

        Closes the listening socket, removes the PID file, and waits
        for active connections to finish.
        """
        remove_pid(self._pid_path)
        try:
            self._status_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            log.info("Server stopped")
        self._shutdown_event.set()

    def _signal_shutdown(self) -> None:
        """Signal handler that triggers graceful shutdown."""
        log.info("Shutdown signal received, stopping server...")
        task = asyncio.get_running_loop().create_task(self.stop())
        task.add_done_callback(
            lambda t: log.error("stop() raised an exception: %s", t.exception())
            if not t.cancelled() and t.exception() is not None
            else None
        )

    def _signal_rotate(self) -> None:
        """Signal handler (SIGUSR1) that triggers upstream rotation."""
        log.info("Rotation signal (SIGUSR1) received")
        asyncio.get_running_loop().create_task(self._do_rotate())

    async def _do_rotate(self) -> None:
        """Rotate the upstream SOCKS5 session and update the cached upstream.

        Called either by the monitor's automatic rotation or by SIGUSR1.
        """
        if self._upstream_manager is None:
            log.warning("Cannot rotate: no upstream manager configured")
            return

        try:
            new_upstream = await asyncio.to_thread(self._upstream_manager.rotate)
            self._upstream = new_upstream
            log.info(
                "Upstream rotated: %s (country=%s)",
                self._upstream.url,
                self._upstream.country or "any",
            )
            await self._update_status_file_async()
        except Exception as exc:
            log.error(
                "Upstream rotation failed — keeping current upstream: %s", exc
            )

    async def health_check(self) -> tuple[bool, str]:
        """Check upstream connectivity, rotating on failure.

        Opens a SOCKS5 tunnel to icanhazip.com and reads the exit IP.
        On failure, rotates the upstream and retries up to
        ``_HEALTH_MAX_RETRIES`` times.  All rotation logic stays inside the
        server — callers just get back (ok, message).

        Returns:
            Tuple of (success, body) where body is the exit IP on success
            or an error description on failure.
        """
        if self._upstream is None:
            return False, "server not started"

        last_error = ""
        for attempt in range(1, _HEALTH_MAX_RETRIES + 1):
            assert self._upstream is not None
            result = None  # reset each iteration so the except block never closes a stale writer
            try:
                result = await asyncio.wait_for(
                    open_tunnel(
                        _HEALTH_TARGET_HOST,
                        _HEALTH_TARGET_PORT,
                        self._upstream,
                    ),
                    timeout=_HEALTH_READ_TIMEOUT,
                )
                # Send a minimal HTTP request through the tunnel
                request = (
                    f"GET / HTTP/1.1\r\n"
                    f"Host: {_HEALTH_TARGET_HOST}\r\n"
                    f"Connection: close\r\n"
                    f"\r\n"
                )
                result.writer.write(request.encode("latin-1"))
                await result.writer.drain()

                # Read until we find the end-of-headers delimiter and have
                # enough body to extract the IP.  Two reads (8 KiB) is more
                # than enough for a health-check response from icanhazip
                # (E-RL1).  We cap reads to avoid unbounded memory growth
                # if the server sends excessive data.
                raw_buf = bytearray()
                _max_health_reads = 4
                for _ in range(_max_health_reads):
                    chunk = await asyncio.wait_for(
                        result.reader.read(4096),
                        timeout=_HEALTH_READ_TIMEOUT,
                    )
                    if not chunk:
                        break
                    raw_buf.extend(chunk)
                    if b"\r\n\r\n" in raw_buf:
                        # Headers found — body is included in same or next chunk.
                        # For icanhazip the body is just an IP address.
                        break
                result.writer.close()
                await result.writer.wait_closed()
                result = None  # writer closed; prevent double-close in except

                # Parse the HTTP response body (after headers)
                text = raw_buf.decode("utf-8", errors="replace")
                if "\r\n\r\n" in text:
                    body = text.split("\r\n\r\n", 1)[1].strip()
                else:
                    body = text.strip()

                # Validate the body looks like a real IP address
                try:
                    ipaddress.ip_address(body)
                except ValueError:
                    last_error = f"response body is not a valid IP address: {body!r}"
                    log.warning(
                        "Health check response invalid (attempt %d/%d): %s",
                        attempt, _HEALTH_MAX_RETRIES, last_error,
                    )
                    if attempt < _HEALTH_MAX_RETRIES:
                        log.info("Rotating upstream before retry...")
                        await self._do_rotate()
                    continue

                log.info("Health check OK (attempt %d/%d): exit IP %s", attempt, _HEALTH_MAX_RETRIES, body)
                return True, body

            except Exception as exc:
                # Close the tunnel writer only if it was opened in this iteration
                if result is not None and hasattr(result, "writer"):
                    try:
                        result.writer.close()
                        await result.writer.wait_closed()
                    except Exception:
                        pass
                last_error = str(exc)
                log.warning(
                    "Health check failed (attempt %d/%d): %s",
                    attempt, _HEALTH_MAX_RETRIES, last_error,
                )
                if attempt < _HEALTH_MAX_RETRIES:
                    log.info("Rotating upstream before retry...")
                    await self._do_rotate()

        # Build a descriptive error with upstream context and actionable hints.
        profile = self._profile_name
        country = self._upstream.country or "any"
        upstream_url = self._upstream.url

        hint_parts: list[str] = []
        low = last_error.lower()
        if "host unreachable" in low or "connection refused" in low:
            hint_parts.append(
                "the upstream proxy accepted the connection but the exit node "
                "could not reach the target — the residential IP pool for this "
                "region may be exhausted or the city targeting too narrow"
            )
            hint_parts.append("try: proxy-st rotate --profile %s" % profile)
            hint_parts.append(
                "if rotation keeps failing, remove the 'city' setting from "
                "the profile in ~/.config/proxy-st/config.toml"
            )
        elif "timed out" in low:
            hint_parts.append(
                "the upstream proxy did not respond in time — the provider "
                "may be experiencing an outage or the region is overloaded"
            )
            hint_parts.append("try: proxy-st check --profile %s" % profile)

        lines = [
            f"upstream unreachable after {_HEALTH_MAX_RETRIES} attempts "
            f"(profile={profile!r}, country={country}, upstream={upstream_url})",
            f"last error: {last_error}",
        ]
        if hint_parts:
            lines.append("hint: " + "; ".join(hint_parts))

        return False, "\n  ".join(lines)

    def _update_status_file(
        self,
        stats_dict: dict | None,
        profile: str,
        upstream_url: str,
        country: str | None,
        active_connections: int,
        total_connections: int,
    ) -> None:
        """Write current server status to the status JSON file.

        All mutable state is passed in as arguments so this method can be
        safely called from a thread-pool thread without touching shared objects
        while the event loop may be mutating them (E-RL8).
        """
        write_status(
            host=self._host,
            port=self._port,
            upstream_url=upstream_url,
            country=country,
            profile=profile,
            active_connections=active_connections,
            total_connections=total_connections,
            stats=stats_dict,
            path=self._status_path,
        )

    async def _update_status_file_async(self) -> None:
        """Snapshot event-loop state then write the status file off-thread.

        The snapshot is taken in the event loop (single-threaded context) so
        that the thread-pool thread receives a stable copy of all values (E-RL8).
        """
        if self._upstream is None:
            return

        # Snapshot all event-loop state before handing off to the thread pool.
        stats_dict: dict | None = None
        if self._monitor is not None:
            stats_dict = asdict(self._monitor.get_stats())

        profile = self._upstream_manager.profile_name if self._upstream_manager is not None else ""
        upstream_url = self._upstream.url
        country = self._upstream.country
        active_connections = self._active_connections
        total_connections = self._total_connections

        await asyncio.to_thread(
            self._update_status_file,
            stats_dict,
            profile,
            upstream_url,
            country,
            active_connections,
            total_connections,
        )

    @property
    def monitor_stats(self) -> MonitorStats | None:
        """Return current monitor stats, or None if monitoring is disabled."""
        if self._monitor is None:
            return None
        return self._monitor.get_stats()

    async def _on_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Callback for each new client connection.

        Args:
            reader: Client stream reader.
            writer: Client stream writer.
        """
        # Guard against connections arriving before start() completes (E-RL3).
        # assert stripped in -O mode would silently proceed with None upstream.
        if self._upstream is None:
            await send_error(writer, 502, "Bad Gateway")
            return

        # Snapshot the upstream reference at connection start so that a
        # concurrent rotation in _do_rotate() does not affect this handler
        # mid-flight.
        upstream_snapshot = self._upstream

        self._active_connections += 1
        self._total_connections += 1
        conn_id = self._total_connections

        log.debug(
            "Connection #%d accepted (active=%d)",
            conn_id,
            self._active_connections,
        )

        try:
            await handle_connection(
                reader, writer, upstream_snapshot,
                monitor=self._monitor,
                health_callback=self.health_check,
            )
        finally:
            self._active_connections -= 1
            log.debug(
                "Connection #%d finished (active=%d)",
                conn_id,
                self._active_connections,
            )
            # Debounce status-file writes: at most once every _STATUS_DEBOUNCE_SECS
            # seconds to avoid excessive I/O when many connections close rapidly (E-RL5).
            now = time.monotonic()
            if now - self._last_status_write >= _STATUS_DEBOUNCE_SECS:
                self._last_status_write = now
                await self._update_status_file_async()

    @property
    def host(self) -> str:
        """Return the bind host."""
        return self._host

    @property
    def port(self) -> int:
        """Return the bind port."""
        return self._port

    @property
    def active_connections(self) -> int:
        """Return the number of active connections."""
        return self._active_connections

    @property
    def total_connections(self) -> int:
        """Return the total number of connections served."""
        return self._total_connections

    @property
    def is_running(self) -> bool:
        """Return True if the server is currently running."""
        return self._server is not None and self._server.is_serving()


async def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    profile_name: str = "browse",
    on_ready: Callable[[], None] | None = None,
    monitor_config: MonitorConfig | None = None,
) -> None:
    """Convenience function to create and run a proxy server.

    Args:
        host: Local bind address.
        port: Local bind port.
        profile_name: proxy-st profile name for upstream resolution.
        on_ready: Optional callback invoked when the server is ready.
        monitor_config: Optional monitor configuration for quality tracking.
    """
    manager = UpstreamManager(profile_name)
    server = ProxyServer(
        host=host,
        port=port,
        upstream_manager=manager,
        monitor_config=monitor_config,
        profile_name=profile_name,
    )

    await server.start()

    if on_ready is not None:
        on_ready()

    await server.serve_forever()
