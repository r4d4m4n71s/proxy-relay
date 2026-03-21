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
# =============================================================================
# proxy-relay configuration
# =============================================================================
# Local HTTP CONNECT proxy that forwards traffic through upstream SOCKS5
# proxies provided by proxy-st.
#
# Usage:
#   proxy-relay start --profile <name>     Start the proxy server
#   proxy-relay browse --profile <name>    Launch a browser through the proxy
#   proxy-relay status                     Show all running instances
#   proxy-relay stop --profile <name>      Stop a running instance
#   proxy-relay block --profile <name> --domains <d1,d2,...>    Block domains
#   proxy-relay unblock --profile <name> --domains <d1,d2,...>  Unblock domains
#
# --profile is REQUIRED on all commands except status.
# Profile names must match proxy-st profile names (same identity = same name).
# =============================================================================

# Global log level. Applies to all commands.
# Values: DEBUG, INFO, WARNING, ERROR
log_level = "INFO"

# ---------------------------------------------------------------------------
# [server] — Local proxy server bind settings
# ---------------------------------------------------------------------------
# The proxy listens on this address. All profiles share the same bind host.
# Each profile gets its own port (configured in [profiles.<name>]).
#
# WARNING: binding to a non-loopback address exposes the proxy to the network.
[server]
host = "127.0.0.1"                         # Bind address (loopback only)

# ---------------------------------------------------------------------------
# [monitor] — Connection quality monitoring
# ---------------------------------------------------------------------------
# Tracks tunnel success/failure rates in a rolling window. When errors exceed
# the threshold, auto-rotates to a new upstream exit IP.
[monitor]
enabled = true                              # Enable/disable the monitor
slow_threshold_ms = 2000.0                  # Log warning when tunnel > this (ms)
error_threshold_count = 5                   # Errors in window before auto-rotate
window_size = 100                           # Rolling window size (connections)

# ---------------------------------------------------------------------------
# [anti_leak] — IP and identity leak prevention
# ---------------------------------------------------------------------------
[anti_leak]
warn_timezone_mismatch = true               # Warn if system TZ != proxy country

# ---------------------------------------------------------------------------
# [capture] — Traffic capture for debugging (optional)
# ---------------------------------------------------------------------------
# Requires: pip install proxy-relay[capture]
# Captures HTTP traffic metadata for analysis. Not per-profile — applies globally.
#
# [capture]
# auto_analyze = true                       # Auto-analyze captured traffic
# auto_report = true                        # Auto-generate traffic report
# domains = ["tidal.com", "qobuz.com"]      # Domains to capture
# max_body_bytes = 65536                    # Max request/response body stored
# cookie_poll_interval_s = 30.0            # Seconds between cookie snapshots
# storage_poll_interval_s = 60.0           # Seconds between localStorage polls
# report_dir = "~/.config/proxy-relay/telemetry/reports"  # Directory for capture reports
# min_rotate_kb = 256                      # Skip rotation if DB < this size (KiB)
# max_db_age_days = 7                      # Purge rotated DBs older than N days
# max_db_size_mb = 500                     # Purge rotated DBs larger than N MiB
# max_db_count = 20                        # Keep at most N rotated DBs per profile
# max_report_count = 20                    # Keep at most N report files
# max_report_age_days = 30                 # Purge report files older than N days

# =============================================================================
# [profiles] — Per-profile settings
# =============================================================================
# Each profile maps 1:1 to a proxy-st profile (same name = same proxy identity).
#
# [profiles.default] is REQUIRED — it serves as the inheritance base.
# Named profiles inherit ALL settings from default, then override specific ones.
#
# Inheritance rule:
#   For each field in [profiles.<name>]:
#     - If the field is PRESENT → use it (overrides default)
#     - If the field is ABSENT  → inherit from [profiles.default]
#
# Example: [profiles.miami] with only start_url set inherits port, browser,
# rotate_interval_min, and blocked_domains from [profiles.default].
# =============================================================================

[profiles.default]

# Port to bind the proxy server on. Each profile should use a unique port
# to allow multiple instances to run simultaneously.
# Use port = 0 to let the OS assign a free port automatically.
port = 8080

# Chromium-based browser binary name or absolute path.
# Used by the 'browse' command to launch a browser through the proxy.
# Examples: "chromium", "brave-browser", "/usr/bin/google-chrome-stable"
# Empty string = auto-detect (searches PATH for known Chromium browsers).
browser = ""

# Auto-rotate the upstream exit IP every N minutes during browse sessions.
# Helps avoid long-lived sessions that might trigger detection.
# Set to 0 to disable auto-rotation.
rotate_interval_min = 30

# URL to open automatically when 'browse' launches the browser.
# If the URL is a TIDAL domain (tidal.com, listen.tidal.com, login.tidal.com),
# TIDAL domains are automatically REMOVED from blocked_domains for that session,
# and profile validation + DataDome warmup are triggered if needed.
# Empty string = open the browser's default new-tab page.
start_url = ""

