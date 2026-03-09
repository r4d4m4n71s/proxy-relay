"""proxy-relay: local HTTP CONNECT proxy forwarding via upstream SOCKS5."""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ProxyServer",
    "RelayConfig",
    "UpstreamManager",
    "run_server",
]


def __getattr__(name: str):
    """Lazy import public API to avoid circular imports."""
    if name == "ProxyServer":
        from proxy_relay.server import ProxyServer

        return ProxyServer
    if name == "RelayConfig":
        from proxy_relay.config import RelayConfig

        return RelayConfig
    if name == "UpstreamManager":
        from proxy_relay.upstream import UpstreamManager

        return UpstreamManager
    if name == "run_server":
        from proxy_relay.server import run_server

        return run_server
    raise AttributeError(f"module 'proxy_relay' has no attribute {name!r}")
