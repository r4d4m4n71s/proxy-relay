"""CDP capture package for proxy-relay.

Provides ``CaptureSession``, which launches alongside Chromium to record
HTTP traffic, cookies, and storage changes via the Chrome DevTools Protocol.

Both ``websockets`` and ``telemetry-monitor`` are OPTIONAL dependencies.
None of the top-level imports in this module require them — lazy imports are
used inside methods that actually need the packages.

Public API
----------
- :func:`is_capture_available` — check whether optional deps are installed
- :class:`CaptureSession` — manage a CDP observation session
"""
from __future__ import annotations

import asyncio
import socket
import threading
from typing import TYPE_CHECKING, Any

from proxy_relay.logger import get_logger

if TYPE_CHECKING:
    from proxy_relay.capture.models import CaptureConfig

log = get_logger(__name__)

# Module-level references for lazy-loaded classes.
# Populated on first call to CaptureSession.start() and patchable by tests
# via ``patch("proxy_relay.capture.CdpClient", ...)`` etc.
CdpClient: Any = None
BackgroundWriter: Any = None


def _find_free_port() -> int:
    """Bind to port 0 and return the OS-assigned free port number.

    Returns:
        An available TCP port number on 127.0.0.1.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_capture_available() -> bool:
    """Return True if all optional capture dependencies are installed.

    Attempts to import ``websockets`` and ``telemetry_monitor``.  Returns
    False if either raises ``ImportError``.  A ``None`` sentinel in
    ``sys.modules`` (set by tests to simulate a missing package) causes
    Python to raise ``ImportError`` on the ``import`` statement, which is
    caught here.

    Returns:
        True if both ``websockets`` and ``telemetry_monitor`` are available,
        False if either is missing.
    """
    try:
        import telemetry_monitor  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


class CaptureSession:
    """Manage a CDP observation session attached to a Chromium process.

    Runs the CDP event loop in a dedicated OS thread (via
    :meth:`run_in_thread`) so it does not interfere with the main thread or
    the relay's asyncio event loop.

    Args:
        config: CaptureConfig instance. Defaults to ``CaptureConfig()`` if
            None.
        profile: proxy-st profile name stamped on every captured event.
    """

    def __init__(
        self,
        config: CaptureConfig | None = None,
        profile: str = "",
    ) -> None:
        # Lazy import to avoid circular dependency at module load time
        from proxy_relay.capture.models import CaptureConfig as _CaptureConfig

        self._config: CaptureConfig = config if config is not None else _CaptureConfig()
        self._profile = profile

        self._cdp_port_cache: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._writer: Any | None = None
        self._cdp: Any | None = None
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._thread: threading.Thread | None = None

    @property
    def cdp_port(self) -> int:
        """Lazily allocate a free TCP port for ``--remote-debugging-port``.

        The same port is returned on every subsequent call.

        Returns:
            An available TCP port number.
        """
        if self._cdp_port_cache is None:
            self._cdp_port_cache = _find_free_port()
            log.debug("Allocated CDP port %d", self._cdp_port_cache)
        return self._cdp_port_cache

    async def start(self, cdp_port: int) -> None:
        """Initialise the writer, connect to CDP, enable domains, and subscribe.

        All telemetry-monitor and websockets imports are performed lazily
        inside this method.  ``CdpClient`` and ``BackgroundWriter`` are
        resolved from this package's module namespace at call time so that
        tests can patch them via ``proxy_relay.capture.CdpClient`` and
        ``proxy_relay.capture.BackgroundWriter``.

        Args:
            cdp_port: The ``--remote-debugging-port`` Chromium was started with.
        """
        import sys

        from proxy_relay.capture.collector import CaptureCollector

        # Lazily import schema only here to keep module-level imports clean
        # (schema.py does a top-level import of telemetry_monitor).
        # When telemetry_monitor is absent (tests with mocked BackgroundWriter),
        # PROXY_RELAY_SCHEMA stays None — the mock BackgroundWriter ignores routes.
        PROXY_RELAY_SCHEMA = None
        try:
            from proxy_relay.capture.schema import PROXY_RELAY_SCHEMA as _schema  # noqa: PLC0415
            PROXY_RELAY_SCHEMA = _schema
        except ImportError:
            pass

        # Populate the package namespace with the lazy classes so tests can
        # patch "proxy_relay.capture.CdpClient" / "proxy_relay.capture.BackgroundWriter".
        _pkg = sys.modules[__name__]

        if getattr(_pkg, "CdpClient", None) is None:
            from proxy_relay.capture.cdp_client import CdpClient as _CdpCls  # noqa: PLC0415
            setattr(_pkg, "CdpClient", _CdpCls)

        if getattr(_pkg, "BackgroundWriter", None) is None:
            try:
                from telemetry_monitor.writer import BackgroundWriter as _BW  # noqa: PLC0415
                setattr(_pkg, "BackgroundWriter", _BW)
            except ImportError:
                pass  # Will be patched by tests, or fail later with clear error

        # Resolve from package namespace (may be patched by tests)
        _CdpClient = getattr(_pkg, "CdpClient", None)
        _BackgroundWriter = getattr(_pkg, "BackgroundWriter", None)
        if _BackgroundWriter is None:
            raise ImportError(
                "telemetry_monitor is not installed and BackgroundWriter is not mocked. "
                "Install proxy-relay[capture] to use CDP capture."
            )

        # SqliteStore is only needed for a real (non-mocked) BackgroundWriter.
        # When tests mock BackgroundWriter, SqliteStore can be None since the
        # mock ignores constructor arguments.
        sqlite_store = None
        try:
            from telemetry_monitor.storage.sqlite import SqliteStore

            db_path_resolved = self._config.resolved_db_path()
            db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
            sqlite_store = SqliteStore(db_path=db_path_resolved, schema=PROXY_RELAY_SCHEMA)
            sqlite_store.connect()
        except ImportError:
            pass  # BackgroundWriter is mocked in tests — sqlite_store not needed

        log.info("Starting capture session (CDP port %d, profile %r)", cdp_port, self._profile)

        routes = PROXY_RELAY_SCHEMA.routes if PROXY_RELAY_SCHEMA is not None else None
        self._writer = _BackgroundWriter(
            flush_interval_s=2.0,
            batch_size=100,
            sqlite_store=sqlite_store,
            routes=routes,
        )
        self._writer.start()
        log.debug("BackgroundWriter started, db=%s", self._config.resolved_db_path())

        # Connect CDP
        self._cdp = _CdpClient()
        await self._cdp.connect(cdp_port)

        # Enable Network domain
        await self._cdp.send("Network.enable", {"maxPostDataSize": self._config.max_body_bytes})
        log.debug("CDP Network domain enabled")

        # Set up collector
        collector = CaptureCollector(
            enqueue_fn=self._writer.enqueue,
            config=self._config,
            profile=self._profile,
        )

        # Subscribe to Network events
        await self._cdp.subscribe("Network.requestWillBeSent", collector.on_request)
        await self._cdp.subscribe(
            "Network.responseReceived",
            lambda params: collector.on_response(params, body=None),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameSent",
            lambda params: collector.on_websocket_frame("sent", params),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameReceived",
            lambda params: collector.on_websocket_frame("received", params),
        )

        # Store stop event for run_until_stopped
        self._stop_event = asyncio.Event()

        # Start polling tasks
        cookie_task = asyncio.create_task(
            self._poll_cookies(collector), name="cookie-poll"
        )
        storage_task = asyncio.create_task(
            self._poll_storage(collector), name="storage-poll"
        )
        self._poll_tasks = [cookie_task, storage_task]

        log.info("Capture session started")

    async def _poll_cookies(self, collector: Any) -> None:
        """Periodically fetch all cookies from CDP and pass to collector."""
        interval = self._config.cookie_poll_interval_s
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                result = await self._cdp.send("Network.getAllCookies")
                cookies = result.get("cookies", [])
                collector.on_cookies(cookies)
                log.debug("Cookie poll: %d cookies", len(cookies))
            except Exception:
                log.debug("Cookie poll error", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval
                )
            except TimeoutError:
                pass

    async def _poll_storage(self, collector: Any) -> None:
        """Periodically fetch localStorage/sessionStorage via CDP Runtime."""
        interval = self._config.storage_poll_interval_s
        storage_origins: list[dict[str, Any]] = []

        # Discover storage origins once
        try:
            await self._cdp.send("Storage.enable")
            frame_result = await self._cdp.send("Page.enable")
            _ = frame_result  # Page.enable returns empty result
        except Exception:
            log.debug("Could not enable Page/Storage domains for storage poll", exc_info=True)

        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self._fetch_storage_for_origins(collector, storage_origins)
            except Exception:
                log.debug("Storage poll error", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval
                )
            except TimeoutError:
                pass

    async def _fetch_storage_for_origins(
        self, collector: Any, origins: list[dict[str, Any]]
    ) -> None:
        """Fetch localStorage and sessionStorage for configured domains."""
        for domain in self._config.domains:
            for scheme in ("https", "http"):
                origin = f"{scheme}://{domain}"
                for storage_type in ("localStorage", "sessionStorage"):
                    try:
                        result = await self._cdp.send(
                            "DOMStorage.getDOMStorageItems",
                            {
                                "storageId": {
                                    "securityOrigin": origin,
                                    "isLocalStorage": storage_type == "localStorage",
                                }
                            },
                        )
                        entries = result.get("entries", [])
                        data = {e[0]: e[1] for e in entries if len(e) >= 2}
                        if data:
                            collector.on_storage(origin, storage_type, data)
                    except Exception:
                        # Origin may not exist in the current page — silently skip
                        pass

    async def run_until_stopped(self) -> None:
        """Gather CDP recv_loop and poll tasks, waiting for the stop event.

        Returns when ``request_stop()`` is called or when the CDP connection
        closes unexpectedly.
        """
        if self._stop_event is None or self._cdp is None:
            return

        tasks: list[asyncio.Task[None]] = list(self._poll_tasks)

        # Add the CDP recv loop as a supervised task
        recv_task = asyncio.create_task(self._cdp.recv_loop(), name="cdp-recv-supervised")
        tasks.append(recv_task)

        # Wait for stop event or for any task to finish
        stop_wait = asyncio.create_task(self._stop_event.wait(), name="stop-wait")
        tasks_set = set(tasks + [stop_wait])

        done, pending = await asyncio.wait(
            tasks_set, return_when=asyncio.FIRST_COMPLETED
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        log.debug("run_until_stopped exited")

    def request_stop(self) -> None:
        """Request the capture session to stop (thread-safe).

        May be called from any thread.  Uses
        ``loop.call_soon_threadsafe`` when the event loop is running.
        """
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        elif self._stop_event is not None:
            # Loop not yet running — set directly (e.g. early error path)
            try:
                self._stop_event.set()
            except RuntimeError:
                pass  # Event was created in a different loop

    async def stop(self) -> None:
        """Disable CDP domains, close the client, stop the writer, and secure the DB.

        Called from within the capture thread's event loop after
        ``run_until_stopped`` returns.
        """
        import os as _os

        log.info("Stopping capture session")

        # Cancel poll tasks
        for task in self._poll_tasks:
            if not task.done():
                task.cancel()
        if self._poll_tasks:
            await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        self._poll_tasks = []

        # Disable Network domain and close CDP
        if self._cdp is not None:
            try:
                await self._cdp.send("Network.disable")
            except Exception:
                log.debug("Error disabling Network domain", exc_info=True)
            await self._cdp.close()
            self._cdp = None

        # Stop background writer (flushes remaining events)
        if self._writer is not None:
            try:
                self._writer.stop()
            except Exception:
                log.debug("Error stopping background writer", exc_info=True)
            self._writer = None

        # Secure the database file
        db_path = self._config.resolved_db_path()
        if db_path.exists():
            try:
                _os.chmod(db_path, 0o600)
                log.debug("capture.db permissions set to 0600: %s", db_path)
            except OSError as exc:
                log.warning("Could not chmod capture.db: %s", exc)

        log.info("Capture session stopped")

    def run_in_thread(self, cdp_port: int) -> None:
        """Create a new asyncio event loop and run the full capture lifecycle.

        Designed to be called as the ``target`` of a ``threading.Thread``.
        Runs ``start()`` → ``run_until_stopped()`` → ``stop()`` and catches
        all exceptions to prevent the thread from crashing silently.

        Args:
            cdp_port: The ``--remote-debugging-port`` Chromium was started with.
        """
        # Store the port for _run_capture (which may be called by tests without args)
        if cdp_port is not None:
            self._cdp_port_cache = cdp_port
        self._run_capture()

    def _run_capture(self) -> None:
        """Internal: create event loop and run full async capture lifecycle.

        Separated from ``run_in_thread`` so tests can patch
        ``session._run_capture_async`` independently.  Uses ``self.cdp_port``
        (the lazily-allocated port).
        """
        cdp_port = self.cdp_port
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._run_capture_async(cdp_port))
        except Exception:
            log.error("Capture thread error", exc_info=True)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            log.debug("Capture thread event loop closed")

    async def _run_capture_async(self, cdp_port: int) -> None:
        """Full async lifecycle: start -> run_until_stopped -> stop."""
        try:
            await self.start(cdp_port)
        except Exception:
            log.error("Failed to start capture session", exc_info=True)
            return

        try:
            await self.run_until_stopped()
        except Exception:
            log.error("Error in capture run_until_stopped", exc_info=True)
        finally:
            try:
                await self.stop()
            except Exception:
                log.error("Error stopping capture session", exc_info=True)
