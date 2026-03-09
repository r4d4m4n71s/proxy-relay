"""Exception hierarchy for proxy-relay."""
from __future__ import annotations


class ProxyRelayError(Exception):
    """Base exception for all proxy-relay errors."""


class ConfigError(ProxyRelayError):
    """Configuration loading or validation error.

    Raised when the TOML config file is missing required fields,
    has invalid values, or cannot be read.
    """


class UpstreamError(ProxyRelayError):
    """Upstream proxy connection or communication error.

    Raised when proxy-st configuration is unavailable, the upstream
    SOCKS5 proxy is unreachable, or session rotation fails.
    """


class TunnelError(ProxyRelayError):
    """CONNECT tunnel establishment or relay error.

    Raised when the SOCKS5 handshake fails, the target host is
    unreachable through the upstream, or the bidirectional relay
    encounters an unrecoverable I/O error.
    """
