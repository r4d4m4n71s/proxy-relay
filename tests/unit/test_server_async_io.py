"""Tests for C4-17: async I/O refactor in ProxyServer.

Verifies that blocking I/O calls in ProxyServer are offloaded to a thread
pool via asyncio.to_thread() instead of being called directly on the event
loop, and that all observable behaviour (status files, upstream rotation,
connection counters) remains correct after the refactor.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from proxy_relay.config import MonitorConfig
from proxy_relay.server import ProxyServer
from proxy_relay.upstream import UpstreamInfo, UpstreamManager


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_upstream() -> UpstreamInfo:
    return UpstreamInfo(
        host="proxy.example.com",
        port=12322,
        username="user",
        password="pass",
        url="socks5://***@proxy.example.com:12322",
        country="us",
    )


def _make_manager(upstream: UpstreamInfo | None = None) -> MagicMock:
    mgr = MagicMock(spec=UpstreamManager)
    mgr.get_upstream.return_value = upstream or _make_upstream()
    mgr.rotate.return_value = upstream or _make_upstream()
    mgr.profile_name = "browse"
    return mgr


def _mock_asyncio_server() -> MagicMock:
    """Return a mock asyncio.Server accepted by asyncio.start_server."""
    mock_srv = AsyncMock()
    sock = MagicMock()
    sock.getsockname.return_value = ("127.0.0.1", 18090)
    mock_srv.sockets = [sock]
    mock_srv.close = MagicMock()
    mock_srv.wait_closed = AsyncMock()
    return mock_srv


# ---------------------------------------------------------------------------
# Helper: run start() with all external I/O mocked.
# Returns (server, to_thread_calls) so tests can inspect what to_thread saw.
# ---------------------------------------------------------------------------

async def _start_server(server: ProxyServer) -> list:
    """Run server.start() and return the list of asyncio.to_thread call args."""
    to_thread_calls: list = []

    original_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *args, **kwargs):  # noqa: ANN001
        to_thread_calls.append(call(fn, *args, **kwargs))
        # Actually execute the function synchronously so callers get a real
        # return value (get_upstream, write_pid, _update_status_file).
        return fn(*args, **kwargs)

    mock_srv = _mock_asyncio_server()

    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.start_server", new_callable=AsyncMock, return_value=mock_srv),
        patch("proxy_relay.server.write_pid"),
        patch("proxy_relay.server.write_status"),
        patch("asyncio.get_running_loop", return_value=MagicMock()),
    ):
        await server.start()

    return to_thread_calls


# ===========================================================================
# TestUpdateStatusFileAsync
# ===========================================================================


class TestUpdateStatusFileAsync:
    """_update_status_file_async() wraps the sync method via asyncio.to_thread."""

    async def test_delegates_to_sync_via_to_thread(self) -> None:
        """_update_status_file_async must call asyncio.to_thread(_update_status_file, ...)."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18090, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            await server._update_status_file_async()

        # E-RL8: _update_status_file_async now snapshots state and passes args
        mock_to_thread.assert_called_once()
        call_args = mock_to_thread.call_args
        assert call_args.args[0] == server._update_status_file

    async def test_async_wrapper_exists_and_is_coroutine(self) -> None:
        """_update_status_file_async must be an async method (awaitable)."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18090, upstream_manager=mgr)
        server._upstream = _make_upstream()

        assert asyncio.iscoroutinefunction(server._update_status_file_async), (
            "_update_status_file_async must be declared with 'async def'"
        )

    async def test_sync_method_still_callable_directly(self) -> None:
        """The underlying _update_status_file() sync method must still exist and work."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18090, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("proxy_relay.server.write_status") as mock_write:
            # E-RL8: _update_status_file now requires explicit args (snapshotted state)
            server._update_status_file(
                stats_dict=None,
                profile="browse",
                upstream_url=server._upstream.url,
                country=server._upstream.country,
                active_connections=0,
                total_connections=0,
            )

        mock_write.assert_called_once()

    async def test_async_wrapper_produces_same_side_effects_as_sync(self) -> None:
        """Awaiting _update_status_file_async() writes the status file exactly once."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18091, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("proxy_relay.server.write_status") as mock_write:
            # Run the real async wrapper (without mocking to_thread) to
            # confirm end-to-end behaviour is preserved.
            await server._update_status_file_async()

        mock_write.assert_called_once()

    async def test_noop_when_upstream_is_none(self) -> None:
        """_update_status_file_async must be a no-op when upstream is not set."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18090, upstream_manager=mgr)
        # _upstream is None by default (before start() is called)

        with patch("proxy_relay.server.write_status") as mock_write:
            await server._update_status_file_async()

        mock_write.assert_not_called()


