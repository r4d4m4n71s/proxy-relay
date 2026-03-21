# Plan: Per-Profile Configuration with Domain Blocking

## Goal

Refactor proxy-relay's configuration to support per-profile settings with inheritance from a `[profiles.default]` base. Domain blocking (TIDAL by default) prevents accidental IP poisoning. Runtime `block`/`unblock` commands modify config.toml directly (via tomlkit) and signal the server to reload via SIGUSR2. The `--profile` flag becomes mandatory on all commands (except `status`) to force explicit proxy identity awareness.

---

## 1. Config.toml Target Schema

### Default config template (generated on first run)

```toml
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
# report_dir = "~/.config/proxy-relay"     # Directory for capture reports

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
```

### Field reference

| Field | Type | Default | Scope | Description |
|-------|------|---------|-------|-------------|
| `log_level` | string | `"INFO"` | Global | Log verbosity: DEBUG, INFO, WARNING, ERROR |
| `server.host` | string | `"127.0.0.1"` | Global | Bind address for all proxy instances |
| `monitor.enabled` | bool | `true` | Global | Enable connection quality monitoring |
| `monitor.slow_threshold_ms` | float | `2000.0` | Global | Warn when tunnel establishment exceeds this (ms) |
| `monitor.error_threshold_count` | int | `5` | Global | Errors in rolling window before auto-rotate |
| `monitor.window_size` | int | `100` | Global | Rolling window size (connections) |
| `anti_leak.warn_timezone_mismatch` | bool | `true` | Global | Warn if system timezone mismatches proxy country |
| `profiles.<name>.port` | int | `8080` | Profile | Bind port (unique per profile for concurrent instances) |
| `profiles.<name>.browser` | string | `""` | Profile | Browser binary name/path (empty = auto-detect) |
| `profiles.<name>.rotate_interval_min` | int | `30` | Profile | IP rotation cadence in minutes (0 = disabled) |
| `profiles.<name>.start_url` | string | `""` | Profile | URL to open on `browse`. TIDAL URLs trigger auto-unblock + warmup. |
| `profiles.<name>.blocked_domains` | list[str] | TIDAL domains | Profile | Domains to block with 403 (empty list = no blocking) |

---

## 2. Profile Inheritance

### Rules

1. `[profiles.default]` is **required** — error if missing
2. Named profiles inherit ALL fields from `[profiles.default]`
3. Any field explicitly set in a named profile **overrides** the inherited value
4. A field set to its "empty" value (e.g., `blocked_domains = []`) is an explicit override, NOT inheritance

### Inheritance resolution

```python
def _parse_profile(data: dict, name: str, parent: ProfileConfig | None = None) -> ProfileConfig:
    """Parse a profile section.

    For each field:
      - If present in data → use it
      - If absent and parent exists → inherit from parent
      - If absent and no parent (default profile) → use dataclass default
    """
```

### Example

```toml
[profiles.default]
port = 8080
browser = "chromium"
blocked_domains = ["tidal.com", "listen.tidal.com", "login.tidal.com"]

[profiles.miami]
port = 8081
# browser → inherited: "chromium"
# blocked_domains → inherited: ["tidal.com", ...]
# start_url → inherited: ""

[profiles.medellin]
port = 8082
browser = "brave-browser"        # override
blocked_domains = []             # override: no blocking
start_url = "https://listen.tidal.com"  # override
```

Effective values for `medellin`:
- `port = 8082` (overridden)
- `browser = "brave-browser"` (overridden)
- `rotate_interval_min = 30` (inherited from default)
- `blocked_domains = []` (overridden — no blocking)
- `start_url = "https://listen.tidal.com"` (overridden)

---

## 3. Mandatory --profile

### What changes

- **Remove** `default_proxy_profile` from `RelayConfig`, `_parse_config()`, `_DEFAULT_CONFIG`
- **Remove** `[browse]` section entirely — its fields (`browser`, `rotate_interval_min`) move to profiles
- **Remove** `BrowseConfig` dataclass
- **No backward compat**: old keys (`default_proxy_profile`, `proxy_st_profile`, `[browse]`) are removed, not silently ignored. Old config.toml must be updated.
- **`--profile` required** on: `start`, `stop`, `rotate`, `browse`, `block`, `unblock`
- **`--host` and `--port` CLI flags on `start`**: **kept** as overrides. Precedence: CLI flag > profile config > default profile.
- **`status`**: shows all profiles by default. `--profile` is optional filter only. `--all` flag removed.

