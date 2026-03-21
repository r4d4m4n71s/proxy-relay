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
import uuid
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

    Note: There is a known TOCTOU race between releasing the socket here and
    Chromium binding to the returned port.  This window is negligibly small on
    loopback and the worst-case outcome is a port-in-use error that surfaces
    clearly in the log (G-RL13).

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
        self._collector: Any | None = None
        self._poll_tasks: list[asyncio.Task[None]] = []
        self._thread: threading.Thread | None = None
        self._session_id: str = ""

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

        # F-RL20: generate a unique session ID for this capture run
        self._session_id = str(uuid.uuid4())

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
            import sqlite3 as _sqlite3

            from telemetry_monitor.storage.sqlite import SqliteStore

            db_path_resolved = self._config.resolved_db_path()
            db_path_resolved.parent.mkdir(parents=True, exist_ok=True)

            # F-RL21: rotate existing DB before opening a new session
            if self._config.rotate_db and db_path_resolved.exists():
                from datetime import UTC, datetime  # noqa: PLC0415

                # Skip rotation if the existing DB is too small (near-empty session)
                try:
                    existing_size = db_path_resolved.stat().st_size
                except OSError:
                    existing_size = 0
                min_size_bytes = self._config.min_rotate_kb * 1024

                if existing_size >= min_size_bytes:
                    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                    profile_tag = self._profile or "default"
                    rotated = db_path_resolved.parent / f"{profile_tag}-{ts}.capture.db"
                    db_path_resolved.rename(rotated)
                    log.info("Rotated capture DB to %s", rotated)
                else:
                    log.debug(
                        "Skipping rotation — capture DB below min_rotate_kb (%d KB < %d KB)",
                        existing_size // 1024, self._config.min_rotate_kb,
                    )

                # F-RL23: purge old rotated DBs by age, size, and count
                self._purge_old_dbs(db_path_resolved.parent)

            sqlite_store = SqliteStore(db_path=db_path_resolved, schema=PROXY_RELAY_SCHEMA)
            sqlite_store.connect()

            # BackgroundWriter drains in its own daemon thread, but
            # SqliteStore.connect() was called in this (capture) thread.
            # Re-open the connection with check_same_thread=False so the
            # writer thread can use it without raising ProgrammingError.
            #
            # J-RL7: prefer sqlite_store.reconnect(check_same_thread=False) when
            # available (telemetry-monitor may add this public API in a future
            # release).  Fall back to the private _conn access when reconnect()
            # does not exist yet, so we stay compatible with the current release
            # without forking telemetry-monitor.
            try:
                sqlite_store.reconnect(check_same_thread=False)
            except AttributeError:
                # reconnect() not yet available — use private _conn as before.
                if sqlite_store._conn is not None:
                    sqlite_store._conn.close()
                    sqlite_store._conn = _sqlite3.connect(
                        str(db_path_resolved), timeout=10.0, check_same_thread=False,
                    )
                    sqlite_store._conn.execute("PRAGMA journal_mode=WAL")
                    sqlite_store._conn.execute("PRAGMA synchronous=NORMAL")
                    sqlite_store._conn.execute("PRAGMA busy_timeout=5000")
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
        log.warning(
            "CDP active — elevated DataDome detection risk. "
            "Use warmup (no CDP) to build trust before capture sessions."
        )

        # Enable CDP domains
        await self._cdp.send("Network.enable", {"maxPostDataSize": self._config.max_body_bytes})
        await self._cdp.send("Page.enable")
        # J-RL11: enable storage domains here (once, at session start) so that
        # _poll_storage() never calls enable mid-session — which would fail after
        # the first call because the domain is already enabled.
        try:
            await self._cdp.send("Storage.enable")
        except Exception:
            log.debug("Could not enable Storage domain", exc_info=True)
        try:
            await self._cdp.send("IndexedDB.enable")
        except Exception:
            log.debug("Could not enable IndexedDB domain", exc_info=True)
        log.debug("CDP Network + Page + Storage + IndexedDB domains enabled")

        # Set up collector
        collector = CaptureCollector(
            enqueue_fn=self._writer.enqueue,
            config=self._config,
            profile=self._profile,
            session_id=self._session_id,
        )
        self._collector = collector

        # Subscribe to Network events
        await self._cdp.subscribe("Network.requestWillBeSent", collector.on_request)
        await self._cdp.subscribe(
            "Network.responseReceived",
            self._make_response_handler(collector),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameSent",
            lambda params: collector.on_websocket_frame("sent", params),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameReceived",
            lambda params: collector.on_websocket_frame("received", params),
        )

        # Subscribe to Page navigation events
        await self._cdp.subscribe("Page.frameNavigated", collector.on_navigation)

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

    def _make_response_handler(self, collector: Any) -> Any:
        """Build an async callback that fetches response bodies via CDP.

        The returned coroutine function is registered as the
        ``Network.responseReceived`` subscriber.  For non-binary MIME types on
        matching domains it calls ``Network.getResponseBody`` to retrieve the
        actual body before forwarding to the collector.  Binary or
        non-matching responses are forwarded with ``body=None``.
        """
        from proxy_relay.capture.models import should_capture_body

        cdp = self._cdp

        async def _on_response(params: dict[str, Any]) -> None:
            response = params.get("response", {})
            mime_type: str = response.get("mimeType", "")
            request_id: str = params.get("requestId", "")
            url: str = response.get("url", "")

            body: str | None = None

            if request_id and should_capture_body(mime_type) and collector.matches_domain(url):
                try:
                    result = await cdp.send(
                        "Network.getResponseBody", {"requestId": request_id},
                    )
                    body = result.get("body", "")
                    if result.get("base64Encoded", False):
                        import base64
                        body = base64.b64decode(body).decode("utf-8", errors="replace")
                except Exception:
                    log.debug("Could not fetch body for %s (request %s)", url, request_id)

            collector.on_response(params, body=body)

        return _on_response

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
        """Periodically fetch localStorage/sessionStorage/IndexedDB via CDP.

        Storage and IndexedDB domains are enabled once in ``start()`` (and
        ``_reconnect_cdp()`` after reconnect), not here, so enabling is never
        called redundantly mid-session (J-RL11).
        """
        interval = self._config.storage_poll_interval_s

        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self._fetch_storage_for_origins(collector)
            except Exception:
                log.debug("Storage poll error", exc_info=True)
            try:
                await self._fetch_indexed_db_for_origins(collector)
            except Exception:
                log.debug("IndexedDB poll error", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval
                )
            except TimeoutError:
                pass

    async def _fetch_storage_for_origins(self, collector: Any) -> None:
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

    async def _fetch_indexed_db_for_origins(self, collector: Any) -> None:
        """Fetch IndexedDB contents for configured domains and pass to collector."""
        from proxy_relay.capture.models import INDEXEDDB_PAGE_SIZE

        for domain in self._config.domains:
            for scheme in ("https", "http"):
                origin = f"{scheme}://{domain}"
                try:
                    db_result = await self._cdp.send(
                        "IndexedDB.requestDatabaseNames",
                        {"securityOrigin": origin},
                    )
                    db_names: list[str] = db_result.get("databaseNames", [])
                except Exception:
                    continue  # Origin may not have IndexedDB

                for db_name in db_names:
                    try:
                        db_info = await self._cdp.send(
                            "IndexedDB.requestDatabase",
                            {"securityOrigin": origin, "databaseName": db_name},
                        )
                        object_stores = (
                            db_info.get("databaseWithObjectStores", {})
                            .get("objectStores", [])
                        )
                    except Exception:
                        continue

                    for store in object_stores:
                        store_name: str = store.get("name", "")
                        storage_type = f"indexedDB:{db_name}/{store_name}"
                        try:
                            data_result = await self._cdp.send(
                                "IndexedDB.requestData",
                                {
                                    "securityOrigin": origin,
                                    "databaseName": db_name,
                                    "objectStoreName": store_name,
                                    "indexName": "",
                                    "skipCount": 0,
                                    "pageSize": INDEXEDDB_PAGE_SIZE,
                                },
                            )
                            entries = data_result.get("objectStoreDataEntries", [])
                            data: dict[str, str] = {}
                            for entry in entries:
                                key = str(entry.get("key", {}).get("value", ""))
                                value = str(entry.get("value", {}).get("value", ""))
                                if key:
                                    data[key] = value
                            if data:
                                collector.on_storage(origin, storage_type, data)
                        except Exception:
                            pass  # Object store may be empty or inaccessible

    def _purge_old_dbs(self, capture_dir: Any) -> None:
        """Remove rotated capture DBs that exceed age, size, or count limits.

        Scans *capture_dir* for rotated DB files (both legacy ``capture-*.db``
        and new ``*.capture.db`` patterns) and:
        1. Deletes files older than ``config.max_db_age_days`` or larger than
           ``config.max_db_size_mb``.
        2. If more than ``config.max_db_count`` files remain, deletes the
           oldest until the count is within the limit.

        Args:
            capture_dir: Directory (Path) to scan for rotated DB files.
        """
        import time as _time
        from pathlib import Path as _Path

        capture_dir = _Path(capture_dir)
        if not capture_dir.is_dir():
            return

        max_age_s = self._config.max_db_age_days * 86400
        max_size_b = self._config.max_db_size_mb * 1024 * 1024
        now = _time.time()

        # Collect rotated DBs: new pattern + legacy pattern
        rotated: list[_Path] = []
        rotated.extend(capture_dir.glob("*.capture.db"))
        rotated.extend(capture_dir.glob("capture-*.db"))
        # Deduplicate in case patterns overlap
        rotated = list(dict.fromkeys(rotated))

        # Pass 1: purge by age and size
        surviving: list[tuple[float, _Path]] = []
        for db_file in rotated:
            try:
                stat = db_file.stat()
                too_old = (now - stat.st_mtime) > max_age_s
                too_large = stat.st_size > max_size_b
                if too_old or too_large:
                    db_file.unlink()
                    reason = "age" if too_old else "size"
                    log.info("Purged rotated DB (%s): %s", reason, db_file.name)
                else:
                    surviving.append((stat.st_mtime, db_file))
            except OSError:
                log.debug("Could not purge %s", db_file, exc_info=True)

        # Pass 2: enforce max_db_count — delete oldest first
        max_count = self._config.max_db_count
        if max_count > 0 and len(surviving) > max_count:
            surviving.sort(key=lambda t: t[0])  # oldest first
            excess = surviving[: len(surviving) - max_count]
            for _, db_file in excess:
                try:
                    db_file.unlink()
                    log.info("Purged rotated DB (count): %s", db_file.name)
                except OSError:
                    log.debug("Could not purge %s", db_file, exc_info=True)

    def _purge_old_reports(self, report_dir: Any) -> None:
        """Remove old report files by age and count.

        Scans *report_dir* for report files (both legacy ``capture-report-*.md``
        and new ``*.report.md`` patterns) and purges by age and count.

        Args:
            report_dir: Directory (Path) to scan for report files.
        """
        import time as _time
        from pathlib import Path as _Path

        report_dir = _Path(report_dir)
        if not report_dir.is_dir():
            return

        max_age_s = self._config.max_report_age_days * 86400
        now = _time.time()

        # Collect reports: new pattern + legacy pattern
        reports: list[_Path] = []
        reports.extend(report_dir.glob("*.report.md"))
        reports.extend(report_dir.glob("capture-report-*.md"))
        reports = list(dict.fromkeys(reports))

        # Pass 1: purge by age
        surviving: list[tuple[float, _Path]] = []
        for report_file in reports:
            try:
                stat = report_file.stat()
                if (now - stat.st_mtime) > max_age_s:
                    report_file.unlink()
                    log.info("Purged old report (age): %s", report_file.name)
                else:
                    surviving.append((stat.st_mtime, report_file))
            except OSError:
                log.debug("Could not purge %s", report_file, exc_info=True)

        # Pass 2: enforce max_report_count
        max_count = self._config.max_report_count
        if max_count > 0 and len(surviving) > max_count:
            surviving.sort(key=lambda t: t[0])
            for _, report_file in surviving[: len(surviving) - max_count]:
                try:
                    report_file.unlink()
                    log.info("Purged old report (count): %s", report_file.name)
                except OSError:
                    log.debug("Could not purge %s", report_file, exc_info=True)

    async def _reconnect_cdp(self) -> None:
        """Reconnect to CDP after an unexpected WebSocket disconnect.

        Re-creates the CdpClient, enables domains, and re-subscribes the
        existing collector to CDP events.  Reuses the same writer and
        collector so captured data continues flowing to the same DB.
        """
        import sys

        _pkg = sys.modules[__name__]
        _CdpClient = getattr(_pkg, "CdpClient", None)

        # Close old client if still around
        if self._cdp is not None:
            try:
                await self._cdp.close()
            except Exception:
                pass

        cdp_port = self._cdp_port_cache
        if cdp_port is None or _CdpClient is None:
            raise RuntimeError("Cannot reconnect: no CDP port or CdpClient class")

        self._cdp = _CdpClient()
        await self._cdp.connect(cdp_port)

        # Re-enable CDP domains (J-RL11: mirror the enable sequence from start())
        await self._cdp.send("Network.enable", {"maxPostDataSize": self._config.max_body_bytes})
        await self._cdp.send("Page.enable")
        try:
            await self._cdp.send("Storage.enable")
        except Exception:
            log.debug("Could not enable Storage domain on reconnect", exc_info=True)
        try:
            await self._cdp.send("IndexedDB.enable")
        except Exception:
            log.debug("Could not enable IndexedDB domain on reconnect", exc_info=True)

        # Re-subscribe collector to events
        collector = self._collector
        if collector is None:
            raise RuntimeError("Cannot reconnect: no collector")

        await self._cdp.subscribe("Network.requestWillBeSent", collector.on_request)
        await self._cdp.subscribe(
            "Network.responseReceived",
            self._make_response_handler(collector),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameSent",
            lambda params: collector.on_websocket_frame("sent", params),
        )
        await self._cdp.subscribe(
            "Network.webSocketFrameReceived",
            lambda params: collector.on_websocket_frame("received", params),
        )
        await self._cdp.subscribe("Page.frameNavigated", collector.on_navigation)

    async def run_until_stopped(self) -> None:
        """Gather CDP recv_loop and poll tasks, waiting for the stop event.

        Returns when ``request_stop()`` is called.  If the CDP WebSocket
        disconnects (e.g. page navigation, target change), the session
        automatically reconnects and re-subscribes to CDP events.
        """
        if self._stop_event is None or self._cdp is None:
            return

        max_reconnects = self._config.max_cdp_reconnects
        reconnect_delay = self._config.cdp_reconnect_delay_s

        for attempt in range(max_reconnects):
            if self._stop_event.is_set():
                break

            tasks: list[asyncio.Task[None]] = list(self._poll_tasks)

            # Use the existing recv_loop task started by connect() — do NOT
            # create a second one (two concurrent recv() on the same WebSocket
            # causes immediate disconnection).
            # J-RL8: use the public recv_task property instead of _recv_task.
            if self._cdp is not None and self._cdp.recv_task is not None:
                tasks.append(self._cdp.recv_task)

            # Wait for stop event or for any task to finish
            stop_wait = asyncio.create_task(self._stop_event.wait(), name="stop-wait")
            tasks_set = set(tasks + [stop_wait])

            done, pending = await asyncio.wait(
                tasks_set, return_when=asyncio.FIRST_COMPLETED
            )

            # If stop was requested, clean up and exit
            if self._stop_event.is_set():
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                break

            # CDP recv_task finished unexpectedly — attempt reconnect
            stop_wait.cancel()
            try:
                await stop_wait
            except asyncio.CancelledError:
                pass

            log.warning(
                "CDP connection lost (attempt %d/%d), reconnecting in %.1fs...",
                attempt + 1, max_reconnects, reconnect_delay,
            )

            # Wait for reconnect_delay but wake immediately if stop requested
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=reconnect_delay)
                # stop_event was set during the delay — exit
                break
            except TimeoutError:
                pass  # Normal: delay elapsed, proceed with reconnect

            if self._stop_event.is_set():
                break

            try:
                await self._reconnect_cdp()
                log.info("CDP reconnected successfully")
                # Reset backoff delay on success
                reconnect_delay = self._config.cdp_reconnect_delay_s
            except Exception:
                log.warning("CDP reconnect failed", exc_info=True)
                # Apply exponential backoff, capped at max
                reconnect_delay = min(
                    reconnect_delay * self._config.cdp_reconnect_backoff_factor,
                    self._config.cdp_reconnect_max_delay_s,
                )
                continue
        else:
            log.error("CDP reconnect limit reached (%d), stopping capture", max_reconnects)

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

        # Disable all enabled CDP domains and close connection
        # J-RL11: include Storage.disable to pair with the Storage.enable in start()
        if self._cdp is not None:
            for domain_cmd in ("Network.disable", "Page.disable", "Storage.disable", "IndexedDB.disable"):
                try:
                    await self._cdp.send(domain_cmd)
                except Exception:
                    log.debug("Error sending %s", domain_cmd, exc_info=True)
            await self._cdp.close()
            self._cdp = None

        # Stop background writer (flushes remaining events)
        if self._writer is not None:
            try:
                self._writer.stop()
            except Exception:
                log.debug("Error stopping background writer", exc_info=True)
            self._writer = None

        # Log capture summary for diagnostics
        # G-RL14: resolve db_path once and reuse — resolved_db_path() creates
        # the parent directory on each call, so a single resolution is preferable.
        db_path = self._config.resolved_db_path()
        if db_path.exists():
            try:
                import sqlite3 as _sqlite3

                _conn = _sqlite3.connect(str(db_path))
                _req_count = _conn.execute("SELECT count(*) FROM http_requests").fetchone()[0]
                _resp_count = _conn.execute("SELECT count(*) FROM http_responses").fetchone()[0]
                _conn.close()
                log.info(
                    "Capture DB: %d requests, %d responses written to %s",
                    _req_count, _resp_count, db_path,
                )
            except Exception:
                log.debug("Could not read capture DB row counts", exc_info=True)

        # Run post-capture analysis
        if self._config.auto_analyze or self._config.auto_report:
            try:
                from proxy_relay.capture.analyzer import analyze as _analyze

                _report = _analyze(db_path)

                if self._config.auto_analyze:
                    from proxy_relay.capture.analyzer import print_report as _print_analysis

                    _print_analysis(_report)

                if self._config.auto_report:
                    from proxy_relay.capture.analyzer import write_report as _write_report

                    report_dir = self._config.resolved_report_dir()
                    _path = _write_report(
                        _report, output_dir=report_dir, profile=self._profile,
                    )
                    log.info("Analysis report written to %s", _path)
                    self._purge_old_reports(report_dir)
            except Exception:
                log.debug("Post-capture analysis skipped", exc_info=True)

        # Secure the database file (reuse db_path resolved above)
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
