# proxy-relay

Local HTTP/CONNECT proxy that forwards all traffic through an upstream SOCKS5 proxy resolved via [proxy-st](https://github.com/r4d4m4n71s/proxy-st).

## Features

- **HTTP CONNECT tunneling** — full HTTPS support via CONNECT method
- **Plain HTTP forwarding** — GET/POST/etc. requests forwarded through SOCKS5
- **DNS leak prevention** — all DNS resolution happens at the SOCKS5 exit node, never locally
- **Header sanitization** — strips leak-prone headers (X-Forwarded-For, Via, etc.)
- **Connection monitoring** — rolling window quality tracking with auto-rotation
- **Timezone mismatch detection** — warns if local TZ doesn't match proxy exit country
- **Supervised browsing** — launch Chromium through the proxy with isolated profiles, health checks, and auto-rotation
- **CLI management** — start/stop/status/rotate/browse via command line or signals

## Installation

```bash
# From local source (requires proxy-st installed first)
pip install -e .
```

## Quick Start

```bash
# Start the relay (uses default config or ~/.config/proxy-relay/config.toml)
proxy-relay start

# Check status
proxy-relay status

# Launch Chromium through the proxy (auto-rotates every 30 min)
proxy-relay browse

# Rotate upstream proxy
proxy-relay rotate

# Stop
proxy-relay stop
```

## Configuration

Config file: `~/.config/proxy-relay/config.toml`

```toml
log_level = "INFO"
proxy_st_profile = "browse"

[server]
host = "127.0.0.1"
port = 8080

[monitor]
enabled = true
slow_threshold_ms = 2000.0
error_threshold_count = 5
window_size = 100

[anti_leak]
warn_timezone_mismatch = true

[browse]
rotate_interval_min = 30  # auto-rotate every N minutes (0 = disabled)
```

### `[browse]` section

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rotate_interval_min` | int | `30` | Minutes between automatic upstream proxy rotations. Set to `0` to disable. |

## CLI Reference

| Command | Description |
|---------|-------------|
| `proxy-relay start` | Start the proxy relay server |
| `proxy-relay stop` | Stop the running server (sends SIGTERM) |
| `proxy-relay status` | Show server status and connection stats |
| `proxy-relay status --json` | Output status as JSON |
| `proxy-relay rotate` | Trigger upstream proxy rotation (sends SIGUSR1) |
| `proxy-relay browse` | Launch Chromium through the running proxy relay |
| `proxy-relay --version` | Show version |

### Start Options

| Flag | Description |
|------|-------------|
| `--host` | Bind address (default: from config) |
| `--port` | Bind port (default: from config) |
| `--profile` | proxy-st profile name |
| `--config` | Path to config file |
| `--log-level` | Log level: DEBUG, INFO, WARNING, ERROR |

### Browse Options

| Flag | Description |
|------|-------------|
| `--rotate-min N` | Auto-rotate interval in minutes (default: from config or 30) |
| `--no-rotate` | Disable auto-rotation entirely |
| `--profile NAME` | Override proxy-st profile (also selects the browser workspace) |
| `--config` | Path to config file |

## Browse Command

The `browse` command launches Chromium routed through the running proxy-relay, with proxy-chain verification, profile isolation, relay supervision, and optional auto-rotation.

### Prerequisites

proxy-relay must already be running. Start it first:

```bash
proxy-relay start
proxy-relay browse
```

### Usage

```bash
# Launch with defaults (30-minute auto-rotation from config)
proxy-relay browse

# Rotate upstream proxy every 15 minutes
proxy-relay browse --rotate-min 15

# Disable auto-rotation
proxy-relay browse --no-rotate

# Use a custom config file
proxy-relay browse --config /path/to/config.toml
```

### What happens on launch

1. **PID check** — verifies proxy-relay is running (reads PID file).
2. **Health check** — calls the server's internal `/__health` endpoint. The server verifies upstream connectivity through the SOCKS5 tunnel, automatically rotating and retrying (up to 3 attempts) if the upstream is unreachable. Prints the exit IP on success.
3. **Chromium discovery** — locates a Chromium or Chrome binary. Checks `/snap/bin/chromium` first, then `chromium`, `chromium-browser`, and `google-chrome` on PATH.
4. **Profile isolation** — creates (or reuses) a dedicated browser profile at `~/.config/proxy-relay/browser-profiles/{profile}/`. Each proxy-st profile gets its own directory with separate cookies, cache, history, and extensions — fully isolated from the user's normal browser and from other proxy profiles.
5. **Browser launch** — starts Chromium with `--proxy-server`, `--user-data-dir`, `--start-maximized`, `--no-first-run`, `--disable-default-apps`, `--disable-sync`.
6. **Supervision loop** — the `browse` command stays running. A background thread polls proxy-relay's PID every 2 seconds. If the relay dies, Chromium is terminated automatically.
7. **Auto-rotation** (if enabled) — a background thread sends `SIGUSR1` to proxy-relay at the configured interval to rotate the upstream proxy.

### Rotation interval resolution

The rotation interval is resolved in this priority order:

1. `--no-rotate` flag — sets interval to 0 (disabled)
2. `--rotate-min N` flag — uses the CLI value
3. `[browse] rotate_interval_min` in config — uses the config value
4. Default: 30 minutes

### Exit behavior

| Scenario | What happens | Exit code |
|----------|-------------|-----------|
| User closes Chromium | Command exits cleanly, proxy-relay keeps running | 0 |
| User presses Ctrl-C | Chromium is terminated, command exits | 130 |
| proxy-relay dies/crashes | Chromium is killed automatically, warning printed | 1 |

### Error scenarios

| Error | Message | Solution |
|-------|---------|----------|
| Proxy not running | `proxy-relay is not running — start it first with: proxy-relay start` | Run `proxy-relay start` first |
| Health check fails | `Health check failed: <details>` | The server already tried rotating 3 times. Check that the upstream SOCKS5 provider is reachable, or try a different profile/country. |
| Chromium not found | `Chromium not found. Install chromium or google-chrome and ensure it is on PATH.` | Install Chromium: `sudo snap install chromium` or `sudo apt install chromium-browser` |
| Config error | `Configuration error: <details>` | Fix the TOML config file |

## Architecture

```
proxy-relay browse                    proxy-relay start
      |                                      |
      |── GET /__health ─────────────────>   |── try SOCKS5 tunnel
      |                                      |   ├─ ok → return exit IP
      |                                      |   └─ fail → rotate → retry (×3)
      |<── {"ok":true,"exit_ip":"x.x.x.x"} ─|
      |── Find Chromium                      |
      |── Create profile dir                 |
      |                                      |
      v                                      v
  Chromium ──HTTP CONNECT──> proxy-relay (127.0.0.1:8080)
      ^                          |
      |                          |── Header sanitization
      |                          |── DNS leak prevention
   Supervisor                    |── Connection monitoring
      |── PID poll (2s)          |
      |── Auto-rotate (SIGUSR1)  v
                            SOCKS5 tunnel ──> upstream proxy (via proxy-st)
                                 |
                                 v
                            Target server
```

## Security

- **No local DNS resolution** — hostnames are passed as strings to the SOCKS5 connector with `rdns=True`
- **Header stripping** — removes X-Forwarded-For, Via, Proxy-Authorization, and other leak headers
- **Timezone check** — warns if system timezone doesn't match proxy exit country
- **PID file management** — prevents multiple instances, clean shutdown
- **Isolated browser profiles** — each proxy-st profile gets a separate Chromium user-data directory, preventing cross-profile data leakage

## License

Private — not for redistribution.