### CLI help text

| Command | `--profile` | Help text |
|---------|-------------|-----------|
| `start` | Required | `proxy-st profile name (required)` |
| `stop` | Required | `proxy-st profile name (required)` |
| `rotate` | Required | `proxy-st profile name (required)` |
| `browse` | Required | `proxy-st profile name (required)` |
| `block` | Required | `proxy-st profile name (required)` |
| `unblock` | Required | `proxy-st profile name (required)` |
| `status` | Optional | `proxy-st profile name (optional filter)` |

---

## 4. Data Flows

### 4.1 Start flow

```
proxy-relay start --profile miami
  1. Load config.toml → RelayConfig (with profiles dict, inheritance resolved)
  2. Resolve profile: config.profiles["miami"] → ProfileConfig
  3. Port precedence: --port CLI flag > profile.port > default.port
  4. resolve_blocked_domains(profile) → frozenset or None
  5. ProxyServer(port=effective_port, blocked_domains=effective_blocked)
  6. server.start() installs SIGUSR2 handler for runtime config reload
  7. Handler checks blocked_domains on every CONNECT/HTTP request
```

### 4.2 Browse flow

```
proxy-relay browse --profile medellin
  1. Load config → profiles["medellin"] (with inheritance)
  2. Effective start_url = --start-url CLI flag or profile.start_url
  3. Effective browser = --browser CLI flag or profile.browser or auto-detect
  4. Effective rotate = --rotate-min CLI flag or profile.rotate_interval_min
  5. If effective start_url is TIDAL URL:
     a. TIDAL domains auto-unblocked for this session (in-memory)
     b. Profile validation runs (DataDome cookie check, poisoned check, etc.)
     c. If validation fails → execute remediations → run warmup if needed
  6. If no TIDAL start_url → blocked_domains from config used as-is → TIDAL blocked
  7. Auto-start server or reuse existing
  8. Launch browser at effective start_url
```

### 4.3 Warmup trigger (unchanged logic, new config source)

```
Warmup triggers when ALL of:
  1. Effective start_url is a TIDAL URL (profile.start_url or --start-url)
  2. Profile validation runs and rules FAIL (datadome_cookie_exists, etc.)
  3. At least one failed rule has a non-NONE remediation action

Flow:
  validation fails → "Press Enter to apply" → execute remediations →
  if needs_warmup → launch warmup browser (listen.tidal.com, mouse/scroll) →
  DataDome cookie acquired → main browser launches
```

### 4.4 Runtime block/unblock flow

```
proxy-relay unblock --profile miami --domains tidal.com,listen.tidal.com
  1. Load config.toml with tomlkit (preserves comments/formatting)
  2. Read current [profiles.miami].blocked_domains (or inherited from default)
  3. Compute: current - specified_domains
  4. Write updated blocked_domains to [profiles.miami] in config.toml
     (creates [profiles.miami] section if it doesn't exist)
  5. Read PID from miami.pid → send SIGUSR2
  6. Server re-reads config.toml → updates in-memory blocked_domains
  7. Print confirmation

proxy-relay block --profile miami --domains example.com
  (same but computes: current | specified_domains)
```

### 4.5 SIGUSR2 — Server config reload

```
Server receives SIGUSR2
  1. Re-read config.toml → RelayConfig (with profiles + inheritance)
  2. Resolve profile's blocked_domains
  3. Atomically swap self._blocked_domains
  4. Log the change
  5. If config.toml has parse error → log error, keep current in-memory state
```

No override files. config.toml is the single source of truth.

### 4.6 Status flow

```
proxy-relay status
  → Shows ALL running profiles (scans *.status.json)

proxy-relay status --profile miami
  → Shows only miami
```

### 4.7 tidal-dl login flow

