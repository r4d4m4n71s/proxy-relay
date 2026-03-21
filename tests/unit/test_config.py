"""Tests for proxy_relay.config — RelayConfig loading and validation."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# TestProfileConfig — dataclass defaults
# ---------------------------------------------------------------------------


class TestProfileConfig:
    """Test ProfileConfig dataclass default values."""

    def test_default_values(self):
        """ProfileConfig() has correct defaults."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig()
        assert pc.port == 8080
        assert pc.browser == ""
        assert pc.rotate_interval_min == 30
        assert pc.start_url == ""

    def test_blocked_domains_default_is_none(self):
        """ProfileConfig().blocked_domains is None (resolves to TIDAL defaults)."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig()
        assert pc.blocked_domains is None

    def test_profile_config_is_frozen(self):
        """ProfileConfig is frozen — mutations raise AttributeError."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig()
        with pytest.raises((AttributeError, TypeError)):
            pc.port = 9999  # type: ignore[misc]

    def test_explicit_values_stored(self):
        """Explicitly provided values are stored correctly."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig(
            port=8081,
            browser="brave-browser",
            rotate_interval_min=15,
            start_url="https://example.com",
            blocked_domains=["example.com"],
        )
        assert pc.port == 8081
        assert pc.browser == "brave-browser"
        assert pc.rotate_interval_min == 15
        assert pc.start_url == "https://example.com"
        assert pc.blocked_domains == ["example.com"]

    def test_empty_blocked_domains_list_is_valid(self):
        """blocked_domains=[] is a valid explicit override (no blocking)."""
        from proxy_relay.config import ProfileConfig

        pc = ProfileConfig(blocked_domains=[])
        assert pc.blocked_domains == []


# ---------------------------------------------------------------------------
# TestParseProfile — _parse_profile() function
# ---------------------------------------------------------------------------


class TestParseProfile:
    """Test _parse_profile() with inheritance logic."""

    def test_parse_default_profile_all_fields(self):
        """Parses [profiles.default] with all fields present."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        data = {
            "port": 8081,
            "browser": "chromium",
            "rotate_interval_min": 20,
            "start_url": "https://example.com",
            "blocked_domains": ["tidal.com"],
        }
        pc = _parse_profile(data, "default")
        assert pc.port == 8081
        assert pc.browser == "chromium"
        assert pc.rotate_interval_min == 20
        assert pc.start_url == "https://example.com"
        assert pc.blocked_domains == ["tidal.com"]

    def test_parse_default_profile_empty_data_uses_dataclass_defaults(self):
        """Empty data dict for default profile uses ProfileConfig dataclass defaults."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        pc = _parse_profile({}, "default")
        assert pc.port == 8080
        assert pc.browser == ""
        assert pc.rotate_interval_min == 30
        assert pc.start_url == ""
        assert pc.blocked_domains is None

    def test_parse_named_profile_inherits_from_parent(self):
        """Named profile with no fields inherits everything from parent."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        parent = ProfileConfig(
            port=8080,
            browser="chromium",
            rotate_interval_min=30,
            start_url="https://parent.com",
            blocked_domains=["tidal.com"],
        )
        pc = _parse_profile({}, "miami", parent=parent)
        assert pc.port == 8080
        assert pc.browser == "chromium"
        assert pc.rotate_interval_min == 30
        assert pc.start_url == "https://parent.com"
        assert pc.blocked_domains == ["tidal.com"]

    def test_parse_named_profile_overrides_parent(self):
        """Named profile with explicit fields overrides parent values."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        parent = ProfileConfig(
            port=8080,
            browser="chromium",
            rotate_interval_min=30,
            start_url="",
            blocked_domains=["tidal.com"],
        )
        data = {
            "port": 8082,
            "browser": "brave-browser",
            "rotate_interval_min": 15,
            "start_url": "https://listen.tidal.com",
            "blocked_domains": [],
        }
        pc = _parse_profile(data, "medellin", parent=parent)
        assert pc.port == 8082
        assert pc.browser == "brave-browser"
        assert pc.rotate_interval_min == 15
        assert pc.start_url == "https://listen.tidal.com"
        assert pc.blocked_domains == []

    def test_parse_named_profile_partial_override(self):
        """Named profile with only some fields overrides only those, inherits rest."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        parent = ProfileConfig(
            port=8080,
            browser="chromium",
            rotate_interval_min=30,
            start_url="",
            blocked_domains=["tidal.com"],
        )
        data = {"port": 8081}
        pc = _parse_profile(data, "miami", parent=parent)
        # Overridden
        assert pc.port == 8081
        # Inherited
        assert pc.browser == "chromium"
        assert pc.rotate_interval_min == 30
        assert pc.start_url == ""
        assert pc.blocked_domains == ["tidal.com"]

    def test_parse_blocked_domains_empty_list_not_inherited(self):
        """blocked_domains=[] is an explicit override — NOT inherited from parent."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        parent = ProfileConfig(blocked_domains=["tidal.com", "listen.tidal.com"])
        data = {"blocked_domains": []}
        pc = _parse_profile(data, "medellin", parent=parent)
        assert pc.blocked_domains == []

    def test_parse_blocked_domains_absent_inherits_from_parent(self):
        """Absent blocked_domains inherits the parent's list."""
        from proxy_relay.config import ProfileConfig, _parse_profile

        parent = ProfileConfig(blocked_domains=["tidal.com", "listen.tidal.com", "login.tidal.com"])
        data = {"port": 8081}
        pc = _parse_profile(data, "miami", parent=parent)
        assert pc.blocked_domains == ["tidal.com", "listen.tidal.com", "login.tidal.com"]

    def test_parse_invalid_port_type_raises_config_error(self):
        """Non-integer port raises ConfigError."""
        from proxy_relay.config import _parse_profile
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError, match="port"):
            _parse_profile({"port": "eight-thousand"}, "default")

    def test_parse_empty_string_in_blocked_domains_filtered(self):
        """Empty strings in blocked_domains are filtered out during parsing."""
        from proxy_relay.config import _parse_profile

        data = {"blocked_domains": ["", "tidal.com", "", "listen.tidal.com"]}
        pc = _parse_profile(data, "default")
        assert "" not in pc.blocked_domains
        assert "tidal.com" in pc.blocked_domains
        assert "listen.tidal.com" in pc.blocked_domains

    def test_parse_port_zero_is_valid(self):
        """port=0 (OS-assigned ephemeral port) is a valid value."""
        from proxy_relay.config import _parse_profile

        pc = _parse_profile({"port": 0}, "default")
        assert pc.port == 0

    def test_parse_rotate_interval_zero_is_valid(self):
        """rotate_interval_min=0 disables rotation and is valid."""
        from proxy_relay.config import _parse_profile

        pc = _parse_profile({"rotate_interval_min": 0}, "default")
        assert pc.rotate_interval_min == 0


