"""Tests for proxy_relay.config — [capture] section parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

_PROFILES_SECTION = '\n[profiles.default]\nport = 8080\n'


class TestConfigCaptureSection:
    """Verify RelayConfig correctly parses (or omits) the [capture] section."""

    def test_config_without_capture_section_returns_none(self, tmp_path):
        """When [capture] is absent, RelayConfig.capture is None."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[server]\n'
            'host = "127.0.0.1"\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is None

    def test_config_with_capture_section_returns_capture_config(self, tmp_path):
        """When [capture] is present, RelayConfig.capture is a CaptureConfig."""
        from proxy_relay.capture.models import CaptureConfig
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[server]\n'
            'host = "127.0.0.1"\n'
            '\n'
            '[capture]\n'
            'max_body_bytes = 32768\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert isinstance(cfg.capture, CaptureConfig)

    def test_config_capture_default_domains(self, tmp_path):
        """[capture] section without 'domains' key uses the CaptureConfig defaults."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[capture]\n'
            '# no domains key\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert "tidal.com" in cfg.capture.domains
        assert "qobuz.com" in cfg.capture.domains

    def test_config_capture_custom_domains(self, tmp_path):
        """[capture] with a domains list parses into a frozenset on CaptureConfig."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[capture]\n'
            'domains = ["tidal.com", "login.tidal.com"]\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert "tidal.com" in cfg.capture.domains
        assert "login.tidal.com" in cfg.capture.domains
        assert "qobuz.com" not in cfg.capture.domains, (
            "Custom domains must override defaults, not merge with them"
        )

    def test_config_capture_custom_max_body_bytes(self, tmp_path):
        """[capture] max_body_bytes is parsed correctly."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[capture]\n'
            'max_body_bytes = 32768\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.max_body_bytes == 32768

    def test_config_capture_custom_poll_intervals(self, tmp_path):
        """[capture] poll interval fields are parsed correctly."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[capture]\n'
            'cookie_poll_interval_s = 15.0\n'
            'storage_poll_interval_s = 45.0\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.cookie_poll_interval_s == pytest.approx(15.0)
        assert cfg.capture.storage_poll_interval_s == pytest.approx(45.0)

    def test_config_capture_db_path(self, tmp_path):
        """[capture] db_path is parsed as a Path."""
        from proxy_relay.config import RelayConfig

        db_path = str(tmp_path / "capture.db")
        path = tmp_path / "config.toml"
        path.write_text(
            f'[capture]\n'
            f'db_path = "{db_path}"\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.db_path == Path(db_path)

    def test_config_capture_rotation_fields(self, tmp_path):
        """[capture] rotation and purge fields are parsed correctly."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[capture]\n'
            'min_rotate_kb = 512\n'
            'max_db_age_days = 3\n'
            'max_db_size_mb = 100\n'
            'max_db_count = 10\n'
            + _PROFILES_SECTION
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.min_rotate_kb == 512
        assert cfg.capture.max_db_age_days == 3
        assert cfg.capture.max_db_size_mb == 100
        assert cfg.capture.max_db_count == 10

    def test_config_capture_rotation_defaults(self, tmp_path):
        """[capture] without rotation fields uses CaptureConfig defaults."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text('[capture]\n' + _PROFILES_SECTION)
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.min_rotate_kb == 256
        assert cfg.capture.max_db_age_days == 7
        assert cfg.capture.max_db_count == 20

    def test_config_parse_config_dict_without_capture(self):
        """_parse_config({}) must produce capture=None, not an error."""
        from proxy_relay.config import _parse_config

        cfg = _parse_config({"profiles": {"default": {"port": 8080}}})
        assert cfg.capture is None

    def test_config_parse_config_dict_with_empty_capture_section(self):
        """_parse_config({'capture': {}}) must produce a CaptureConfig with defaults."""
        from proxy_relay.capture.models import CaptureConfig
        from proxy_relay.config import _parse_config

        cfg = _parse_config({"capture": {}, "profiles": {"default": {"port": 8080}}})
        assert cfg.capture is not None
        assert isinstance(cfg.capture, CaptureConfig)