```
tidal-dl login (with proxy configured)
  1. tidal_dl/cli.py resolves proxy_st_profile from provider config
  2. Calls open_login_browser(auth_url, proxy_info, proxy_st_profile="miami")
  3. auth/browser.py calls auto_start_server("miami", start_url=auth_url)
  4. auto_start_server() launches:
       proxy-relay start --profile miami --port 0 --start-url <auth_url>
     (--port 0 = OS-assigned ephemeral port; --start-url = TIDAL unblock trigger)
  5. Server reads config + applies --start-url → TIDAL unblocked in-memory
  6. On cleanup_browser(), server is stopped
```

---

## 5. Architect Review Findings (all resolved)

### Issue 1: tomlkit is NOT a dependency (BLOCKER → resolved)

`tomlkit` was removed from `pyproject.toml` in F02. Must be **re-added** as runtime dependency for `block`/`unblock` commands.

**Action:** Add `tomlkit` to `dependencies` in `pyproject.toml`.

### Issue 2: Ephemeral port for auto_start_server (BLOCKER → resolved)

`auto_start_server()` uses `--port 0` for OS-assigned ephemeral ports. Must keep this.

**Decision:** `--host` and `--port` CLI flags remain on `start` as overrides. Precedence: CLI > profile > default. `auto_start_server()` always passes `--port 0`.

### Issue 3: TIDAL unblock for auto-started servers (HIGH → resolved)

With `--block-domains` removed, the subprocess needs a way to know it should unblock TIDAL.

**Decision:** Keep `--start-url` as a **hidden** flag on `start` (not shown in `--help`). `auto_start_server()` passes `--start-url <url>` when launching for browse/login. The server uses `resolve_blocked_domains(profile, start_url)` which removes TIDAL domains if start_url is a TIDAL URL. This is cleaner than relying solely on profile config because the same profile may be used for both TIDAL and non-TIDAL sessions.

### Issue 4: run_server() convenience function (MEDIUM → noted)

`server.py` has a public `run_server()`. Remains unchanged — internal/testing convenience.

### Issue 5: config.server.port fallback in _cmd_browse (MEDIUM → resolved)

After refactor, `config.server.port` fallbacks become `profile.port`.

### Issue 6: --block-domains removal ordering (MEDIUM → resolved)

Removing `--block-domains` from parser and from `auto_start_server()` **must be atomic** (same commit).

### Issue 7: Old proxy_st_profile key in user config (LOW → resolved)

No backward compat — old config must be regenerated. Old keys cause parse errors.

### Issue 8: Test rewrite scope for --all removal (LOW → noted)

~6 `--all` tests in `test_cli.py` need full rewrite. Task 13 effort adjusted to L.

---

## 6. Code Contracts

### 6.1 ProfileConfig (config.py)

```python
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
```

### 6.2 RelayConfig changes (config.py)

```python
@dataclass
class RelayConfig:
    log_level: str = "INFO"
    # REMOVED: default_proxy_profile
    # REMOVED: browse (BrowseConfig) — fields moved to ProfileConfig
    server: ServerConfig = field(default_factory=ServerConfig)  # host only
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    anti_leak: AntiLeakConfig = field(default_factory=AntiLeakConfig)
    capture: object | None = None
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)  # NEW
```

### 6.3 ServerConfig changes (config.py)

```python
@dataclass(frozen=True)
class ServerConfig:
    """Local proxy server bind settings. Port moved to ProfileConfig."""
    host: str = "127.0.0.1"
```

### 6.4 resolve_blocked_domains() (config.py)

```python
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
```

### 6.5 _parse_profile() (config.py)

```python
def _parse_profile(
    data: dict,
    name: str,
    parent: ProfileConfig | None = None,
) -> ProfileConfig:
    """Parse a [profiles.<name>] section with inheritance."""
```

### 6.6 ProxyServer changes (server.py)

```python
class ProxyServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        upstream_manager: UpstreamManager | None = None,
        monitor_config: MonitorConfig | None = None,
        profile_name: str = "browse",
        blocked_domains: frozenset[str] | None = None,
        config_path: Path | None = None,  # NEW: for SIGUSR2 config reload
    ) -> None:
        self._blocked_domains = blocked_domains
        self._config_path = config_path

    def _update_blocked_domains(self, new_domains: frozenset[str] | None) -> None:
        """Atomically replace blocked domains."""

    def _signal_block_update(self) -> None:
        """SIGUSR2 handler — re-reads config.toml and updates blocked_domains."""

    def _reload_blocked_from_config(self) -> None:
        """Re-read config.toml, resolve profile's blocked_domains, swap in-memory.
        On parse error, logs warning and keeps current state."""
```