# ---------------------------------------------------------------------------
# TestResolveBlockedDomains — resolve_blocked_domains() function
# ---------------------------------------------------------------------------


class TestResolveBlockedDomains:
    """Test resolve_blocked_domains() logic."""

    def test_none_blocked_domains_returns_tidal_defaults(self):
        """profile.blocked_domains=None resolves to TIDAL_DOMAINS frozenset."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        profile = ProfileConfig(blocked_domains=None)
        result = resolve_blocked_domains(profile)
        assert result == TIDAL_DOMAINS
        assert isinstance(result, frozenset)

    def test_explicit_list_returns_frozenset(self):
        """Explicit blocked_domains list is returned as frozenset."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains

        profile = ProfileConfig(blocked_domains=["example.com", "other.org"])
        result = resolve_blocked_domains(profile)
        assert result == frozenset({"example.com", "other.org"})
        assert isinstance(result, frozenset)

    def test_empty_list_returns_none(self):
        """blocked_domains=[] means no blocking — returns None."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains

        profile = ProfileConfig(blocked_domains=[])
        result = resolve_blocked_domains(profile)
        assert result is None

    def test_tidal_start_url_removes_tidal_domains(self):
        """When start_url is a TIDAL URL, TIDAL domains are removed from blocked set."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        # Profile with default TIDAL blocking (None -> TIDAL_DOMAINS)
        profile = ProfileConfig(blocked_domains=None)
        result = resolve_blocked_domains(profile, start_url="https://listen.tidal.com")
        # All blocked domains are TIDAL, so after removal the set is empty → None
        assert result is None

    def test_tidal_start_url_with_explicit_tidal_blocked_domains_removes_them(self):
        """TIDAL start_url removes TIDAL domains from an explicitly set list."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        profile = ProfileConfig(
            blocked_domains=["tidal.com", "listen.tidal.com", "login.tidal.com", "example.com"]
        )
        result = resolve_blocked_domains(profile, start_url="https://tidal.com")
        assert result is not None
        assert "example.com" in result
        for domain in TIDAL_DOMAINS:
            assert domain not in result

    def test_non_tidal_start_url_keeps_blocked_set_unchanged(self):
        """Non-TIDAL start_url does not modify the blocked set."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        profile = ProfileConfig(blocked_domains=None)
        result = resolve_blocked_domains(profile, start_url="https://example.com")
        # TIDAL domains should still be blocked
        assert result == TIDAL_DOMAINS

    def test_empty_start_url_keeps_blocked_set_unchanged(self):
        """Empty string start_url (default) does not affect blocking."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        profile = ProfileConfig(blocked_domains=None)
        result = resolve_blocked_domains(profile)
        assert result == TIDAL_DOMAINS

    def test_tidal_start_url_with_no_tidal_in_blocked_keeps_non_tidal(self):
        """TIDAL start_url with non-TIDAL blocked domains leaves non-TIDAL domains alone."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains

        profile = ProfileConfig(blocked_domains=["example.com", "other.org"])
        result = resolve_blocked_domains(profile, start_url="https://listen.tidal.com")
        assert result is not None
        assert "example.com" in result
        assert "other.org" in result

    def test_tidal_start_url_with_empty_blocked_list_returns_none(self):
        """blocked_domains=[] with TIDAL start_url still returns None (no blocking)."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains

        profile = ProfileConfig(blocked_domains=[])
        result = resolve_blocked_domains(profile, start_url="https://listen.tidal.com")
        assert result is None

    def test_tidal_login_url_triggers_tidal_unblock(self):
        """login.tidal.com URL is recognized as a TIDAL URL and triggers unblock."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains
        from proxy_relay.profile_rules import TIDAL_DOMAINS

        profile = ProfileConfig(blocked_domains=None)
        result = resolve_blocked_domains(profile, start_url="https://login.tidal.com/authorize")
        # All blocked domains are TIDAL, so after removal → None
        assert result is None

    def test_returns_frozenset_type(self):
        """Return type is always frozenset (when not None)."""
        from proxy_relay.config import ProfileConfig, resolve_blocked_domains

        profile = ProfileConfig(blocked_domains=["a.com"])
        result = resolve_blocked_domains(profile)
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# TestParseConfig (updated) — _parse_config() top-level parsing
# ---------------------------------------------------------------------------


