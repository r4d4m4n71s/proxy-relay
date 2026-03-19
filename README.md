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
- **CDP traffic capture** — record HTTP requests, responses, cookies, localStorage, and WebSocket frames from target domains via the Chrome DevTools Protocol (optional; requires `websockets` + `telemetry-monitor`)
- **CLI management** — start/stop/status/rotate/browse via command line or signals

## Installation

```bash
# From local source (requires proxy-st installed first)
pip install -e .

# With CDP traffic capture support (websockets + telemetry-monitor)
pip install -e ".[capture]"
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

Config file: `~/.config/proxy-relay/config.toml` (created on first run with commented defaults, permissions `0600`).

See [`docs/reference.md`](docs/reference.md) for the full configuration reference with all parameters, value tables, and behavioral explanations.

## CLI Reference

| Command | Description |
|---------|-------------|
| `proxy-relay start` | Start the proxy relay server |
| `proxy-relay stop` | Stop the running server |
| `proxy-relay status` | Show server status and connection stats |
| `proxy-relay rotate` | Trigger upstream proxy rotation |
| `proxy-relay browse` | Launch Chromium through the proxy (auto-starts server if needed); add `--capture` to record CDP traffic |
| `proxy-relay profile-clean` | Remove PID/status files for stopped instances |
| `proxy-relay analyze` | Analyze captured CDP traffic from the SQLite database |
| `proxy-relay --version` | Show version |

All commands accept `--profile` to target a specific proxy-st profile (default: `browse`). Multiple profiles can run simultaneously as separate server instances.

See [`docs/reference.md`](docs/reference.md) for full flag details per command.

## Architecture

```
proxy-relay browse
      |
      |── Check: is server running for this profile?
      |   ├─ YES → reuse existing server (read host:port from status file)
      |   └─ NO  → auto-start server subprocess (--port 0 for OS-assigned port)
      |            └─ poll status file until server is ready
      |
      |── GET /__health ────────────────────> server
      |                                        |── try SOCKS5 tunnel
      |                                        |   ├─ ok → return exit IP
      |                                        |   └─ fail → rotate → retry (×3)
      |<── {"ok":true,"exit_ip":"x.x.x.x"} ──|
      |── Find Chromium
      |── Create/reuse profile dir
      |
      v
  Chromium ──HTTP CONNECT──> proxy-relay (127.0.0.1:<port>)
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

On exit: if server was auto-started, it is auto-stopped (SIGTERM → SIGKILL).
         If server was already running, it keeps running.
```

## Traffic Capture

When `proxy-relay[capture]` is installed, passing `--capture` to the browse command records HTTP traffic, cookies, localStorage, and WebSocket frames from Chromium via the Chrome DevTools Protocol (CDP).

```bash
# Capture traffic for the default domains (tidal.com, qobuz.com)
proxy-relay browse --capture

# Capture traffic for specific domains
proxy-relay browse --capture --capture-domains api.example.com,cdn.example.com
```

**How it works:** Chromium is launched with `--remote-debugging-port` on a free local port. A background thread connects to the CDP WebSocket, subscribes to `Network.*` events, and writes structured rows to `~/.config/proxy-relay/capture.db` (SQLite, permissions `0600`) via telemetry-monitor's `BackgroundWriter`.

**What is captured per domain match:**
- HTTP requests — URL, method, headers (sensitive headers redacted to first 10 chars), POST body (truncated to `max_body_bytes`)
- HTTP responses — status, MIME type, headers, body, response latency
- Cookies — polled every `cookie_poll_interval_s` seconds; only new or changed cookies are stored; `httpOnly` values are stored as SHA-256 hashes
- localStorage / sessionStorage — polled every `storage_poll_interval_s` seconds; only changed or removed keys are stored
- WebSocket frames — all sent/received frames (not filtered by domain)

Domain matching is suffix-based: configuring `tidal.com` also captures `api.tidal.com` and `listen.tidal.com`.

Capture is configured via the `[capture]` section of `config.toml` or overridden per-session with CLI flags. See [`docs/reference.md`](docs/reference.md) for all parameters.

## Security

- **No local DNS resolution** — hostnames are passed as strings to the SOCKS5 connector with `rdns=True`
- **Header stripping** — removes X-Forwarded-For, Via, Proxy-Authorization, and other leak headers
- **Timezone check** — warns if system timezone doesn't match proxy exit country
- **Profile-scoped PID files** — each profile runs as an independent instance (`~/.config/proxy-relay/{profile}.pid`), enabling multi-instance support
- **Isolated browser profiles** — each proxy-st profile gets a separate Chromium user-data directory, preventing cross-profile data leakage

## License

Private — not for redistribution.