### 6.7 CLI — block/unblock subcommands (cli.py)

```python
def _cmd_block(args: argparse.Namespace) -> int:
    """Add domains to profile's block list in config.toml + signal server."""

def _cmd_unblock(args: argparse.Namespace) -> int:
    """Remove domains from profile's block list in config.toml + signal server.
    Creates [profiles.<name>] section if it doesn't exist."""
```

### 6.8 auto_start_server() (browse.py)

```python
def auto_start_server(
    profile_name: str,
    host: str = "127.0.0.1",
    config_path: Path | None = None,
    log_level: str = "INFO",
    start_url: str = "",  # NEW: passed as --start-url to subprocess
    # REMOVED: blocked_domains parameter
    # Port: always --port 0 (OS-assigned) for ephemeral servers
) -> subprocess.Popen[bytes]:
```

### 6.9 Hidden --start-url on `start` command (cli.py)

```python
# Internal flag, not shown in --help.
# Used by auto_start_server() to tell the server subprocess
# which URL the browse session targets, so it can auto-unblock TIDAL.
start_parser.add_argument("--start-url", type=str, default="", help=argparse.SUPPRESS)
```

---

## 7. Files Touched

### proxy-relay (production)

| # | File | Changes |
|---|------|---------|
| 1 | `config.py` | Add `ProfileConfig`. Add `_parse_profile()` with inheritance. Add `profiles` to `RelayConfig`. Remove `default_proxy_profile`. Remove `BrowseConfig`. Move `port` from `ServerConfig` to `ProfileConfig`. Add `resolve_blocked_domains(profile, start_url)`. Update `_parse_config()` and `_DEFAULT_CONFIG`. |
| 2 | `server.py` | Add `config_path` param. Add `_update_blocked_domains()`. Add `_signal_block_update()` SIGUSR2. Add `_reload_blocked_from_config()`. Install SIGUSR2 in `start()`. |
| 3 | `cli.py` | Remove `--block-domains` from `start`. Add hidden `--start-url` to `start`. Make `--profile` required on start/stop/rotate/browse. Remove `--all` from status. Add `block`/`unblock` subcommands. Remove profile fallbacks. Update `_cmd_start()` (port/blocked from profile, CLI overrides). Update `_cmd_browse()` (browser/start_url/rotate from profile). Update `_cmd_status()` (show all by default). Remove `BrowseConfig` refs. Fix `config.server.port` fallbacks → `profile.port`. |
| 4 | `handler.py` | No changes — already has `blocked_domains` + `_is_domain_blocked()`. |
| 5 | `browse.py` | Remove `blocked_domains` param from `auto_start_server()`. Keep `--port 0`. Add `--start-url`. Remove `--block-domains`. **ATOMIC with cli.py --block-domains removal.** |
| 6 | `profile_rules.py` | No changes. |
| 7 | `pyproject.toml` | Add `tomlkit` to `dependencies`. |

### proxy-relay (tests)

| # | File | Changes |
|---|------|---------|
| 8 | `test_config.py` | ProfileConfig parsing. Inheritance tests. `resolve_blocked_domains()` (incl. TIDAL auto-unblock via start_url). Remove `default_proxy_profile`/`BrowseConfig` assertions. |
| 9 | `test_cli.py` | Fix profile=None → real name. Remove `--block-domains`. Add `block`/`unblock` tests. Rewrite ~6 `--all` tests. Update parsers with `--profile`. |
| 10 | `test_browse.py` | Add `--profile`. Update `auto_start_server` (remove blocked_domains, add start_url). |
| 11 | `test_cli_capture.py` | Add `--profile` to parser tests. |
| 12 | `test_server.py` | SIGUSR2 handler. `_reload_blocked_from_config()` (success + parse error). |
| 13 | `test_handler.py` | No changes. |

### tidal-dl (cross-project sync)

| # | File | Changes |
|---|------|---------|
| 14 | `tidal_dl/auth/browser.py` | Update `auto_start_server()` call: remove `blocked_domains`, pass `start_url=auth_url`. |

---

## 8. Implementation Tasks

