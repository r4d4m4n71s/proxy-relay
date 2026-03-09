"""Async TCP proxy server: accept loop and lifecycle management."""
from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from proxy_relay.handler import handle_connection
from proxy_relay.logger import get_logger
from proxy_relay.upstream import UpstreamInfo, UpstreamManager

log = get_logger(__name__)


class ProxyServer:
    """Local HTTP CONNECT proxy server.

    Binds to a local address and accepts incoming HTTP/CONNECT requests,
    forwarding them through the upstream SOCKS5 proxy resolved via proxy-st.

    Args:
        host: Local bind address (default: 127.0.0.1).
        port: Local bind port (default: 8080).
        upstream_manager: Upstream proxy manager instance.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        upstream_manager: UpstreamManager | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._upstream_manager = upstream_manager
        self._server: asyncio.Server | None = None
        self._upstream: UpstreamInfo | None = None
        self._active_connections: int = 0
        self._total_connections: int = 0
        self._shutdown_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """Start the proxy server.

        Resolves the upstream SOCKS5 proxy, binds the TCP server, and
        begins accepting connections. Installs signal handlers for
        graceful shutdown (SIGINT, SIGTERM).

        Raises:
            UpstreamError: If the upstream proxy cannot be resolved.
            OSError: If the bind address/port is unavailable.
        """
        if self._upstream_manager is None:
            log.error("No upstream manager configured")
            return

        # Resolve upstream before accepting connections
        self._upstream = self._upstream_manager.get_upstream()
        log.info(
            "Upstream resolved: %s (country=%s)",
            self._upstream.url,
            self._upstream.country or "any",
        )

        # Start the TCP server
        self._server = await asyncio.start_server(
            self._on_connection,
            host=self._host,
            port=self._port,
        )

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_shutdown)

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

        Closes the listening socket and waits for active connections
        to finish.
        """
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            log.info("Server stopped")
        self._shutdown_event.set()

    def _signal_shutdown(self) -> None:
        """Signal handler that triggers graceful shutdown."""
        log.info("Shutdown signal received, stopping server...")
        asyncio.get_running_loop().create_task(self.stop())

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
        assert self._upstream is not None

        self._active_connections += 1
        self._total_connections += 1
        conn_id = self._total_connections

        log.debug(
            "Connection #%d accepted (active=%d)",
            conn_id,
            self._active_connections,
        )

        try:
            await handle_connection(reader, writer, self._upstream)
        finally:
            self._active_connections -= 1
            log.debug(
                "Connection #%d finished (active=%d)",
                conn_id,
                self._active_connections,
            )

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
) -> None:
    """Convenience function to create and run a proxy server.

    Args:
        host: Local bind address.
        port: Local bind port.
        profile_name: proxy-st profile name for upstream resolution.
        on_ready: Optional callback invoked when the server is ready.
    """
    manager = UpstreamManager(profile_name)
    server = ProxyServer(host=host, port=port, upstream_manager=manager)

    await server.start()

    if on_ready is not None:
        on_ready()

    await server.serve_forever()
