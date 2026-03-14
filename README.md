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

Config file: `~/.config/proxy-relay/config.toml` (created on first run with commented defaults, permissions `0600`).

See [`docs/reference.md`](docs/reference.md) for the full configuration reference with all parameters, value tables, and behavioral explanations.

## CLI Reference

| Command | Description |
|---------|-------------|
| `proxy-relay start` | Start the proxy relay server |
| `proxy-relay stop` | Stop the running server |
| `proxy-relay status` | Show server status and connection stats |
| `proxy-relay rotate` | Trigger upstream proxy rotation |
| `proxy-relay browse` | Launch Chromium through the proxy (auto-starts server if needed) |
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

## Security

- **No local DNS resolution** — hostnames are passed as strings to the SOCKS5 connector with `rdns=True`
- **Header stripping** — removes X-Forwarded-For, Via, Proxy-Authorization, and other leak headers
- **Timezone check** — warns if system timezone doesn't match proxy exit country
- **Profile-scoped PID files** — each profile runs as an independent instance (`~/.config/proxy-relay/{profile}.pid`), enabling multi-instance support
- **Isolated browser profiles** — each proxy-st profile gets a separate Chromium user-data directory, preventing cross-profile data leakage

## License

Private — not for redistribution.
