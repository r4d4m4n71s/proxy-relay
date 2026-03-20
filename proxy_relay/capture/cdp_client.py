"""Async CDP WebSocket client for proxy-relay capture.

``websockets`` is an optional dependency — it is imported lazily the first
time ``connect()`` is called and stored as a module-level attribute so test
patches on ``proxy_relay.capture.cdp_client.websockets`` work correctly.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# Module-level reference to the websockets package, populated lazily in
# connect() and patchable by tests via
# ``patch("proxy_relay.capture.cdp_client.websockets")``.
websockets: Any = None  # populated lazily by connect()


class CdpClient:
    """Minimal async Chrome DevTools Protocol client over WebSocket.

    Connects to a running Chromium instance with ``--remote-debugging-port``
    set.  Exposes ``send()`` for request/response calls and ``subscribe()``
    for event callbacks.

    The ``recv_loop()`` coroutine runs as a background task and dispatches
    both responses (keyed by ``id``) and events (keyed by ``method``) to
    registered listeners.

    ``websockets`` must be installed (``pip install websockets>=12.0``) to
    use this class.  The import is performed lazily inside ``connect()`` so
    that importing this module never fails at install-time.
    """

    def __init__(self) -> None:
        self._ws: Any | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._subscribers: dict[str, list[Callable[..., Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._closed: bool = False
        # G-RL10: track async subscriber tasks to prevent fire-and-forget silencing
        # exceptions and to allow cancellation on close.
        self._pending_tasks: set[asyncio.Task[None]] = set()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def recv_task(self) -> asyncio.Task[None] | None:
        """Return the active CDP receive loop task, or None if not connected.

        Exposes the internal ``_recv_task`` as a read-only public attribute so
        callers (e.g. ``CaptureSession.run_until_stopped``) can await or
        cancel the task without accessing private state (J-RL8).

        Returns:
            The background asyncio.Task running ``recv_loop()``, or None.
        """
        return self._recv_task

    async def connect(
        self,
        port: int,
        timeout: float = 10.0,
        max_retries: int = 10,
        retry_delay: float = 1.0,
    ) -> None:
        """Connect to a Chromium remote debugging endpoint.

        Fetches ``/json`` (then ``/json/version``) over HTTP to discover the
        WebSocket debugger URL, then opens a WebSocket connection and starts
        the background receive loop.

        Chromium may take several seconds to open its debug port after launch,
        so the discovery phase retries up to *max_retries* times with
        *retry_delay* seconds between attempts.

        Args:
            port: The ``--remote-debugging-port`` Chromium was started with.
            timeout: Seconds to wait for each HTTP discovery call.
            max_retries: Maximum number of discovery attempts before giving up.
            retry_delay: Seconds to wait between retries.

        Raises:
            CaptureError: If the endpoint is unreachable or the response is
                malformed.
        """
        import urllib.error
        import urllib.request

        from proxy_relay.exceptions import CaptureError

        # Populate the module-level ``websockets`` attribute so tests can
        # patch ``proxy_relay.capture.cdp_client.websockets``.
        global websockets  # noqa: PLW0603
        if websockets is None:
            try:
                import websockets as _ws
                websockets = _ws
            except ImportError as exc:
                raise CaptureError(
                    "websockets is not installed — install proxy-relay[capture]"
                ) from exc

        # Retry loop: Chromium needs time to open its debug port after launch.
        ws_url: str | None = None
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            ws_url = None

            # 1. Try /json to find a page target
            targets_url = f"http://127.0.0.1:{port}/json"
            log.debug("CDP discovery attempt %d/%d: %s", attempt, max_retries, targets_url)
            try:
                req = urllib.request.Request(targets_url)  # noqa: S310
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    targets: list[dict[str, Any]] = json.loads(resp.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        ws_url = target["webSocketDebuggerUrl"]
                        log.debug("Using page target: %s", target.get("url", "?"))
                        break
            except Exception:
                log.debug("Could not fetch /json targets (attempt %d)", attempt, exc_info=True)

            # 2. Fallback to /json/version (browser-level — limited domain support)
            if not ws_url:
                version_url = f"http://127.0.0.1:{port}/json/version"
                log.debug("No page target found, falling back to %s", version_url)
                try:
                    req = urllib.request.Request(version_url)  # noqa: S310
                    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                        version_data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
                    ws_url = version_data.get("webSocketDebuggerUrl")
                except (urllib.error.URLError, OSError) as exc:
                    last_error = exc
                except Exception as exc:
                    raise CaptureError(f"CDP discovery failed at port {port}: {exc}") from exc

            if ws_url:
                break

            # Wait before retrying
            if attempt < max_retries:
                log.debug("CDP not ready, retrying in %.1fs...", retry_delay)
                await asyncio.sleep(retry_delay)

        if not ws_url:
            if last_error and isinstance(last_error, urllib.error.URLError):
                raise CaptureError(
                    f"Could not reach Chromium CDP at port {port} after {max_retries} attempts: "
                    f"{last_error.reason}"
                ) from last_error
            raise CaptureError(
                f"No webSocketDebuggerUrl found from port {port} after {max_retries} attempts "
                f"(tried /json and /json/version)"
            )

        log.debug("Connecting to CDP WebSocket: %s", ws_url)

        try:
            self._ws = await websockets.connect(ws_url)
        except Exception as exc:
            raise CaptureError(f"WebSocket connect to {ws_url} failed: {exc}") from exc

        self._closed = False
        self._recv_task = asyncio.create_task(self.recv_loop(), name="cdp-recv")

        # Verify connection is live with a Browser.getVersion ping (uses id=1).
        try:
            await self.send("Browser.getVersion")
        except Exception:
            log.debug("Browser.getVersion probe failed — continuing anyway", exc_info=True)

        log.info("CDP connected on port %d", port)

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a CDP command and await the response.

        Args:
            method: CDP method name, e.g. ``"Network.enable"``.
            params: Optional parameters dict.

        Returns:
            The ``result`` field of the CDP response.

        Raises:
            CaptureError: If the WebSocket is not connected, the command
                times out (10 s), or the CDP response contains an error.
        """
        from proxy_relay.exceptions import CaptureError

        if self._ws is None or self._closed:
            raise CaptureError("CdpClient is not connected — call connect() first")

        msg_id = self._next_id
        self._next_id += 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = future

        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            raise CaptureError(f"CDP send failed for {method}: {exc}") from exc

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise CaptureError(f"CDP command {method!r} timed out after 10s") from None

        if "error" in result:
            err = result["error"]
            raise CaptureError(
                f"CDP error for {method!r}: {err.get('message', err)}"
            )

        return result.get("result", {})

    async def subscribe(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a CDP event.

        Multiple callbacks can be registered for the same event name.
        Both synchronous and async callbacks are supported.

        This is an ``async`` method to allow it to be awaited from async test
        fixtures and coroutines.  No I/O is performed; it returns immediately.

        Args:
            event: CDP event name, e.g. ``"Network.requestWillBeSent"``.
            callback: Called with the event ``params`` dict when the event
                arrives.  May be a plain or async callable.
        """
        self._subscribers.setdefault(event, []).append(callback)
        log.debug("Subscribed to CDP event: %s", event)

    async def recv_loop(self) -> None:
        """Receive messages from the WebSocket and dispatch them.

        Resolves pending ``send()`` futures when a response with ``id`` is
        received.  Calls subscribers when an event with ``method`` arrives.

        Both sync and async subscriber callbacks are supported.

        Exits cleanly when the WebSocket is closed or ``close()`` is called.
        Should be run as a background ``asyncio.Task``.
        """
        if self._ws is None:
            return

        try:
            while not self._closed:
                try:
                    raw = await self._ws.recv()
                except Exception:
                    # WebSocket closed or error — exit loop
                    break

                try:
                    msg: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("Non-JSON CDP message ignored")
                    continue

                if "id" in msg:
                    # Response to a send() call
                    msg_id: int = msg["id"]
                    future = self._pending.pop(msg_id, None)
                    if future is not None and not future.done():
                        future.set_result(msg)
                    # Yield to event loop so the next send() can register its
                    # future in _pending before we read the next queue message.
                    await asyncio.sleep(0)

                elif "method" in msg:
                    # Event notification
                    method: str = msg["method"]
                    params: dict[str, Any] = msg.get("params", {})
                    callbacks = self._subscribers.get(method, [])
                    for cb in callbacks:
                        try:
                            result = cb(params)
                            # Support async callbacks — track tasks to avoid
                            # fire-and-forget that silently swallows exceptions
                            # (G-RL10).
                            if asyncio.iscoroutine(result):
                                task: asyncio.Task[None] = asyncio.create_task(result)
                                self._pending_tasks.add(task)

                                def _task_done(t: asyncio.Task[None]) -> None:
                                    self._pending_tasks.discard(t)
                                    if not t.cancelled() and t.exception() is not None:
                                        log.debug(
                                            "Error in async CDP subscriber for %r: %s",
                                            method,
                                            t.exception(),
                                        )

                                task.add_done_callback(_task_done)
                        except Exception:
                            log.debug("Error in CDP subscriber for %r", method, exc_info=True)

        except Exception:
            if not self._closed:
                log.debug("CDP recv_loop ended unexpectedly", exc_info=True)

        # Cancel all pending futures on disconnect
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        log.debug("CDP recv_loop exited")

    async def close(self) -> None:
        """Close the WebSocket connection and cancel the receive task.

        Safe to call multiple times.
        """
        self._closed = True

        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._recv_task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._recv_task = None

        # Cancel tracked async subscriber tasks (G-RL10)
        for task in list(self._pending_tasks):
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                log.debug("Error closing CDP WebSocket", exc_info=True)
            self._ws = None

        log.debug("CDP client closed")