class TestParseConfig:
    """Test _parse_config() with profiles section."""

    def test_profiles_required_raises_config_error(self):
        """Config without [profiles.default] raises ConfigError."""
        from proxy_relay.config import _parse_config
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError, match="profiles.default"):
            _parse_config({})

    def test_profiles_default_parsed(self):
        """Config with [profiles.default] produces correct ProfileConfig."""
        from proxy_relay.config import _parse_config

        data = {
            "profiles": {
                "default": {
                    "port": 8080,
                    "browser": "",
                    "rotate_interval_min": 30,
                    "start_url": "",
                    "blocked_domains": ["tidal.com"],
                }
            }
        }
        cfg = _parse_config(data)
        assert "default" in cfg.profiles
        assert cfg.profiles["default"].port == 8080
        assert cfg.profiles["default"].blocked_domains == ["tidal.com"]

    def test_profiles_named_inherits_from_default(self):
        """[profiles.miami] without fields inherits from [profiles.default]."""
        from proxy_relay.config import _parse_config

        data = {
            "profiles": {
                "default": {
                    "port": 8080,
                    "browser": "chromium",
                    "blocked_domains": ["tidal.com"],
                },
                "miami": {
                    "port": 8081,
                },
            }
        }
        cfg = _parse_config(data)
        miami = cfg.profiles["miami"]
        assert miami.port == 8081
        # Inherited from default
        assert miami.browser == "chromium"
        assert miami.blocked_domains == ["tidal.com"]

    def test_profiles_named_overrides_default(self):
        """[profiles.medellin] with all fields overrides [profiles.default]."""
        from proxy_relay.config import _parse_config

        data = {
            "profiles": {
                "default": {
                    "port": 8080,
                    "browser": "chromium",
                    "blocked_domains": ["tidal.com"],
                },
                "medellin": {
                    "port": 8082,
                    "browser": "brave-browser",
                    "blocked_domains": [],
                },
            }
        }
        cfg = _parse_config(data)
        medellin = cfg.profiles["medellin"]
        assert medellin.port == 8082
        assert medellin.browser == "brave-browser"
        assert medellin.blocked_domains == []

    def test_relay_config_has_profiles_dict(self):
        """RelayConfig.profiles is a dict[str, ProfileConfig]."""
        from proxy_relay.config import ProfileConfig, _parse_config

        data = {
            "profiles": {
                "default": {"port": 8080},
                "miami": {"port": 8081},
            }
        }
        cfg = _parse_config(data)
        assert isinstance(cfg.profiles, dict)
        assert isinstance(cfg.profiles["default"], ProfileConfig)
        assert isinstance(cfg.profiles["miami"], ProfileConfig)

    def test_no_default_proxy_profile_attribute(self):
        """RelayConfig no longer has default_proxy_profile attribute."""
        from proxy_relay.config import RelayConfig

        cfg = RelayConfig()
        assert not hasattr(cfg, "default_proxy_profile"), (
            "default_proxy_profile was removed from RelayConfig"
        )

    def test_no_browse_config_attribute(self):
        """RelayConfig no longer has browse (BrowseConfig) attribute."""
        from proxy_relay.config import RelayConfig

        cfg = RelayConfig()
        assert not hasattr(cfg, "browse"), (
            "browse (BrowseConfig) was removed from RelayConfig"
        )

    def test_server_config_no_port(self):
        """ServerConfig only has host attribute, no port."""
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        assert not hasattr(sc, "port"), (
            "port was moved from ServerConfig to ProfileConfig"
        )

    def test_log_level_parsing(self):
        """log_level is parsed correctly from top-level key."""
        from proxy_relay.config import _parse_config

        data = {
            "log_level": "DEBUG",
            "profiles": {"default": {}},
        }
        cfg = _parse_config(data)
        assert cfg.log_level == "DEBUG"

    def test_multiple_named_profiles(self):
        """Multiple named profiles are all parsed into cfg.profiles."""
        from proxy_relay.config import _parse_config

        data = {
            "profiles": {
                "default": {"port": 8080},
                "miami": {"port": 8081},
                "medellin": {"port": 8082},
            }
        }
        cfg = _parse_config(data)
        assert "default" in cfg.profiles
        assert "miami" in cfg.profiles
        assert "medellin" in cfg.profiles