# ===========================================================================
# TestStartUsesToThread
# ===========================================================================


class TestStartUsesToThread:
    """start() offloads get_upstream() and write_pid() via asyncio.to_thread."""

    async def test_get_upstream_called_via_to_thread(self) -> None:
        """start() must call asyncio.to_thread(manager.get_upstream), not get_upstream() directly."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18092, upstream_manager=mgr)

        to_thread_calls = await _start_server(server)

        # The callable passed as the first positional arg to to_thread must be
        # the manager's get_upstream method.
        thread_fns = [c.args[0] for c in to_thread_calls]
        assert mgr.get_upstream in thread_fns, (
            "asyncio.to_thread was not called with manager.get_upstream; "
            f"got: {thread_fns}"
        )

    async def test_write_pid_called_via_to_thread(self) -> None:
        """start() must call asyncio.to_thread(write_pid, pid_path), not write_pid() directly."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18093, upstream_manager=mgr)

        to_thread_calls = await _start_server(server)

        # When proxy_relay.server.write_pid is patched with a MagicMock,
        # __name__ is absent but _mock_name holds the callable's name.
        # For real functions, __name__ is the authoritative source.
        def _fn_name(fn: object) -> str:
            return (
                getattr(fn, "__name__", None)
                or getattr(fn, "_mock_name", None)
                or repr(fn)
            )

        thread_fn_names = [_fn_name(c.args[0]) for c in to_thread_calls]
        assert "write_pid" in thread_fn_names, (
            "asyncio.to_thread was not called with a callable named 'write_pid'; "
            f"names seen: {thread_fn_names}"
        )

    async def test_update_status_called_via_to_thread_on_start(self) -> None:
        """start() must update status via _update_status_file_async (not sync _update_status_file)."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18094, upstream_manager=mgr)

        # Spy on _update_status_file_async to confirm start() calls it.
        original_async_fn = server._update_status_file_async
        async_calls: list[int] = []

        async def spy_async() -> None:
            async_calls.append(1)
            await original_async_fn()

        server._update_status_file_async = spy_async  # type: ignore[method-assign]

        # Use a real-enough to_thread shim so the rest of start() works.
        async def fake_to_thread(fn, *args, **kwargs):  # noqa: ANN001
            return fn(*args, **kwargs)

        mock_srv = _mock_asyncio_server()
        with (
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch("asyncio.start_server", new_callable=AsyncMock, return_value=mock_srv),
            patch("proxy_relay.server.write_pid"),
            patch("proxy_relay.server.write_status"),
            patch("asyncio.get_running_loop", return_value=MagicMock()),
        ):
            await server.start()

        assert async_calls, "start() did not call _update_status_file_async()"

    async def test_start_sets_upstream_from_to_thread_result(self) -> None:
        """The UpstreamInfo returned by get_upstream (via to_thread) must be stored."""
        expected = _make_upstream()
        mgr = _make_manager(upstream=expected)
        server = ProxyServer(host="127.0.0.1", port=18095, upstream_manager=mgr)

        await _start_server(server)

        assert server._upstream is expected, (
            "server._upstream was not set from the result of asyncio.to_thread(get_upstream)"
        )

    async def test_start_with_no_manager_returns_without_to_thread(self) -> None:
        """start() without an upstream manager must return early without calling to_thread."""
        server = ProxyServer(host="127.0.0.1", port=18096)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            await server.start()

        mock_to_thread.assert_not_called()


# ===========================================================================
# TestDoRotateUsesToThread
# ===========================================================================


class TestDoRotateUsesToThread:
    """_do_rotate() offloads manager.rotate() via asyncio.to_thread."""

    async def test_rotate_called_via_to_thread(self) -> None:
        """_do_rotate() must call asyncio.to_thread(manager.rotate), not rotate() directly."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18097, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = _make_upstream()
            await server._do_rotate()

        # Inspect every call to to_thread; at least one must pass manager.rotate.
        thread_fns = [c.args[0] for c in mock_to_thread.call_args_list]
        assert mgr.rotate in thread_fns, (
            "_do_rotate() did not call asyncio.to_thread with manager.rotate; "
            f"got: {thread_fns}"
        )

    async def test_rotate_updates_upstream_with_new_info(self) -> None:
        """After _do_rotate(), server._upstream must reflect the rotated upstream."""
        new_upstream = UpstreamInfo(
            host="new-proxy.example.com",
            port=12323,
            username="user2",
            password="pass2",
            url="socks5://***@new-proxy.example.com:12323",
            country="de",
        )
        mgr = _make_manager()
        mgr.rotate.return_value = new_upstream
        server = ProxyServer(host="127.0.0.1", port=18098, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("proxy_relay.server.write_status"):
            await server._do_rotate()

        assert server._upstream is new_upstream, (
            "server._upstream was not updated to the rotated upstream"
        )

    async def test_rotate_calls_update_status_file_async(self) -> None:
        """After successful rotation, _do_rotate() must call _update_status_file_async."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18099, upstream_manager=mgr)
        server._upstream = _make_upstream()

        async_status_calls: list[int] = []
        original = server._update_status_file_async

        async def spy():
            async_status_calls.append(1)
            # Run the real impl so write_status is called (patched below).
            await original()

        server._update_status_file_async = spy  # type: ignore[method-assign]

        with patch("proxy_relay.server.write_status"):
            await server._do_rotate()

        assert async_status_calls, (
            "_do_rotate() did not call _update_status_file_async after rotation"
        )

    async def test_rotate_without_manager_does_not_crash(self) -> None:
        """_do_rotate() must be a no-op (log warning) when no upstream manager is set."""
        server = ProxyServer(host="127.0.0.1", port=18097)
        server._upstream = _make_upstream()

        # No exception should propagate.
        await server._do_rotate()

        assert server._upstream is not None  # upstream unchanged

    async def test_rotate_exception_keeps_current_upstream(self) -> None:
        """When rotate() raises, _do_rotate() must retain the original upstream."""
        mgr = _make_manager()
        mgr.rotate.side_effect = RuntimeError("session expired")
        server = ProxyServer(host="127.0.0.1", port=18097, upstream_manager=mgr)
        original_upstream = _make_upstream()
        server._upstream = original_upstream

        # Must not raise; upstream must be unchanged.
        await server._do_rotate()

        assert server._upstream is original_upstream, (
            "_do_rotate() changed upstream even though rotation raised an exception"
        )


# ===========================================================================
# TestOnConnectionUsesAsyncStatusUpdate
# ===========================================================================


class TestOnConnectionUsesAsyncStatusUpdate:
    """_on_connection() finally block calls _update_status_file_async (not sync)."""

    async def test_on_connection_finally_calls_async_status_update(self) -> None:
        """After a connection completes, status is updated via _update_status_file_async."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18100, upstream_manager=mgr)
        server._upstream = _make_upstream()

        async_status_calls: list[int] = []

        async def spy_async_status():
            async_status_calls.append(1)

        server._update_status_file_async = spy_async_status  # type: ignore[method-assign]

        # Simulate a completed connection by mocking handle_connection.
        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)

        with patch(
            "proxy_relay.server.handle_connection",
            new_callable=AsyncMock,
        ) as mock_handle:
            mock_handle.return_value = None
            await server._on_connection(reader, writer)

        assert async_status_calls, (
            "_on_connection finally block did not call _update_status_file_async"
        )

    async def test_on_connection_finally_calls_async_status_update_on_error(self) -> None:
        """Even when handle_connection raises, status is updated via _update_status_file_async."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18100, upstream_manager=mgr)
        server._upstream = _make_upstream()

        async_status_calls: list[int] = []

        async def spy_async_status():
            async_status_calls.append(1)

        server._update_status_file_async = spy_async_status  # type: ignore[method-assign]

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)

        with patch(
            "proxy_relay.server.handle_connection",
            new_callable=AsyncMock,
            side_effect=ConnectionResetError("peer reset"),
        ):
            with pytest.raises(ConnectionResetError):
                await server._on_connection(reader, writer)

        assert async_status_calls, (
            "_on_connection finally block did not call _update_status_file_async even after error"
        )

    async def test_on_connection_decrements_active_connections_in_finally(self) -> None:
        """Connection counter is always decremented, even when handle_connection raises."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18101, upstream_manager=mgr)
        server._upstream = _make_upstream()

        async def noop_status():
            pass

        server._update_status_file_async = noop_status  # type: ignore[method-assign]

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)

        with patch(
            "proxy_relay.server.handle_connection",
            new_callable=AsyncMock,
            side_effect=OSError("broken pipe"),
        ):
            with pytest.raises(OSError):
                await server._on_connection(reader, writer)

        assert server._active_connections == 0, (
            "active_connections was not decremented in the finally block"
        )
        assert server._total_connections == 1, (
            "total_connections was not incremented before the error"
        )


