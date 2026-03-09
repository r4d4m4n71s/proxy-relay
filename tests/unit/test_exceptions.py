"""Tests for proxy_relay.exceptions — exception hierarchy."""
from __future__ import annotations

import pytest


class TestExceptionHierarchy:
    """Verify the exception class hierarchy."""

    def test_proxy_relay_error_is_exception(self):
        from proxy_relay.exceptions import ProxyRelayError

        assert issubclass(ProxyRelayError, Exception)

    def test_config_error_is_proxy_relay_error(self):
        from proxy_relay.exceptions import ConfigError, ProxyRelayError

        assert issubclass(ConfigError, ProxyRelayError)

    def test_upstream_error_is_proxy_relay_error(self):
        from proxy_relay.exceptions import ProxyRelayError, UpstreamError

        assert issubclass(UpstreamError, ProxyRelayError)

    def test_tunnel_error_is_proxy_relay_error(self):
        from proxy_relay.exceptions import ProxyRelayError, TunnelError

        assert issubclass(TunnelError, ProxyRelayError)

    def test_config_error_message(self):
        from proxy_relay.exceptions import ConfigError

        err = ConfigError("bad port")
        assert str(err) == "bad port"

    def test_upstream_error_message(self):
        from proxy_relay.exceptions import UpstreamError

        err = UpstreamError("connection refused")
        assert str(err) == "connection refused"

    def test_tunnel_error_message(self):
        from proxy_relay.exceptions import TunnelError

        err = TunnelError("SOCKS5 handshake failed")
        assert str(err) == "SOCKS5 handshake failed"

    def test_catch_all_proxy_relay_error(self):
        from proxy_relay.exceptions import ConfigError, ProxyRelayError, TunnelError, UpstreamError

        for exc_cls in (ConfigError, UpstreamError, TunnelError):
            with pytest.raises(ProxyRelayError):
                raise exc_cls("test")