# ---------------------------------------------------------------------------
# TestRelayConfigLoad (updated) — RelayConfig.load() with profiles
# ---------------------------------------------------------------------------


class TestRelayConfigLoad:
    """Test RelayConfig.load() with profiles-based TOML."""

    def test_load_with_profiles_default(self, tmp_path):
        """TOML with [profiles.default] parses correctly."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[profiles.default]\n'
            'port = 8080\n'
            'browser = ""\n'
            'rotate_interval_min = 30\n'
            'start_url = ""\n'
            'blocked_domains = ["tidal.com", "listen.tidal.com", "login.tidal.com"]\n'
        )
        cfg = RelayConfig.load(path)
        assert "default" in cfg.profiles
        assert cfg.profiles["default"].port == 8080

    def test_load_missing_profiles_default_raises_config_error(self, tmp_path):
        """TOML without [profiles.default] raises ConfigError."""
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import ConfigError

        path = tmp_path / "config.toml"
        path.write_text('log_level = "INFO"\n')
        with pytest.raises(ConfigError, match="profiles.default"):
            RelayConfig.load(path)

    def test_load_malformed_toml_raises_config_error(self, malformed_toml):
        """Malformed TOML raises ConfigError."""
        from proxy_relay.config import RelayConfig
        from proxy_relay.exceptions import ConfigError

        with pytest.raises(ConfigError):
            RelayConfig.load(malformed_toml)

    def test_load_creates_default_config_when_absent(self, tmp_path):
        """When config path does not exist, create a default config file."""
        from proxy_relay.config import RelayConfig

        absent_path = tmp_path / "nonexistent" / "config.toml"
        cfg = RelayConfig.load(absent_path)

        # The default config must include [profiles.default]
        assert "default" in cfg.profiles
        assert absent_path.exists()

    def test_load_named_profile_with_inheritance(self, tmp_path):
        """Named profile in TOML correctly inherits from default."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[profiles.default]\n'
            'port = 8080\n'
            'browser = "chromium"\n'
            'blocked_domains = ["tidal.com"]\n'
            '\n'
            '[profiles.miami]\n'
            'port = 8081\n'
        )
        cfg = RelayConfig.load(path)
        miami = cfg.profiles["miami"]
        assert miami.port == 8081
        assert miami.browser == "chromium"
        assert miami.blocked_domains == ["tidal.com"]

    def test_load_none_path_uses_default_location(self, tmp_path):
        """load(None) uses the default config directory without crashing."""
        from proxy_relay import config as _config
        from proxy_relay.config import RelayConfig

        # Point CONFIG_PATH to a temp file so we don't read real config
        cfg_file = tmp_path / "config.toml"
        with patch.object(_config, "CONFIG_PATH", cfg_file):
            cfg = RelayConfig.load(None)
        assert cfg.server.host is not None


