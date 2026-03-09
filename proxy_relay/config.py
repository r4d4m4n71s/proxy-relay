"""TOML configuration loader for proxy-relay."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from proxy_relay.exceptions import ConfigError
from proxy_relay.logger import get_logger

log = get_logger(__name__)

CONFIG_DIR: Path = Path.home() / ".config" / "proxy-relay"
CONFIG_PATH: Path = CONFIG_DIR / "config.toml"

_VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

_DEFAULT_CONFIG: str = """\
# proxy-relay configuration
# Local HTTP CONNECT proxy forwarding traffic through upstream SOCKS5 via proxy-st.

# log_level = "INFO"

# proxy-st profile name to use for upstream SOCKS5 connections.
proxy_st_profile = "browse"

[server]
host = "127.0.0.1"
port = 8080

[monitor]
# enabled = true
# slow_threshold_ms = 2000.0
# error_threshold_count = 5
# window_size = 100

[anti_leak]
# warn_timezone_mismatch = true

[browse]
# rotate_interval_min = 30  # auto-rotate every N minutes (0 = disabled)
"""


@dataclass(frozen=True)
class ServerConfig:
    """Local proxy server bind settings."""

    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True)
class MonitorConfig:
    """Connection monitoring thresholds."""

    enabled: bool = True
    slow_threshold_ms: float = 2000.0
    error_threshold_count: int = 5
    window_size: int = 100


@dataclass(frozen=True)
class AntiLeakConfig:
    """Anti-leak detection settings."""

    warn_timezone_mismatch: bool = True


@dataclass(frozen=True)
class BrowseConfig:
    """Browser launch and auto-rotation settings."""

    rotate_interval_min: int = 30


@dataclass
class RelayConfig:
    """Root configuration for proxy-relay."""

    log_level: str = "INFO"
    proxy_st_profile: str = "browse"
    server: ServerConfig = field(default_factory=ServerConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    anti_leak: AntiLeakConfig = field(default_factory=AntiLeakConfig)
    browse: BrowseConfig = field(default_factory=BrowseConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> RelayConfig:
        """Load configuration from TOML file.

        Creates the config directory and a default config if it does not exist.
        File permissions are set to 0o600 (owner-only read/write).

        Args:
            path: Path to the TOML config file. Defaults to
                ``~/.config/proxy-relay/config.toml``.

        Returns:
            Populated RelayConfig instance.

        Raises:
            ConfigError: If the file is malformed or has invalid values.
        """
        return load_config(path or CONFIG_PATH)


def load_config(path: Path = CONFIG_PATH) -> RelayConfig:
    """Load configuration from TOML file.

    Creates the config directory and a default config if it does not exist.
    File permissions are set to 0o600 (owner-only read/write).

    Args:
        path: Path to the TOML config file.

    Returns:
        Populated RelayConfig instance.

    Raises:
        ConfigError: If the file is malformed or has invalid values.
    """
    path = Path(path)

    if not path.exists():
        log.info("Config file not found, creating default at %s", path)
        _create_default_config(path)

    try:
        raw = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Malformed TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc

    return _parse_config(data)


def _create_default_config(path: Path) -> None:
    """Write the default config template and set permissions.

    Args:
        path: Destination path for the config file.

    Raises:
        ConfigError: If the directory or file cannot be created.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        os.chmod(path, 0o600)
        log.info("Default config created at %s (permissions 0600)", path)
    except OSError as exc:
        raise ConfigError(f"Cannot create default config at {path}: {exc}") from exc


def _parse_config(data: dict) -> RelayConfig:
    """Parse raw TOML dict into a RelayConfig instance.

    Args:
        data: Parsed TOML data (from tomllib.loads).

    Returns:
        Populated RelayConfig.

    Raises:
        ConfigError: If validation fails.
    """
    # Top-level fields
    log_level = str(data.get("log_level", "INFO")).upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(
            f"Invalid log_level={log_level!r}, must be one of: "
            f"{', '.join(sorted(_VALID_LOG_LEVELS))}"
        )

    proxy_st_profile = str(data.get("proxy_st_profile", "browse"))
    if not proxy_st_profile:
        raise ConfigError("proxy_st_profile must not be empty")

    # [server]
    server_data = data.get("server", {})
    host = str(server_data.get("host", "127.0.0.1"))
    port = server_data.get("port", 8080)
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ConfigError(f"server.port must be an integer 1-65535, got: {port!r}")
    server = ServerConfig(host=host, port=port)

    # [monitor]
    monitor_data = data.get("monitor", {})
    monitor_enabled = monitor_data.get("enabled", True)
    if not isinstance(monitor_enabled, bool):
        raise ConfigError(
            f"monitor.enabled must be a boolean, got: {type(monitor_enabled).__name__}"
        )
    slow_threshold = monitor_data.get("slow_threshold_ms", 2000.0)
    if not isinstance(slow_threshold, (int, float)) or slow_threshold <= 0:
        raise ConfigError(
            f"monitor.slow_threshold_ms must be a positive number, got: {slow_threshold!r}"
        )
    error_count = monitor_data.get("error_threshold_count", 5)
    if not isinstance(error_count, int) or error_count < 0:
        raise ConfigError(
            f"monitor.error_threshold_count must be a non-negative integer, got: {error_count!r}"
        )
    window_size = monitor_data.get("window_size", 100)
    if not isinstance(window_size, int) or window_size < 1:
        raise ConfigError(
            f"monitor.window_size must be a positive integer, got: {window_size!r}"
        )
    monitor = MonitorConfig(
        enabled=monitor_enabled,
        slow_threshold_ms=float(slow_threshold),
        error_threshold_count=error_count,
        window_size=window_size,
    )

    # [anti_leak]
    anti_leak_data = data.get("anti_leak", {})
    warn_tz = anti_leak_data.get("warn_timezone_mismatch", True)
    if not isinstance(warn_tz, bool):
        raise ConfigError(
            f"anti_leak.warn_timezone_mismatch must be a boolean, got: {type(warn_tz).__name__}"
        )
    anti_leak = AntiLeakConfig(warn_timezone_mismatch=warn_tz)

    # [browse]
    browse_data = data.get("browse", {})
    rotate_interval = browse_data.get("rotate_interval_min", 30)
    if not isinstance(rotate_interval, int) or rotate_interval < 0:
        raise ConfigError(
            f"browse.rotate_interval_min must be a non-negative integer, got: {rotate_interval!r}"
        )
    browse_cfg = BrowseConfig(rotate_interval_min=rotate_interval)

    config = RelayConfig(
        log_level=log_level,
        proxy_st_profile=proxy_st_profile,
        server=server,
        monitor=monitor,
        anti_leak=anti_leak,
        browse=browse_cfg,
    )

    log.debug(
        "Config loaded: profile=%s, bind=%s:%d",
        proxy_st_profile,
        host,
        port,
    )

    return config
