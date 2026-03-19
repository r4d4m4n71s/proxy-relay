# proxy-relay Configuration Reference

Complete reference for every configuration parameter, CLI flag, and internal concept in proxy-relay.

---

## Table of Contents

- [Config File](#config-file)
- [Top-Level Parameters](#top-level-parameters)
- [[server] Section](#server-section)
- [[monitor] Section](#monitor-section)
- [[anti_leak] Section](#anti_leak-section)
- [[browse] Section](#browse-section)
- [[capture] Section](#capture-section)
- [CLI Commands](#cli-commands)
- [Chromium Flags (browse command)](#chromium-flags-browse-command)
- [Concepts](#concepts)
- [Python Library API](#python-library-api)

---

## Config File

**Location:** `~/.config/proxy-relay/config.toml`

Created automatically on first run with commented defaults. Format: [TOML](https://toml.io/). Permissions are set to `0600` (owner-only read/write).

Override with `--config /path/to/file.toml` on `start` or `browse` commands.

---

## Top-Level Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `log_level` | string | `"INFO"` | Logging verbosity. Controls what gets printed to the console and log output. |
| `default_proxy_profile` | string | `"browse"` | Name of the proxy-st profile to use for upstream SOCKS5 connections. Must match a profile defined in proxy-st's config. |

### `log_level`

Controls the minimum severity of log messages printed to the console.

| Value | What you see |
|-------|-------------|
| `"DEBUG"` | Everything: connection details, URL building, header stripping, tunnel establishment. Very verbose. |
| `"INFO"` | Normal operation: startup, upstream resolution, rotations, browser launch. Recommended for daily use. |
| `"WARNING"` | Only problems: slow connections, timezone mismatches, rotation triggers, proxy deaths. |
| `"ERROR"` | Only failures: fatal errors, unrecoverable tunnel failures, process crashes. |

### `default_proxy_profile`

The profile name from your proxy-st configuration (`~/.config/proxy-st/config.toml`). Each profile defines a country, session lifetime, and connection parameters.

```toml
# Use the "us-browse" profile from proxy-st
default_proxy_profile = "us-browse"
```

---

## [server] Section

Controls where proxy-relay listens for incoming connections from your browser or applications.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `host` | string | `"127.0.0.1"` | Any valid IP address | Local bind address. `127.0.0.1` means only local connections. `0.0.0.0` would accept connections from other machines (not recommended). |
| `port` | integer | `8080` | 1–65535 | Local bind port. The port your browser connects to. |

```toml
[server]
host = "127.0.0.1"
port = 8080
```

**How it works:** Your browser sends HTTP CONNECT requests to `host:port`. proxy-relay accepts them, opens a SOCKS5 tunnel through the upstream proxy, and relays traffic bidirectionally.

---

## [monitor] Section

The connection monitor tracks every connection in a rolling window and automatically rotates the upstream proxy when quality degrades.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `enabled` | boolean | `true` | `true` / `false` | Enable or disable the connection quality monitor entirely. |
| `slow_threshold_ms` | float | `2000.0` | > 0 | Maximum acceptable tunnel establishment latency in milliseconds. Connections slower than this trigger a warning in the logs. |
| `error_threshold_count` | integer | `2` | >= 0 | Number of errors in the rolling window before triggering automatic upstream rotation. |
| `window_size` | integer | `100` | >= 1 | Number of recent connections to track in the rolling window. Older records are evicted when the window is full. |

```toml
[monitor]
enabled = true
slow_threshold_ms = 2000.0
error_threshold_count = 2
window_size = 100
```

### `slow_threshold_ms`

**What it measures:** The time (in milliseconds) to establish a SOCKS5 tunnel to the target host. This is the handshake time, not the total page load time.

**What it does:** When a connection takes longer than this threshold, a warning is logged:
```
WARNING — Slow connection to example.com:443: 3500ms (threshold 2000ms)
```

**What it does NOT do:** It does not trigger rotation on its own. It's an observability signal — you see it in the logs and can decide whether your proxy is getting sluggish.

| Value | Use case |
|-------|----------|
| `1000.0` | Aggressive — flag anything over 1 second |
| `2000.0` | Balanced (default) — most proxied connections establish under 2s |
| `5000.0` | Lenient — for very distant proxy exits or slow networks |

### `error_threshold_count`

**What it measures:** The number of failed connections (tunnel errors, timeouts, connection resets) within the current rolling window.

**What it does:** When the error count reaches this threshold, proxy-relay automatically:
1. Sends SIGUSR1 to itself (triggers `UpstreamManager.rotate()`)
2. Gets a new session ID from proxy-st → new exit IP
3. Clears the rolling window (prevents immediate re-triggering)
4. Logs: `WARNING — Error threshold reached: 2 errors in window (threshold=2), triggering rotation`

**How it interacts with `window_size`:** The window holds the last N connections. If you have `window_size = 100` and `error_threshold_count = 2`, then 2 errors out of the last 100 connections will trigger rotation. This is quite sensitive — a good thing for maintaining a clean proxy.

| Value | Behavior |
|-------|----------|
| `0` | Rotate on **every** error (most aggressive) |
| `1` | Rotate after the first error |
| `2` | Rotate after 2 errors in the window (default — recommended) |
| `5` | More tolerant — allows occasional errors before rotating |
| `10` | Very lenient — only rotate if the proxy is clearly broken |

### `window_size`

**What it is:** The maximum number of connection records kept in memory. Works like a sliding window — when full, the oldest record is dropped as a new one arrives.

**How it affects error detection:** A smaller window means errors are "forgotten" sooner (they scroll out of the window). A larger window means errors persist longer, making the threshold more likely to be hit.

| Value | Effect |
|-------|--------|
| `20` | Short memory — only considers the last 20 connections |
| `100` | Default — balanced window |
| `500` | Long memory — errors stick around longer |

### `enabled`

Set to `false` to disable the entire monitor. No connection tracking, no latency warnings, no automatic rotation. Manual rotation via `proxy-relay rotate` still works.

---

## [anti_leak] Section

Anti-fingerprinting protections that prevent your real identity from leaking through the proxy.

| Parameter | Type | Default | Values | Description |
|-----------|------|---------|--------|-------------|
| `warn_timezone_mismatch` | boolean | `true` | `true` / `false` | Check if your system timezone matches the proxy exit country on startup. |

```toml
[anti_leak]
warn_timezone_mismatch = true
```

### `warn_timezone_mismatch`

**The problem:** When you browse through a proxy in Germany but your system clock says `America/Bogota`, websites can detect this mismatch using JavaScript:
```javascript
Intl.DateTimeFormat().resolvedOptions().timeZone
// Returns "America/Bogota" — doesn't match a German IP
```

**What it does on `start`:** Compares your system's UTC offset against the expected range for the proxy's exit country. If they don't match, logs a warning:
```
WARNING — Timezone mismatch detected: local UTC-5.0 is outside expected range
UTC+1.0 to UTC+2.0 for country 'DE'. Websites may detect this via JavaScript timezone APIs.
```

**Proactive fix on `browse`:** The `browse` command goes further — it **automatically sets the `TZ` environment variable** on the Chromium process to match the proxy exit country. This means JavaScript will report a timezone consistent with your proxy IP without you having to change your system timezone.

For example, if your proxy exits in Germany:
- Chromium is launched with `TZ=Europe/Berlin`
- JavaScript `Intl.DateTimeFormat()` reports `Europe/Berlin`
- Your system timezone remains unchanged

**Supported countries:** 40+ countries with IANA timezone mappings. For countries not in the table, the system timezone is used and a warning is logged.

---

## [browse] Section

Controls the `proxy-relay browse` command behavior.

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `rotate_interval_min` | integer | `30` | >= 0 | Minutes between automatic upstream proxy rotations. `0` disables auto-rotation. |
| `browser` | string | `""` | — | Chromium-based browser binary name or path. Empty means auto-detect. |

```toml
[browse]
rotate_interval_min = 30
```

### `rotate_interval_min`

**What it does:** While Chromium is open, a background thread sends SIGUSR1 to the proxy-relay process at this interval, forcing the upstream proxy to rotate to a new exit IP.

**Why rotate while browsing:** Long-lived proxy sessions can be flagged by anti-fraud systems. Periodic rotation makes your browsing pattern look more like a mobile user moving between cell towers.

| Value | Behavior |
|-------|----------|
| `0` | Disabled — keep the same exit IP for the entire session |
| `10` | Aggressive — new IP every 10 minutes |
| `30` | Balanced (default) — new IP every 30 minutes |
| `60` | Conservative — new IP every hour |

**Resolution priority** (first match wins):
1. `--no-rotate` CLI flag → `0` (disabled)
2. `--rotate-min N` CLI flag → `N`
3. `[browse] rotate_interval_min` in config → config value
4. Default → `30`

### `browser`

**What it does:** Specifies which Chromium-based browser to use. When empty (default), proxy-relay searches for browsers in this order: Snap Chromium, Chromium, Chrome, Chrome Stable, Edge, Brave, Vivaldi, Opera.

**Supported browsers:** Any Chromium-based browser that accepts `--proxy-server`, `--user-data-dir`, and WebRTC flags.

| Value | Browser |
|-------|---------|
| `""` | Auto-detect (default) |
| `"chromium"` | Chromium |
| `"chromium-stable"` | Chromium (stable channel) |
| `"google-chrome"` | Google Chrome |
| `"google-chrome-stable"` | Google Chrome (stable channel) |
| `"brave-browser"` | Brave Browser |
| `"brave-browser-stable"` | Brave Browser (stable channel) |
| `"microsoft-edge"` | Microsoft Edge |
| `"microsoft-edge-stable"` | Microsoft Edge (stable channel) |
| `"vivaldi"` | Vivaldi |
| `"vivaldi-stable"` | Vivaldi (stable channel) |
| `"opera"` | Opera |
| `"/usr/bin/chromium"` | Explicit path (skips PATH search) |

---

## [capture] Section

Optional section. When present, enables CDP traffic capture via the Chrome DevTools Protocol. Requires the `[capture]` extra: `pip install -e ".[capture]"` (installs `websockets` and `telemetry-monitor`).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `domains` | list of strings | `["tidal.com", "qobuz.com"]` | Domain suffixes to capture traffic for. Matching is suffix-based: `"tidal.com"` also captures `api.tidal.com`. |
| `db_path` | string | `~/.config/proxy-relay/capture/capture.db` | Path to the SQLite capture database. Created on first use with permissions `0600`. |
| `max_body_bytes` | integer | `65536` | Maximum UTF-8 bytes of request/response body and WebSocket payload stored per event. Larger payloads are truncated. |
| `cookie_poll_interval_s` | float | `30.0` | Seconds between `Network.getAllCookies` polls. Only new or changed cookies are written. |
| `storage_poll_interval_s` | float | `60.0` | Seconds between localStorage/sessionStorage polls. Only changed or removed keys are written. |
| `rotate_db` | boolean | `true` | Rename existing DB before opening a new session so each session gets a fresh database. Rotated files are named `capture-{ISO-timestamp}.db`. |
| `max_db_size_mb` | integer | `500` | Purge rotated capture DBs larger than this size in MiB. Set to `0` to disable size-based purge. |
| `max_db_age_days` | integer | `30` | Purge rotated capture DBs older than this many days. Set to `0` to disable age-based purge. |
| `max_cdp_reconnects` | integer | `50` | Maximum CDP WebSocket reconnect attempts before giving up. |
| `cdp_reconnect_delay_s` | float | `2.0` | Initial delay in seconds before the first CDP reconnect attempt. |
| `cdp_reconnect_backoff_factor` | float | `1.5` | Multiplicative factor applied to the delay after each reconnect attempt. |
| `cdp_reconnect_max_delay_s` | float | `60.0` | Upper bound on the inter-reconnect delay in seconds. |

```toml
[capture]
domains = ["tidal.com", "qobuz.com"]
max_body_bytes = 65536
cookie_poll_interval_s = 30.0
storage_poll_interval_s = 60.0
rotate_db = true
max_db_size_mb = 500
max_db_age_days = 30
```

Config values are used as defaults when `proxy-relay browse --capture` is run. All parameters can be overridden per-session with `--capture-domains` on the CLI. The `db_path` and header-redaction list cannot be overridden from the CLI; set them in `config.toml`.

### Header redaction

The following headers are always redacted in stored payloads (value replaced with the first 10 characters followed by `...`):

`authorization`, `cookie`, `set-cookie`, `x-tidal-token`, `x-user-auth-token`, `proxy-authorization`

### POST body redaction

The following field names are redacted (value replaced with `"[REDACTED]"`) in captured POST bodies. Both JSON and URL-encoded form bodies are supported.

`password`, `passwd`, `_password`, `client_secret`, `secret`, `g-recaptcha-response`, `recaptcha`

Custom fields can be configured via `redact_post_fields` in `CaptureConfig` (not yet exposed in TOML config).

### Database tables

All tables include a `session_id` column (UUID v4) to distinguish data from different capture sessions.

| Table | Contents |
|-------|---------|
| `http_requests` | URL, method, headers, POST body, request ID, session ID, profile |
| `http_responses` | URL, status, MIME type, headers, body, response latency (ms), session ID, profile |
| `cookies` | Domain, name, value (httpOnly values as SHA-256 hash), flags, expiry, session ID, profile |
| `storage_snapshots` | Origin, storage type (localStorage/sessionStorage), key, value, change type, session ID, profile |
| `websocket_frames` | Request ID, URL, direction (sent/received), payload, opcode, session ID, profile |
| `page_navigations` | URL, frame ID, transition type, MIME type, session ID, profile |

---

## CLI Commands

### `proxy-relay start`

Start the proxy relay server.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--host` | string | from config | Override bind address |
| `--port` | integer | from config | Override bind port |
| `--profile` | string | from config | Override proxy-st profile name |
| `--config` | path | `~/.config/proxy-relay/config.toml` | Use a different config file |
| `--log-level` | string | from config | Override log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### `proxy-relay stop`

Stop the running server. Sends SIGTERM to the process identified by the profile-scoped PID file.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--profile` | string | `"browse"` | proxy-st profile name — identifies which server instance to stop |

### `proxy-relay status [--json] [--all]`

Show server status: PID, bind address, upstream proxy, country, connection counts, and monitor stats.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--json` | flag | off | Output as JSON instead of human-readable text |
| `--all` | flag | off | Scan all `*.status.json` files, validate PIDs, show all live relays. Auto-cleans stale files. |
| `--profile` | string | `"browse"` | proxy-st profile name — identifies which server instance to query (ignored when `--all` is set) |

### `proxy-relay rotate`

Trigger an immediate upstream proxy rotation. Sends SIGUSR1 to the running process.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--profile` | string | `"browse"` | proxy-st profile name — identifies which server instance to rotate |

### `proxy-relay profile-clean`

List or delete browser profiles. Without arguments, lists all profiles. With names, deletes the specified profiles.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `names` | positional | *(none)* | Profile name(s) to delete (omit to list) |
| `--all` | flag | off | Delete all browser profiles |

**Examples:**
```bash
proxy-relay profile-clean                    # List all profiles
proxy-relay profile-clean miami steal        # Delete specific profiles
proxy-relay profile-clean --all              # Delete all profiles
```

### `proxy-relay browse`

Launch Chromium through the proxy relay. **Automatically starts a server if none is running** for the requested profile and stops it when the browser exits.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--rotate-min N` | integer | from config or `30` | Auto-rotate interval in minutes |
| `--no-rotate` | flag | off | Disable auto-rotation entirely |
| `--profile` | string | from config | Override proxy-st profile name (also selects the browser workspace) |
| `--config` | path | `~/.config/proxy-relay/config.toml` | Use a different config file |
| `--browser NAME` | string | auto-detect | Chromium-based browser binary name or path (e.g., `brave-browser`, `/usr/bin/google-chrome`) |
| `--capture` | flag | off | Enable CDP traffic capture. Requires `proxy-relay[capture]` (`websockets` + `telemetry-monitor`). |
| `--capture-domains DOMAINS` | string | from config or `tidal.com,qobuz.com` | Comma-separated domain suffixes to capture. Only used when `--capture` is set. |

**Profile resolution** (first match wins):
1. `--profile NAME` CLI flag → `NAME`
2. `default_proxy_profile` in config → config value
3. Default → `"browse"`

**Browser resolution** (first match wins):
1. `--browser NAME` CLI flag → `NAME`
2. `[browse] browser` in config → config value
3. Auto-detect → search candidates in order: Chromium (Snap), Chromium, Chrome, Chrome Stable, Edge, Brave, Vivaldi, Opera

**Server auto-start lifecycle:**

The browse command manages the server lifecycle automatically:

1. **Check** — reads the profile-scoped PID file (`~/.config/proxy-relay/{profile}.pid`) and checks if the process is alive.
2. **Reuse** — if a server is already running for this profile, reuses it (reads host/port from the status file).
3. **Auto-start** — if no server is running, starts one as a subprocess with `--port 0` (OS assigns a free port). Polls the status file until the server writes its actual port, with a 30-second timeout.
4. **Auto-stop** — when the browser exits (or on error), if the server was auto-started, it is terminated (SIGTERM, then SIGKILL after 5s). If the server was already running before browse, it is left untouched.

This means `proxy-relay browse` is fully self-contained — no need to run `proxy-relay start` first.

**Pre-flight checks (after server is ready, in order):**
1. Health check via internal `/__health` endpoint (server-side rotate+retry — see [Health Check](#health-check))
2. Locate Chromium binary on the system
3. Resolve timezone for the proxy exit country

**Supervisor behavior:**
- Polls proxy-relay PID every 2 seconds
- If proxy-relay dies → kills Chromium, exits with code 1
- If user closes Chromium → exits with code 0 (server keeps running if it was pre-existing; auto-stopped if it was auto-started)
- If user presses Ctrl-C → kills Chromium, exits with code 130 (auto-started server is auto-stopped)

---

## Chromium Flags (browse command)

These flags are passed to Chromium when launched by `proxy-relay browse`:

| Flag | Description |
|------|-------------|
| `--proxy-server=http://HOST:PORT` | Route all HTTP/HTTPS traffic through the local proxy-relay instance. Chromium sends CONNECT requests for HTTPS sites. |
| `--user-data-dir=PATH` | Use a separate browser profile directory. See below for details. |
| `--start-maximized` | Open the browser window maximized (full screen size, not fullscreen mode). |
| `--no-first-run` | Skip the "Welcome to Chromium" first-run dialog and setup wizard. |
| `--disable-default-apps` | Don't install default apps (Gmail, YouTube, etc.) on first launch. Keeps the profile clean. |
| `--disable-sync` | Disable Google account sync. Prevents data from leaking to/from your Google account across profiles. |
| `--disable-webrtc-stun-origin` | Prevent WebRTC STUN requests from leaking the real IP address. |
| `--enforce-webrtc-ip-permission-check` | Require explicit permission before WebRTC can access local IPs. |
| `--host-resolver-rules=...` | Force remote DNS resolution through the proxy. Prevents local DNS leaks that could reveal which sites you visit. Only applied when using a proxy. |

### `--user-data-dir` (browser profile isolation)

**Default behavior (without this flag):** Chromium uses `~/.config/chromium/` as its profile directory. This is your "normal" browser — all your cookies, history, passwords, extensions live here.

**What proxy-relay does:** Sets this to a profile directory, creating a completely separate browser identity per proxy-st profile.

**Snap Chromium detection:** If the Chromium binary is under `/snap/` (i.e., installed via `sudo snap install chromium`), the profile directory is placed under `~/snap/chromium/common/proxy-relay-profiles/{profile_name}/` instead. This is because Snap's sandbox prevents Chromium from writing to `~/.config/proxy-relay/`. Non-Snap installations use `~/.config/proxy-relay/browser-profiles/{profile_name}/`.

**What lives in a user-data-dir:**

| Data | Description |
|------|-------------|
| **Cookies** | Login sessions, tracking cookies, CSRF tokens — fully isolated per profile |
| **Cache** | Cached pages, images, scripts, fonts — no cross-profile sharing |
| **History** | Visited URLs, search history, download history |
| **Local Storage** | Site-specific key-value storage (used by many web apps) |
| **IndexedDB** | Client-side database storage (used by Gmail, Google Docs, etc.) |
| **Extensions** | Installed browser extensions (each profile starts clean) |
| **Preferences** | Browser settings, homepage, default search engine |
| **Saved passwords** | Autofill credentials (never shared between profiles) |
| **Bookmarks** | Separate per profile |

**Why this matters for privacy:**

1. **No cookie bleeding:** If you log into a site through `us-browse` profile, that cookie doesn't exist in `de-browse`. Sites can't correlate your sessions across different proxy exits.

2. **No cache fingerprinting:** Cached resources can be used to detect which sites you've visited. Each profile has its own cache, so one exit country's browsing leaves no trace in another.

3. **Clean from your daily browser:** Your personal browser at `~/.config/chromium/` is completely untouched. No proxy cookies leak into your daily browsing, and your real identity doesn't leak into the proxied session.

**Possible values:**

| Value | Effect |
|-------|--------|
| *(not set)* | Uses `~/.config/chromium/` (your real browser — **never do this through a proxy**) |
| `~/.config/proxy-relay/browser-profiles/browse/` | Default for non-Snap Chromium, profile `"browse"` |
| `~/snap/chromium/common/proxy-relay-profiles/browse/` | Default for Snap Chromium, profile `"browse"` |
| `~/.config/proxy-relay/browser-profiles/us-browse/` | Profile named `us-browse` (non-Snap) |
| `/tmp/throwaway-session/` | Ephemeral session (lost on reboot) |

**Disk usage:** Each profile starts at ~5 MB and grows with cache. Typical active profile: 50–500 MB depending on browsing.

### Snap Chromium — known limitations

When Chromium is installed via Snap (`sudo snap install chromium`), the Snap sandbox (AppArmor confinement) imposes restrictions that affect proxy-relay:

| Limitation | Impact | Workaround |
|-----------|--------|------------|
| **Profile directory restriction** | Chromium can only write to `~/snap/chromium/common/`. Profiles must go there, not `~/.config/proxy-relay/`. | proxy-relay auto-detects Snap and redirects profiles. Symlinks provide convenience access at the original location. |
| **Signal delivery blocked** | `os.kill()`, `os.killpg()`, and `pkill` cannot signal Snap Chromium processes — even from the parent process, even with the same UID. AppArmor returns `EPERM`. | `close_browser()` calls `process.terminate()`, which may silently fail. Child processes (zygote, GPU, renderer) linger 5–15 seconds until the Snap sandbox cleans them up. |
| **No cross-session signalling** | Once the parent Python process exits, Snap Chromium children become unkillable from any other process. | Ensure `close_browser()` is called **before** the parent exits. The `finally` block in tidal-dl's login flow handles this. |

**What is NOT affected:**
- Other Chromium windows or browser sessions (each is isolated by `--user-data-dir`)
- Non-Snap browsers (Chrome, Brave, Edge, Vivaldi, Opera) — these respond normally to signals
- Profile data integrity — no corruption from lingering child processes
- Network connections — proxy-relay server is stopped independently via `auto_stop_server()`

**Recommendation:** If clean process termination is important, use a non-Snap Chromium installation (e.g., `google-chrome` from the official `.deb` package or `brave-browser`). Configure via `[browse] browser = "google-chrome"` in `config.toml`.

### `TZ` environment variable (timezone spoofing)

**Not a Chromium flag** but an environment variable set on the Chromium process.

**Default behavior (without this):** Chromium inherits the system timezone. If your system is `America/Bogota` but your proxy exits in Germany, JavaScript reports `America/Bogota` — a detectable mismatch.

**What proxy-relay does:** Automatically looks up the proxy exit country and sets `TZ` to a matching IANA timezone. Chromium (and all JavaScript running in it) then reports the correct timezone.

**Example:** Proxy exits in Germany → `TZ=Europe/Berlin` → JavaScript `Intl.DateTimeFormat().resolvedOptions().timeZone` returns `"Europe/Berlin"`.

**Your system timezone is NOT changed.** Only the Chromium process sees the overridden `TZ`. All other programs on your system keep your real timezone.

---

## Concepts

### Rolling Window

A fixed-size buffer (default: 100 entries) that holds the most recent connection records. When full, the oldest entry is dropped to make room for new ones. This is how the monitor tracks "recent" quality — errors from 200 connections ago don't count against the threshold.

### Upstream Rotation

When proxy-relay rotates, it:
1. Tells proxy-st to invalidate the current sticky session
2. proxy-st generates a new session ID
3. The SOCKS5 provider (e.g., IPRoyal) assigns a new exit IP
4. All new connections use the fresh IP

Existing connections are NOT interrupted — only new connections go through the new IP.

#### Rotation triggers

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ROTATION FLOW                                │
│                                                                     │
│  ┌─────────────────┐    ┌──────────────────┐   ┌────────────────┐  │
│  │ 1. Manual        │    │ 2. Monitor        │   │ 3. Health      │  │
│  │ proxy-relay      │    │ error_threshold   │   │    check retry │  │
│  │ rotate           │    │ reached           │   │    (/__health) │  │
│  │ (sends SIGUSR1)  │    │ (2 errors in 100) │   │    on failure  │  │
│  └───────┬──────────┘    └────────┬──────────┘   └───────┬────────┘  │
│          │                        │                       │          │
│          │   ┌────────────────┐   │                       │          │
│          │   │ 4. Browse timer │   │                       │          │
│          │   │ every N min     │   │                       │          │
│          │   │ (sends SIGUSR1) │   │                       │          │
│          │   └───────┬────────┘   │                       │          │
│          │           │            │                       │          │
│          ▼           ▼            ▼                       ▼          │
│       ┌──────────────────────────────────────────────────────┐      │
│       │            server._do_rotate()                        │      │
│       │  → upstream_manager.rotate()                          │      │
│       │  → new session ID → new exit IP                       │      │
│       │  → status.json updated (country, upstream_url)        │      │
│       └──────────────────────────────────────────────────────┘      │
│                              │                                      │
│                              ▼                                      │
│                 ┌───────────────────────┐                           │
│                 │ ⚠ Browser TZ is NOT   │                           │
│                 │   updated — see below │                           │
│                 └───────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
```

#### Timezone and rotation — known limitation

The `TZ` environment variable is set **once at browser launch** and cannot be updated while the browser is running:

```
proxy-relay browse
    │
    ├─ 1. Server ready → read status.json → country = "DE"
    ├─ 2. Lookup timezone → TZ = "Europe/Berlin"
    ├─ 3. Launch Chromium with TZ=Europe/Berlin    ◄── SET ONCE
    │       └─ JS: Intl.DateTimeFormat() → "Europe/Berlin" ✓
    │
    ├─ ... 30 min later: auto-rotation ...
    │       └─ Server rotates → new exit IP in Japan
    │       └─ status.json: country = "JP"
    │       └─ Browser still has TZ=Europe/Berlin  ◄── NOT UPDATED
    │           └─ JS: Intl.DateTimeFormat() → "Europe/Berlin" ✗
    │              (IP is Japanese, but timezone says Berlin)
    │
    └─ Browser closed → session ends
```

**Why:** `subprocess.Popen(env=...)` sets the environment at process creation. There is no OS mechanism to modify a running process's environment variables from outside.

**Mitigation:** When using `--no-rotate` or with country-pinned proxy-st profiles (same country on every rotation), this is not an issue — the timezone stays correct for the entire session.

### Sticky Sessions

proxy-st supports sticky sessions — the same exit IP is reused for a configurable duration (e.g., 10 minutes). This is controlled in proxy-st's config, not in proxy-relay. proxy-relay simply triggers rotation when needed.

### Health Check

Before launching Chromium, `proxy-relay browse` calls the server's internal `/__health` endpoint:

```
browse command → GET http://127.0.0.1:8080/__health → server health_check()
                                                        │
                                                        ├─ attempt 1: SOCKS5 tunnel → icanhazip.com
                                                        │   └─ fail → rotate upstream
                                                        ├─ attempt 2: SOCKS5 tunnel → icanhazip.com
                                                        │   └─ fail → rotate upstream
                                                        └─ attempt 3: SOCKS5 tunnel → icanhazip.com
                                                            └─ fail → return 503
```

**How it works:**

1. The browse command sends a plain HTTP GET to `http://host:port/__health` (no proxy handler — direct connection to the relay server).
2. The server intercepts this request before it reaches the SOCKS5 forwarding path.
3. The server opens a SOCKS5 tunnel to `icanhazip.com` through the upstream proxy and reads the exit IP.
4. **On failure:** the server rotates the upstream (new session/IP) and retries — up to 3 attempts.
5. Returns JSON: `{"ok": true, "exit_ip": "x.x.x.x"}` (HTTP 200) or `{"ok": false, "error": "..."}` (HTTP 503).

**Why server-side:** All rotation logic stays inside the server process. The browse command just asks "are you healthy?" — it never needs to know about rotation, upstream resolution, or retry mechanics. This prevents duplicate rotation logic across multiple callers.

**Timeout:** The browse command waits up to 60 seconds for the server response (3 attempts × 15s each, plus rotation overhead).

### PID File

Located at `~/.config/proxy-relay/{profile}.pid` (e.g., `browse.pid`, `us-browse.pid`). Contains the process ID of the running proxy-relay server for that profile. Used by `stop`, `status`, `rotate`, and `browse` to find and communicate with the correct server instance.

Legacy single-instance path (`~/.config/proxy-relay/proxy-relay.pid`) is recognized by the `stop` command for backward compatibility but is no longer written by new server instances.

### Status File

Located at `~/.config/proxy-relay/{profile}.status.json` (e.g., `browse.status.json`). Updated after every connection. Contains the current bind address, port, upstream URL, country, connection counts, and monitor statistics. Used by `status` and `browse` to read the running server's actual configuration.

### Multi-Instance Support

Profile-scoped PID and status files enable running multiple proxy-relay servers simultaneously, each serving a different proxy-st profile. For example:

```bash
# Terminal 1: US proxy on auto-assigned port
proxy-relay start --profile us-browse

# Terminal 2: DE proxy on auto-assigned port
proxy-relay start --profile de-browse

# Check status of each
proxy-relay status --profile us-browse
proxy-relay status --profile de-browse

# Browse through a specific profile (auto-starts if not running)
proxy-relay browse --profile us-browse
```

Each instance writes its own `{profile}.pid` and `{profile}.status.json` file, and each browser session gets its own isolated Chromium profile directory.

---

## Python Library API

proxy-relay exposes a public Python API through `proxy_relay.__init__`. All symbols below are importable directly from the package:

```python
import proxy_relay
# or
from proxy_relay import ProxyServer, RelayConfig, open_browser, ...
```

### Version

| Symbol | Type | Description |
|--------|------|-------------|
| `__version__` | `str` | Package version string (e.g. `"0.1.0"`). |

### Core API

| Symbol | Signature | Description |
|--------|-----------|-------------|
| `RelayConfig` | `class` | Root configuration dataclass. Load from TOML via `RelayConfig.load(path)`. |
| `ProxyServer` | `class` | Local HTTP CONNECT proxy server. Constructed with `host`, `port`, `upstream_manager`, `monitor_config`. Managed via `await server.start()` / `await server.stop()`. |
| `UpstreamManager` | `class` | Wraps proxy-st to resolve SOCKS5 upstream URLs. `get_upstream()` returns an `UpstreamInfo`, `rotate()` forces a new session. |
| `run_server` | `async def run_server(host, port, profile_name, on_ready, monitor_config) -> None` | Convenience coroutine: creates `UpstreamManager` + `ProxyServer` and runs until shutdown. |

### Browser API

| Symbol | Signature | Description |
|--------|-----------|-------------|
| `BrowserHandle` | `dataclass` | Handle to a launched browser process. Fields: `process` (Popen), `profile_dir` (Path), `chromium_path` (Path). |
| `BrowseError` | `exception` | Raised by browser API functions on failure (no Chromium found, launch failed, health check failed). |
| `can_launch_browser` | `() -> bool` | Returns `False` in headless/SSH environments or when no Chromium binary is found. |
| `find_chromium` | `() -> Path` | Locate the first Chromium-based browser on the system. Raises `BrowseError` if none found. |
| `open_browser` | `(url, *, proxy_host, proxy_port, profile_name, chromium_path, timezone) -> BrowserHandle` | Launch Chromium configured for proxied browsing. Caller manages lifecycle via `close_browser`. |
| `open_browser_tab` | `(handle, url) -> None` | Open a URL in a running Chromium session (new tab via same `--user-data-dir`). |
| `close_browser` | `(handle) -> None` | Terminate the browser (SIGTERM then SIGKILL). Never raises. |
| `auto_start_server` | `(profile_name, host, config_path, log_level) -> Popen` | Start a proxy-relay server subprocess with `--port 0` (OS assigns port). |
| `wait_for_server_ready` | `(profile_name, server_proc, timeout) -> tuple[str, int]` | Poll the status file until the server writes its actual host/port, or raise `BrowseError` on timeout. |
| `auto_stop_server` | `(server_proc, profile_name) -> None` | Stop an auto-started server subprocess gracefully (SIGTERM then SIGKILL). |
| `health_check` | `(proxy_host, proxy_port) -> str` | Call the server's `/__health` endpoint; returns the exit IP. Raises `BrowseError` on failure. |

### Timezone API

| Symbol | Signature | Description |
|--------|-----------|-------------|
| `get_timezone_for_country` | `(country_code: str) -> str \| None` | Return a representative IANA timezone string for an ISO alpha-2 country code (case-insensitive). Returns `None` for unknown codes. Cached via `lru_cache`. |