| # | Task | Files | Effort | Depends | Notes |
|---|------|-------|--------|---------|-------|
| 1 | ProfileConfig + inheritance + parsing + resolve_blocked_domains() | config.py | L | — | Core change |
| 2 | Remove default_proxy_profile, BrowseConfig, port from ServerConfig | config.py | S | 1 | |
| 3 | Add tomlkit to dependencies | pyproject.toml | XS | — | New dependency |
| 4 | Make --profile required, add block/unblock subparsers, update status | cli.py (parser) | M | — | |
| 5 | Update _cmd_start() (port/blocked from profile, --start-url, CLI overrides) | cli.py | M | 1, 4 | |
| 6 | Update _cmd_browse() (browser/start_url/rotate from profile, warmup flow) | cli.py | M | 1, 5 | start_url triggers warmup |
| 7 | Implement _cmd_block() and _cmd_unblock() (tomlkit edit + SIGUSR2) | cli.py | M | 1, 3 | |
| 8 | Update _cmd_status() — show all by default, optional --profile filter | cli.py | S | 4 | |
| 9 | SIGUSR2 handler + _reload_blocked_from_config() | server.py | M | 1 | Handle parse errors |
| 10 | Update auto_start_server(): remove blocked_domains, add start_url, keep --port 0 | browse.py | S | — | **ATOMIC** with task 4 |
| 11 | Update tidal-dl auth/browser.py | tidal_dl/auth/browser.py | S | 10 | Pass auth_url |
| 12 | Config tests (ProfileConfig, inheritance, resolve) | test_config.py | M | 1, 2 | |
| 13 | CLI tests (mandatory profile, block/unblock, status rewrite) | test_cli.py | L | 4-8 | ~6 --all tests full rewrite |
| 14 | Server tests (SIGUSR2, config reload, parse error) | test_server.py | M | 9 | |
| 15 | Browse + capture tests (add --profile, update auto_start_server) | test_browse.py, test_cli_capture.py | S | 4, 10 | |

---

## 9. Migration

No backward compatibility. Old config.toml is **replaced entirely** with the new format on first run after upgrade.

| Scenario | Behavior |
|----------|----------|
| Old config with `default_proxy_profile`, `[browse]`, etc. | **Error** — must regenerate config |
| Config without `[profiles.default]` | **Error**: "Missing [profiles.default] section" |
| `proxy-relay start` without `--profile` | argparse error: `--profile is required` |
| `proxy-relay status` without args | Shows all profiles |
| `--block-domains` on `start` | argparse error — flag removed |

The user regenerates config.toml from the new `_DEFAULT_CONFIG` template and customizes it.

---

## 10. Edge Cases

| Edge case | Handling |
|-----------|---------|
| Empty string in blocked_domains list | Filtered out during parsing |
| `blocked_domains = []` vs absent key | `[]` = no blocking. Absent = inherit from parent. |
| Profile not in config but used with --profile | Uses [profiles.default] values (logs warning) |
| SIGUSR2 when config.toml has parse error | Log error, keep current in-memory state |
| block/unblock on profile without [profiles.X] | Creates the section via tomlkit |
| start_url is TIDAL but blocked_domains includes TIDAL | Auto-unblock TIDAL (in-memory, no config edit) |
| Two profiles with same port | Second hits EADDRINUSE → auto-fallback |
| IPv6 brackets in CONNECT target | `_is_domain_blocked()` strips brackets |
| Case sensitivity in domains | `_is_domain_blocked()` lowercases |
| Partial suffix (nottidal.com vs tidal.com) | Requires exact match or `.` prefix |
| `run_server()` in server.py | Unchanged — internal/testing convenience |
| Ephemeral servers (browse, login) | Always `--port 0`, ignoring profile port |
| `--start-url` on start (hidden flag) | Used by auto_start_server() only |

---

## 11. Thread Safety for SIGUSR2 Reload

- `self._blocked_domains` is `frozenset[str] | None` — immutable object
- Assignment is atomic in CPython (GIL protects single-bytecode stores)
- SIGUSR2 handler runs in event loop thread (via `loop.add_signal_handler`)
- Handler reads config.toml and swaps the frozenset
- Existing connections keep their snapshot; new connections get updated set
- On parse error: logs warning, keeps current state — no crash
- No locks needed
