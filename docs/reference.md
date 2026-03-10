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
- [CLI Commands](#cli-commands)
- [Chromium Flags (browse command)](#chromium-flags-browse-command)
- [Concepts](#concepts)

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
| `proxy_st_profile` | string | `"browse"` | Name of the proxy-st profile to use for upstream SOCKS5 connections. Must match a profile defined in proxy-st's config. |

### `log_level`

Controls the minimum severity of log messages printed to the console.

| Value | What you see |
|-------|-------------|
| `"DEBUG"` | Everything: connection details, URL building, header stripping, tunnel establishment. Very verbose. |
| `"INFO"` | Normal operation: startup, upstream resolution, rotations, browser launch. Recommended for daily use. |
| `"WARNING"` | Only problems: slow connections, timezone mismatches, rotation triggers, proxy deaths. |
| `"ERROR"` | Only failures: fatal errors, unrecoverable tunnel failures, process crashes. |

### `proxy_st_profile`

The profile name from your proxy-st configuration (`~/.config/proxy-st/config.toml`). Each profile defines a country, session lifetime, and connection parameters.

```toml
# Use the "us-browse" profile from proxy-st
proxy_st_profile = "us-browse"
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

Stop the running server. Sends SIGTERM to the process identified by the PID file.

### `proxy-relay status [--json]`

Show server status: PID, bind address, upstream proxy, country, connection counts, and monitor stats.

| Flag | Description |
|------|-------------|
| `--json` | Output as JSON instead of human-readable text |

### `proxy-relay rotate`

Trigger an immediate upstream proxy rotation. Sends SIGUSR1 to the running process.

### `proxy-relay browse`

Launch Chromium through the running proxy relay.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--rotate-min N` | integer | from config or `30` | Auto-rotate interval in minutes |
| `--no-rotate` | flag | off | Disable auto-rotation entirely |
| `--profile` | string | from config | Override proxy-st profile name (also selects the browser workspace) |
| `--config` | path | `~/.config/proxy-relay/config.toml` | Use a different config file |

**Profile resolution** (first match wins):
1. `--profile NAME` CLI flag → `NAME`
2. `proxy_st_profile` in config → config value
3. Default → `"browse"`

**Pre-flight checks (in order):**
1. Verify proxy-relay is running (PID file + process liveness)
2. Health check via internal `/__health` endpoint (server-side rotate+retry — see [Health Check](#health-check))
3. Locate Chromium binary on the system
4. Resolve timezone for the proxy exit country

**Supervisor behavior:**
- Polls proxy-relay PID every 2 seconds
- If proxy-relay dies → kills Chromium, exits with code 1
- If user closes Chromium → exits with code 0 (proxy keeps running)
- If user presses Ctrl-C → kills Chromium, exits with code 130

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

Located at `~/.config/proxy-relay/proxy-relay.pid`. Contains the process ID of the running proxy-relay server. Used by `stop`, `status`, `rotate`, and `browse` to find and communicate with the running instance.

### Status File

Located at `~/.config/proxy-relay/status.json`. Updated after every connection. Contains the current bind address, upstream URL, country, connection counts, and monitor statistics. Used by `status` and `browse` to read the running server's actual configuration.