# ===========================================================================
# TestAsyncIoRefactorBackwardCompatibility
# ===========================================================================


class TestAsyncIoRefactorBackwardCompatibility:
    """Verify that the async refactor preserves all observable behaviour."""

    async def test_start_end_to_end_observable_state(self) -> None:
        """After start(), server._upstream is populated and server is running."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18102, upstream_manager=mgr)

        await _start_server(server)

        assert server._upstream is not None
        assert server._upstream.host == "proxy.example.com"
        assert server._upstream.country == "us"

    async def test_do_rotate_end_to_end_upstream_changes(self) -> None:
        """_do_rotate() end-to-end: upstream ref is replaced after successful rotate."""
        new_upstream = UpstreamInfo(
            host="rotated.example.com",
            port=12400,
            username="u",
            password="p",
            url="socks5://***@rotated.example.com:12400",
            country="gb",
        )
        mgr = _make_manager()
        mgr.rotate.return_value = new_upstream

        server = ProxyServer(host="127.0.0.1", port=18103, upstream_manager=mgr)
        server._upstream = _make_upstream()

        with patch("proxy_relay.server.write_status"):
            await server._do_rotate()

        assert server._upstream.host == "rotated.example.com"
        assert server._upstream.country == "gb"

    async def test_monitor_still_created_on_start_when_enabled(self) -> None:
        """Connection monitor creation survives the async refactor."""
        mgr = _make_manager()
        monitor_cfg = MonitorConfig(
            enabled=True,
            slow_threshold_ms=1000.0,
            error_threshold_count=3,
            window_size=50,
        )
        server = ProxyServer(
            host="127.0.0.1",
            port=18104,
            upstream_manager=mgr,
            monitor_config=monitor_cfg,
        )

        await _start_server(server)

        assert server.monitor_stats is not None, (
            "ConnectionMonitor was not created during start() after async refactor"
        )

    async def test_multiple_connections_counter_consistent(self) -> None:
        """total_connections increments once per connection; active_connections returns to 0."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18105, upstream_manager=mgr)
        server._upstream = _make_upstream()

        async def noop_status():
            pass

        server._update_status_file_async = noop_status  # type: ignore[method-assign]

        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)

        with patch("proxy_relay.server.handle_connection", new_callable=AsyncMock):
            await server._on_connection(reader, writer)
            await server._on_connection(reader, writer)

        assert server._total_connections == 2
        assert server._active_connections == 0

    @pytest.mark.parametrize(
        "to_thread_target",
        [
            pytest.param("get_upstream", id="get_upstream-offloaded"),
            pytest.param("write_pid", id="write_pid-offloaded"),
        ],
    )
    async def test_start_to_thread_targets(self, to_thread_target: str) -> None:
        """Each blocking call in start() must be routed through asyncio.to_thread."""
        mgr = _make_manager()
        server = ProxyServer(host="127.0.0.1", port=18106, upstream_manager=mgr)

        seen_fn_names: set[str] = set()

        async def recording_to_thread(fn, *args, **kwargs):  # noqa: ANN001
            # For real functions, __name__ is authoritative.
            # For MagicMock callables (patched functions/methods), _mock_name
            # holds the short name (e.g. "get_upstream", "write_pid").
            name = (
                getattr(fn, "__name__", None)
                or getattr(fn, "_mock_name", None)
                or repr(fn)
            )
            seen_fn_names.add(name)
            return fn(*args, **kwargs)

        mock_srv = _mock_asyncio_server()
        with (
            patch("asyncio.to_thread", side_effect=recording_to_thread),
            patch("asyncio.start_server", new_callable=AsyncMock, return_value=mock_srv),
            patch("proxy_relay.server.write_pid"),
            patch("proxy_relay.server.write_status"),
            patch("asyncio.get_running_loop", return_value=MagicMock()),
        ):
            await server.start()

        assert to_thread_target in seen_fn_names, (
            f"asyncio.to_thread was not called with '{to_thread_target}'; "
            f"functions seen: {seen_fn_names}"
        )
