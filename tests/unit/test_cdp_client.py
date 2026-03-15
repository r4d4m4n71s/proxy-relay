"""Tests for proxy_relay.capture.cdp_client — CdpClient async WebSocket CDP client."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ws_factory():
    """Return a factory that builds a controllable fake WebSocket and its queues."""

    def _make():
        recv_queue: asyncio.Queue = asyncio.Queue()
        sent_messages: list[str] = []

        class FakeWS:
            closed = False

            async def send(self, data: str) -> None:
                sent_messages.append(data)

            async def recv(self) -> str:
                return await recv_queue.get()

            async def close(self) -> None:
                self.closed = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

        ws = FakeWS()
        return ws, recv_queue, sent_messages

    return _make


@pytest.fixture
def json_version_response():
    """Canonical /json/version response with a webSocketDebuggerUrl."""
    return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"})


# ---------------------------------------------------------------------------
# 1. Connection bootstrapping
# ---------------------------------------------------------------------------


class TestCdpClientConnect:
    """Verify connect() discovers the WebSocket URL and opens a connection."""

    async def test_connect_discovers_ws_url(self, fake_ws_factory, json_version_response):
        """connect() fetches /json/version and calls websockets.connect with the URL."""
        from proxy_relay.capture.cdp_client import CdpClient

        ws, recv_queue, sent_messages = fake_ws_factory()

        mock_response = MagicMock()
        mock_response.read.return_value = json_version_response.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_response

        connected_urls: list[str] = []

        async def fake_connect(url, **kwargs):
            connected_urls.append(url)
            return ws

        with patch("urllib.request.urlopen", return_value=mock_response), \
             patch("proxy_relay.capture.cdp_client.websockets") as mock_ws_module:
            mock_ws_module.connect = fake_connect

            client = CdpClient()
            # Provide dummy recv so start_listening doesn't block
            recv_queue.put_nowait(json.dumps({"id": 1, "result": {}}))
            await client.connect(9222)

        assert any("ws://" in url for url in connected_urls), (
            f"Expected websockets.connect called with ws:// URL, got: {connected_urls}"
        )

    async def test_connect_timeout_raises_capture_error(self):
        """connect() wraps urllib timeout as CaptureError."""

        from proxy_relay.capture.cdp_client import CdpClient
        from proxy_relay.exceptions import CaptureError

        def timeout_urlopen(*args, **kwargs):
            raise TimeoutError("connection timed out")

        with patch("urllib.request.urlopen", side_effect=timeout_urlopen):
            client = CdpClient()
            with pytest.raises(CaptureError):
                await client.connect(9222, max_retries=1)

    async def test_connect_url_error_raises_capture_error(self):
        """connect() wraps URLError as CaptureError."""
        import urllib.error

        from proxy_relay.capture.cdp_client import CdpClient
        from proxy_relay.exceptions import CaptureError

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            client = CdpClient()
            with pytest.raises(CaptureError):
                await client.connect(9222, max_retries=1)


# ---------------------------------------------------------------------------
# 2. send() — command/response pairing
# ---------------------------------------------------------------------------


class TestCdpClientSend:
    """Verify send() dispatches commands and returns matched results."""

    async def _make_connected_client(self, fake_ws_factory, json_version_response):
        """Helper: return a CdpClient pre-connected via mocked websockets."""
        from proxy_relay.capture.cdp_client import CdpClient

        ws, recv_queue, sent_messages = fake_ws_factory()

        mock_response = MagicMock()
        mock_response.read.return_value = json_version_response.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        async def fake_connect(url, **kwargs):
            return ws

        with patch("urllib.request.urlopen", return_value=mock_response), \
             patch("proxy_relay.capture.cdp_client.websockets") as mock_ws_module:
            mock_ws_module.connect = fake_connect
            client = CdpClient()
            # Seed a dummy result for the initial enable command if any
            recv_queue.put_nowait(json.dumps({"id": 1, "result": {}}))
            await client.connect(9222)

        return client, ws, recv_queue, sent_messages

    async def test_send_returns_result(self, fake_ws_factory, json_version_response):
        """send() returns the result dict from the matched response."""
        client, ws, recv_queue, sent_messages = await self._make_connected_client(
            fake_ws_factory, json_version_response
        )

        # Queue the response before sending
        expected_result = {"sessionId": "abc123"}
        recv_queue.put_nowait(json.dumps({"id": 2, "result": expected_result}))

        result = await client.send("Target.createTarget", {"url": "about:blank"})
        assert result == expected_result

    async def test_send_increments_id(self, fake_ws_factory, json_version_response):
        """Consecutive send() calls use incrementing message IDs."""
        client, ws, recv_queue, sent_messages = await self._make_connected_client(
            fake_ws_factory, json_version_response
        )

        recv_queue.put_nowait(json.dumps({"id": 2, "result": {}}))
        recv_queue.put_nowait(json.dumps({"id": 3, "result": {}}))

        await client.send("Network.enable")
        await client.send("Runtime.enable")

        # Decode all messages sent (excluding the initial connection phase)
        ids = [json.loads(m)["id"] for m in sent_messages]
        # IDs must be strictly increasing
        assert ids == sorted(ids), f"IDs not monotonically increasing: {ids}"
        assert len(set(ids)) == len(ids), f"Duplicate IDs detected: {ids}"


# ---------------------------------------------------------------------------
# 3. subscribe() — event dispatching
# ---------------------------------------------------------------------------


class TestCdpClientSubscribe:
    """Verify subscribe() dispatches CDP events to registered callbacks."""

    async def test_subscribe_dispatches_events(self, fake_ws_factory, json_version_response):
        """Registered callback is invoked when a matching CDP event arrives."""
        from proxy_relay.capture.cdp_client import CdpClient

        ws, recv_queue, sent_messages = fake_ws_factory()

        mock_response = MagicMock()
        mock_response.read.return_value = json_version_response.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        async def fake_connect(url, **kwargs):
            return ws

        received_params: list[dict] = []

        async def on_request(params: dict) -> None:
            received_params.append(params)

        with patch("urllib.request.urlopen", return_value=mock_response), \
             patch("proxy_relay.capture.cdp_client.websockets") as mock_ws_module:
            mock_ws_module.connect = fake_connect
            client = CdpClient()
            recv_queue.put_nowait(json.dumps({"id": 1, "result": {}}))
            await client.connect(9222)

        await client.subscribe("Network.requestWillBeSent", on_request)

        # Inject a CDP event
        event_payload = {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "1", "request": {"url": "https://api.tidal.com"}},
        }
        recv_queue.put_nowait(json.dumps(event_payload))

        # Give the recv loop a chance to dispatch
        await asyncio.sleep(0.05)

        assert len(received_params) == 1
        assert received_params[0]["requestId"] == "1"


# ---------------------------------------------------------------------------
# 4. close() — idempotency and cleanup
# ---------------------------------------------------------------------------


class TestCdpClientClose:
    """Verify close() is safe to call multiple times."""

    async def test_close_is_idempotent(self, fake_ws_factory, json_version_response):
        """Calling close() twice does not raise."""
        from proxy_relay.capture.cdp_client import CdpClient

        ws, recv_queue, sent_messages = fake_ws_factory()

        mock_response = MagicMock()
        mock_response.read.return_value = json_version_response.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        async def fake_connect(url, **kwargs):
            return ws

        with patch("urllib.request.urlopen", return_value=mock_response), \
             patch("proxy_relay.capture.cdp_client.websockets") as mock_ws_module:
            mock_ws_module.connect = fake_connect
            client = CdpClient()
            recv_queue.put_nowait(json.dumps({"id": 1, "result": {}}))
            await client.connect(9222)

        # Should not raise on first or second call
        await client.close()
        await client.close()

    async def test_close_before_connect_is_safe(self):
        """close() on a not-yet-connected client does not raise."""
        from proxy_relay.capture.cdp_client import CdpClient

        client = CdpClient()
        await client.close()  # Must not raise

    async def test_recv_loop_exits_on_close(self, fake_ws_factory, json_version_response):
        """Closing the WebSocket causes the recv loop to exit without error."""
        from proxy_relay.capture.cdp_client import CdpClient

        ws, recv_queue, sent_messages = fake_ws_factory()

        mock_response = MagicMock()
        mock_response.read.return_value = json_version_response.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        async def fake_connect(url, **kwargs):
            return ws

        with patch("urllib.request.urlopen", return_value=mock_response), \
             patch("proxy_relay.capture.cdp_client.websockets") as mock_ws_module:
            mock_ws_module.connect = fake_connect
            client = CdpClient()
            recv_queue.put_nowait(json.dumps({"id": 1, "result": {}}))
            await client.connect(9222)

        # Closing the client should stop the loop gracefully
        await client.close()
        # Give the loop a moment to notice the close
        await asyncio.sleep(0.05)
        # No assertion needed beyond "no exception raised"
