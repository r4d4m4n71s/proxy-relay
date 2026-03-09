"""CONNECT tunnel: async SOCKS5 connect + bidirectional byte relay.

CRITICAL — DNS leak prevention:
    This module NEVER resolves hostnames locally. All target hostnames are
    passed as strings to the SOCKS5 connector, which performs remote DNS
    resolution at the upstream proxy (ATYP=0x03 in the SOCKS5 protocol).

    socket.getaddrinfo() and socket.gethostbyname() are NEVER called in
    any code path within this module.
"""
from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

from proxy_relay.exceptions import TunnelError
from proxy_relay.logger import get_logger
from proxy_relay.upstream import UpstreamInfo

log = get_logger(__name__)

class TunnelResult(NamedTuple):
    """Result of a successful tunnel establishment.

    Attributes:
        reader: AsyncIO stream reader for the tunnel.
        writer: AsyncIO stream writer for the tunnel.
        latency_ms: Time taken to establish the tunnel in milliseconds.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    latency_ms: float


# Buffer size for bidirectional relay (64 KiB)
_RELAY_BUFFER_SIZE: int = 65536

# Timeout for the SOCKS5 handshake + CONNECT phase (seconds)
_CONNECT_TIMEOUT: float = 30.0


async def open_tunnel(
    target_host: str,
    target_port: int,
    upstream: UpstreamInfo,
) -> TunnelResult:
    """Establish a SOCKS5 tunnel to the target through the upstream proxy.

    Connects to the upstream SOCKS5 proxy, performs the SOCKS5 handshake
    with username/password authentication (if configured), and issues a
    CONNECT command to the target host:port.

    IMPORTANT: ``target_host`` is passed as a hostname string — NEVER as
    a resolved IP. The SOCKS5 proxy performs DNS resolution remotely
    (ATYP=0x03), preventing DNS leaks.

    Args:
        target_host: Target hostname (passed as string, NOT resolved locally).
        target_port: Target port number.
        upstream: Parsed upstream SOCKS5 connection parameters.

    Returns:
        TunnelResult with reader, writer, and latency_ms.

    Raises:
        TunnelError: If the SOCKS5 handshake fails, the target is unreachable,
            or the connection times out.
    """
    try:
        from python_socks.async_.asyncio import Proxy
        from python_socks import ProxyType
    except ImportError as exc:
        raise TunnelError(
            "python-socks[asyncio] is not installed. "
            "Install it with: pip install 'python-socks[asyncio]'"
        ) from exc

    proxy = Proxy(
        proxy_type=ProxyType.SOCKS5,
        host=upstream.host,
        port=upstream.port,
        username=upstream.username or None,
        password=upstream.password or None,
        # rdns=True ensures remote DNS resolution (ATYP=0x03).
        # This is the CRITICAL anti-leak setting — the hostname string
        # is sent directly to the SOCKS5 server, which resolves it.
        # Local DNS is NEVER touched.
        rdns=True,
    )

    log.debug(
        "Opening SOCKS5 tunnel: %s:%d via %s:%d",
        target_host,
        target_port,
        upstream.host,
        upstream.port,
    )

    start = time.monotonic()

    try:
        sock = await asyncio.wait_for(
            proxy.connect(
                dest_host=target_host,
                dest_port=target_port,
            ),
            timeout=_CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise TunnelError(
            f"SOCKS5 tunnel to {target_host}:{target_port} timed out "
            f"after {_CONNECT_TIMEOUT:.0f}s"
        ) from exc
    except Exception as exc:
        raise TunnelError(
            f"SOCKS5 tunnel to {target_host}:{target_port} failed: {exc}"
        ) from exc

    elapsed_ms = (time.monotonic() - start) * 1000

    # Wrap the raw socket in asyncio streams
    reader, writer = await asyncio.open_connection(sock=sock)

    log.info(
        "Tunnel established: %s:%d (%.0fms)",
        target_host,
        target_port,
        elapsed_ms,
    )

    return TunnelResult(reader=reader, writer=writer, latency_ms=elapsed_ms)


async def relay_data(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    """Bidirectional byte relay between client and remote streams.

    Runs two concurrent tasks: one copying client->remote and one copying
    remote->client. When either direction reaches EOF or errors, both
    directions are closed.

    Args:
        client_reader: Reader from the local client (browser).
        client_writer: Writer to the local client (browser).
        remote_reader: Reader from the remote target (via SOCKS5 tunnel).
        remote_writer: Writer to the remote target (via SOCKS5 tunnel).
    """
    async def _pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        label: str,
    ) -> None:
        """Copy bytes from reader to writer until EOF or error.

        Args:
            reader: Source stream.
            writer: Destination stream.
            label: Direction label for logging (e.g., "client->remote").
        """
        total_bytes = 0
        try:
            while True:
                data = await reader.read(_RELAY_BUFFER_SIZE)
                if not data:
                    log.debug("EOF on %s after %d bytes", label, total_bytes)
                    break
                writer.write(data)
                await writer.drain()
                total_bytes += len(data)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            log.debug("Connection closed on %s after %d bytes: %s", label, total_bytes, exc)
        except asyncio.CancelledError:
            log.debug("Relay cancelled on %s after %d bytes", label, total_bytes)
        finally:
            try:
                writer.close()
            except OSError:
                pass

    task_c2r = asyncio.create_task(_pipe(client_reader, remote_writer, "client->remote"))
    task_r2c = asyncio.create_task(_pipe(remote_reader, client_writer, "remote->client"))

    # Wait for either direction to finish, then cancel the other
    done, pending = await asyncio.wait(
        {task_c2r, task_r2c},
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    # Await cancelled tasks to suppress warnings
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Check for exceptions in completed tasks
    for task in done:
        exc = task.exception()
        if exc is not None:
            log.debug("Relay task exception: %s", exc)