# ---------------------------------------------------------------------------
# TestServerConfig (updated) — host only, no port
# ---------------------------------------------------------------------------


class TestServerConfig:
    """Test ServerConfig — host only after port moved to ProfileConfig."""

    def test_defaults(self):
        """ServerConfig defaults to 127.0.0.1."""
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        assert sc.host == "127.0.0.1"

    def test_no_port_attribute(self):
        """ServerConfig does NOT have a port attribute (moved to ProfileConfig)."""
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        assert not hasattr(sc, "port"), "port was moved to ProfileConfig"

    def test_frozen(self):
        """ServerConfig is frozen."""
        from proxy_relay.config import ServerConfig

        sc = ServerConfig()
        with pytest.raises((AttributeError, TypeError)):
            sc.host = "0.0.0.0"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestMonitorConfig — unchanged, verify still works
# ---------------------------------------------------------------------------


class TestMonitorConfig:
    """Test MonitorConfig defaults."""

    def test_defaults(self):
        from proxy_relay.config import MonitorConfig

        mc = MonitorConfig()
        assert mc.slow_threshold_ms == 2000.0
        assert mc.error_threshold_count == 5
        assert mc.enabled is True

    def test_window_size_default(self):
        from proxy_relay.config import MonitorConfig

        mc = MonitorConfig()
        assert mc.window_size == 100

    def test_window_size_parsed_from_toml(self, tmp_path):
        """window_size can be set from TOML config."""
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            '[monitor]\n'
            'window_size = 50\n'
            '\n'
            '[profiles.default]\n'
        )
        cfg = RelayConfig.load(path)
        assert cfg.monitor.window_size == 50


# ---------------------------------------------------------------------------
# TestAntiLeakConfig — unchanged
# ---------------------------------------------------------------------------


class TestAntiLeakConfig:
    """Test AntiLeakConfig defaults."""

    def test_default_warn_timezone_mismatch(self):
        from proxy_relay.config import AntiLeakConfig

        alc = AntiLeakConfig()
        assert alc.warn_timezone_mismatch is True


# ---------------------------------------------------------------------------
# TestCaptureConfig — unchanged
# ---------------------------------------------------------------------------


class TestCaptureConfig:
    """Test CaptureConfig defaults and TOML parsing."""

    def test_defaults(self):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig()
        assert cfg.auto_analyze is True
        assert cfg.auto_report is False
        assert cfg.report_dir is None
        assert cfg.db_path is None

    def test_resolved_report_dir_default(self):
        from proxy_relay.capture.models import DEFAULT_REPORT_DIR, CaptureConfig

        cfg = CaptureConfig()
        assert cfg.resolved_report_dir() == DEFAULT_REPORT_DIR

    def test_resolved_report_dir_custom(self, tmp_path):
        from proxy_relay.capture.models import CaptureConfig

        cfg = CaptureConfig(report_dir=tmp_path / "reports")
        assert cfg.resolved_report_dir() == tmp_path / "reports"

    def test_capture_toml_auto_analyze_false(self, tmp_path):
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            "[capture]\n"
            "auto_analyze = false\n"
            "\n"
            "[profiles.default]\n"
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.auto_analyze is False  # type: ignore[union-attr]

    def test_capture_toml_auto_report_true(self, tmp_path):
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            "[capture]\n"
            "auto_report = true\n"
            "\n"
            "[profiles.default]\n"
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.auto_report is True  # type: ignore[union-attr]

    def test_capture_toml_report_dir(self, tmp_path):
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            "[capture]\n"
            'report_dir = "/tmp/my-reports"\n'
            "\n"
            "[profiles.default]\n"
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.report_dir == Path("/tmp/my-reports")  # type: ignore[union-attr]

    def test_capture_toml_defaults_when_omitted(self, tmp_path):
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            "[capture]\n"
            'domains = ["tidal.com"]\n'
            "\n"
            "[profiles.default]\n"
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is not None
        assert cfg.capture.auto_analyze is True  # type: ignore[union-attr]
        assert cfg.capture.auto_report is False  # type: ignore[union-attr]
        assert cfg.capture.report_dir is None  # type: ignore[union-attr]

    def test_no_capture_section_returns_none(self, tmp_path):
        from proxy_relay.config import RelayConfig

        path = tmp_path / "config.toml"
        path.write_text(
            'log_level = "INFO"\n'
            '\n'
            "[profiles.default]\n"
        )
        cfg = RelayConfig.load(path)
        assert cfg.capture is None
