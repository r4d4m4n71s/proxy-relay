"""Tests for proxy_relay.config — RelayConfig loading and validation."""
from __future__ import annotations

from pathlib import Path

import pytest


class TestRelayConfigLoad:
    """Test RelayConfig.load() with various TOML inputs."""

    def test_load_full_toml_all_fields(self, tmp_path):
        """Full TOML populates every field correctly."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            'log_level = "DEBUG"\n'
            'proxy_st_profile = "stealth"\n'
            '\n'
            '[server]\n'
            'host = "0.0.0.0"\n'
            'port = 9090\n'
            '\n'
            '[monitor]\n'
            'slow_threshold_ms = 3000.0\n'
            'error_threshold_count = 10\n'
            'enabled = false\n'
            '\n'
            '[anti_leak]\n'
            'warn_timezone_mismatch = false\n'
        )

        cfg = RelayConfig.load(path)

        assert cfg.log_level == "DEBUG"
        assert cfg.proxy_st_profile == "stealth"
        assert cfg.server.host == "0.0.0.0"
        assert cfg.server.port == 9090
        assert cfg.monitor.slow_threshold_ms == 3000.0
        assert cfg.monitor.error_threshold_count == 10
        assert cfg.monitor.enabled is False
        assert cfg.anti_leak.warn_timezone_mismatch is False

    def test_load_minimal_toml_defaults(self, minimal_toml):
        """Minimal TOML fills missing fields with defaults."""
        from proxy_relay.config import RelayConfig

        cfg = RelayConfig.load(minimal_toml)

        assert cfg.log_level == "INFO"
        assert cfg.proxy_st_profile == "browse"
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8080
        assert cfg.monitor.enabled is True
        assert cfg.anti_leak.warn_timezone_mismatch is True

    def test_load_creates_default_config_when_absent(self, tmp_path):
        """When config path does not exist, create a default config file."""
        from proxy_relay.config import RelayConfig

        absent_path = tmp_path / "nonexistent" / "config.toml"
        cfg = RelayConfig.load(absent_path)

        # Should return valid defaults
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8080
        # The file should now exist
        assert absent_path.exists()

    def test_load_malformed_toml_raises_config_error(self, malformed_toml):
        """Malformed TOML raises ConfigError."""
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError):
            RelayConfig.load(malformed_toml)

    def test_load_invalid_port_raises_config_error(self, tmp_path):
        """Port outside valid range raises ConfigError."""
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import ConfigError

        path = tmp_path / "config.toml"
        path.write_text(
            '[server]\n'
            'port = 99999\n'
        )
        with pytest.raises(ConfigError):
            RelayConfig.load(path)

    def test_load_negative_port_raises_config_error(self, tmp_path):
        """Negative port raises ConfigError."""
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import ConfigError

        path = tmp_path / "config.toml"
        path.write_text(
            '[server]\n'
            'port = -1\n'
        )
        with pytest.raises(ConfigError):
            RelayConfig.load(path)

    def test_load_none_path_uses_default_location(self, tmp_path, monkeypatch):
        """load(None) uses the default config directory."""
        from proxy_relay.config import RelayConfig

        # This should not raise — it uses the default path
        cfg = RelayConfig.load(None)
        assert cfg.server.host is not None


class TestServerConfig:
    """Test ServerConfig defaults."""

    def test_defaults(self):
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        assert sc.host == "127.0.0.1"
        assert sc.port == 8080

    def test_frozen(self):
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        with pytest.raises(AttributeError):
            sc.host = "0.0.0.0"  # type: ignore[misc]


class TestMonitorConfig:
    """Test MonitorConfig defaults."""

    def test_defaults(self):
        from proxy_relay.config import MonitorConfig

        mc = MonitorConfig()
        assert mc.slow_threshold_ms == 2000.0
        assert mc.error_threshold_count == 5
        assert mc.enabled is True


class TestAntiLeakConfig:
    """Test AntiLeakConfig defaults."""

    def test_default_warn_timezone_mismatch(self):
        from proxy_relay.config import AntiLeakConfig

        alc = AntiLeakConfig()
        assert alc.warn_timezone_mismatch is True