# Domains to block at the proxy level. Any CONNECT or HTTP request to these
# domains (or their subdomains) is rejected with 403 Forbidden.
#
# Purpose: prevents accidental navigation to sensitive domains (e.g., TIDAL)
# without proper session setup (DataDome cookie warmup, etc.).
#
# Default includes TIDAL domains to prevent IP poisoning.
# Set to [] (empty list) to disable all blocking for this profile.
#
# Subdomain matching: "tidal.com" also blocks "login.tidal.com",
# "listen.tidal.com", and any other *.tidal.com subdomain.
blocked_domains = ["tidal.com", "listen.tidal.com", "login.tidal.com"]

# ---------------------------------------------------------------------------
# Named profiles — override specific fields, inherit the rest from default
# ---------------------------------------------------------------------------

# [profiles.miami]
# port = 8081                               # Unique port for this profile
# start_url = "https://example.com"         # Auto-navigate on browse

# [profiles.medellin]
# port = 8082
# browser = "brave-browser"                 # Use Brave for this profile
# rotate_interval_min = 15                  # Rotate more frequently
# blocked_domains = []                      # No blocking (TIDAL access allowed)
# start_url = "https://listen.tidal.com"    # Auto-navigate to TIDAL
"""


@dataclass(frozen=True)
class ProfileConfig:
    """Per-profile configuration settings.

    All fields are inheritable from [profiles.default].

    Attributes:
        port: Bind port for this profile's server instance.
        browser: Chromium-based browser binary name or path (empty = auto-detect).
        rotate_interval_min: IP rotation interval in minutes (0 = disabled).
        start_url: URL to open on browse launch (empty = new-tab page).
            If a TIDAL URL, TIDAL domains are auto-unblocked and warmup triggered.
        blocked_domains: Domains to block at the proxy level.
            Dataclass default (None) resolves to TIDAL_DOMAINS.
            When parsed from TOML, the default template provides an explicit list.
            Empty list = explicitly no blocking.
    """

    port: int = 8080
    browser: str = ""
    rotate_interval_min: int = 30
    start_url: str = ""
    blocked_domains: list[str] | None = None


@dataclass(frozen=True)
class ServerConfig:
    """Local proxy server bind settings. Port moved to ProfileConfig."""

    host: str = "127.0.0.1"


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


@dataclass
class RelayConfig:
    """Root configuration for proxy-relay."""

    log_level: str = "INFO"
    server: ServerConfig = field(default_factory=ServerConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    anti_leak: AntiLeakConfig = field(default_factory=AntiLeakConfig)
    capture: object | None = None  # CaptureConfig | None — typed as object to avoid import
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)

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


def _parse_profile(
    data: dict,
    name: str,
    parent: ProfileConfig | None = None,
) -> ProfileConfig:
    """Parse a [profiles.<name>] section with inheritance.

    For each field:
      - If present in data → use it
      - If absent and parent exists → inherit from parent
      - If absent and no parent (default profile) → use dataclass default

    Args:
        data: The raw TOML dict for the profile section.
        name: Profile name (for error messages).
        parent: Parent ProfileConfig to inherit from. None for default profile.

    Returns:
        Populated ProfileConfig.

    Raises:
        ConfigError: If a field has an invalid value.
    """
    defaults = parent if parent is not None else ProfileConfig()

    # port
    if "port" in data:
        port = data["port"]
        if not isinstance(port, int) or port < 0 or port > 65535:
            raise ConfigError(
                f"profiles.{name}.port must be an integer 0-65535, got: {port!r}"
            )
    else:
        port = defaults.port

    # browser
    if "browser" in data:
        browser = str(data["browser"])
    else:
        browser = defaults.browser

    # rotate_interval_min
    if "rotate_interval_min" in data:
        rotate_interval_min = data["rotate_interval_min"]
        if not isinstance(rotate_interval_min, int) or rotate_interval_min < 0:
            raise ConfigError(
                f"profiles.{name}.rotate_interval_min must be a non-negative integer, "
                f"got: {rotate_interval_min!r}"
            )
    else:
        rotate_interval_min = defaults.rotate_interval_min

    # start_url
    if "start_url" in data:
        start_url = str(data["start_url"])
    else:
        start_url = defaults.start_url

    # blocked_domains — presence vs absence matters for inheritance
    if "blocked_domains" in data:
        raw_domains = data["blocked_domains"]
        if not isinstance(raw_domains, list):
            raise ConfigError(
                f"profiles.{name}.blocked_domains must be a list, got: {type(raw_domains).__name__}"
            )
        # Filter empty strings
        blocked_domains: list[str] | None = [
            str(d) for d in raw_domains if str(d).strip()
        ]
    else:
        # Absent → inherit from parent
        blocked_domains = defaults.blocked_domains

    return ProfileConfig(
        port=port,
        browser=browser,
        rotate_interval_min=rotate_interval_min,
        start_url=start_url,
        blocked_domains=blocked_domains,
    )


def resolve_blocked_domains(
    profile: ProfileConfig,
    start_url: str = "",
) -> frozenset[str] | None:
    """Resolve effective blocked domains for a profile.

    Args:
        profile: Resolved ProfileConfig (inheritance already applied).
        start_url: Effective start URL (from CLI or profile). When this is
            a TIDAL URL, TIDAL domains are removed from the blocked set.

    Returns:
        frozenset of domains to block, or None for no blocking.
        Default: TIDAL_DOMAINS if profile.blocked_domains is None.
    """
    from proxy_relay.profile_rules import TIDAL_DOMAINS, is_tidal_url

    # Determine base set
    if profile.blocked_domains is None:
        # Dataclass default — resolve to TIDAL_DOMAINS
        base: frozenset[str] = TIDAL_DOMAINS
    elif not profile.blocked_domains:
        # Explicitly empty list — no blocking
        return None
    else:
        base = frozenset(profile.blocked_domains)

    # If start_url targets TIDAL, auto-unblock TIDAL domains in-memory
    if is_tidal_url(start_url):
        effective = base - TIDAL_DOMAINS
        if effective != base:
            log.debug(
                "TIDAL start_url detected — auto-unblocking TIDAL domains for this session"
            )
        return effective if effective else None

    return base if base else None


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

    # [server]
    server_data = data.get("server", {})
    host = str(server_data.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "::1", "localhost"):
        log.warning(
            "server.host=%r binds to a non-loopback address — "
            "the proxy will be accessible from the network",
            host,
        )
    server = ServerConfig(host=host)

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

    # [capture] — optional; lazy import avoids circular dependency
    capture_data = data.get("capture")
    capture_cfg = None
    if capture_data is not None:
        from pathlib import Path as _Path

        from proxy_relay.capture.models import (
            DEFAULT_CAPTURE_DOMAINS,
            CaptureConfig,
        )

        raw_domains = capture_data.get("domains")
        domains = frozenset(raw_domains) if raw_domains is not None else DEFAULT_CAPTURE_DOMAINS

        raw_db_path = capture_data.get("db_path")
        db_path = _Path(raw_db_path) if raw_db_path is not None else None

        raw_report_dir = capture_data.get("report_dir")
        report_dir = _Path(raw_report_dir) if raw_report_dir is not None else None

        capture_cfg = CaptureConfig(
            db_path=db_path,
            domains=domains,
            max_body_bytes=int(capture_data.get("max_body_bytes", 65_536)),
            cookie_poll_interval_s=float(capture_data.get("cookie_poll_interval_s", 30.0)),
            storage_poll_interval_s=float(capture_data.get("storage_poll_interval_s", 60.0)),
            report_dir=report_dir,
            auto_analyze=bool(capture_data.get("auto_analyze", True)),
            auto_report=bool(capture_data.get("auto_report", False)),
            min_rotate_kb=int(capture_data.get("min_rotate_kb", 256)),
            max_db_age_days=int(capture_data.get("max_db_age_days", 7)),
            max_db_size_mb=int(capture_data.get("max_db_size_mb", 500)),
            max_db_count=int(capture_data.get("max_db_count", 20)),
            max_report_count=int(capture_data.get("max_report_count", 20)),
            max_report_age_days=int(capture_data.get("max_report_age_days", 30)),
        )

    # [profiles] — require [profiles.default]
    profiles_data = data.get("profiles", {})
    if not isinstance(profiles_data, dict):
        raise ConfigError("[profiles] must be a TOML table")

    if "default" not in profiles_data:
        raise ConfigError(
            "Missing [profiles.default] section — "
            "regenerate config with: proxy-relay start --profile default"
        )

    # Parse default profile first (no parent)
    default_profile = _parse_profile(profiles_data["default"], "default", parent=None)
    profiles: dict[str, ProfileConfig] = {"default": default_profile}

    # Parse named profiles with default as parent
    for profile_name, profile_data in profiles_data.items():
        if profile_name == "default":
            continue
        if not isinstance(profile_data, dict):
            raise ConfigError(f"[profiles.{profile_name}] must be a TOML table")
        profiles[profile_name] = _parse_profile(profile_data, profile_name, parent=default_profile)

    log.debug(
        "Config loaded: host=%s, profiles=%s",
        host,
        list(profiles.keys()),
    )

    return RelayConfig(
        log_level=log_level,
        server=server,
        monitor=monitor,
        anti_leak=anti_leak,
        capture=capture_cfg,
        profiles=profiles,
    )
