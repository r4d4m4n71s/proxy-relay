# proxy-relay

Local HTTP/CONNECT proxy that forwards all traffic through an upstream SOCKS5 proxy resolved via [proxy-st](https://github.com/r4d4m4n71s/proxy-st).

## Features

- **HTTP CONNECT tunneling** — full HTTPS support via CONNECT method
- **Plain HTTP forwarding** — GET/POST/etc. requests forwarded through SOCKS5
- **DNS leak prevention** — all DNS resolution happens at the SOCKS5 exit node, never locally
- **Header sanitization** — strips leak-prone headers (X-Forwarded-For, Via, etc.)
- **Connection monitoring** — rolling window quality tracking with auto-rotation
- **Timezone mismatch detection** — warns if local TZ doesn't match proxy exit country
- **CLI management** — start/stop/status/rotate via command line or signals

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
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `proxy-relay start` | Start the proxy relay server |
| `proxy-relay stop` | Stop the running server (sends SIGTERM) |
| `proxy-relay status` | Show server status and connection stats |
| `proxy-relay status --json` | Output status as JSON |
| `proxy-relay rotate` | Trigger upstream proxy rotation (sends SIGUSR1) |
| `proxy-relay --version` | Show version |

### Start Options

| Flag | Description |
|------|-------------|
| `--host` | Bind address (default: from config) |
| `--port` | Bind port (default: from config) |
| `--profile` | proxy-st profile name |
| `--config` | Path to config file |
| `--log-level` | Log level: DEBUG, INFO, WARNING, ERROR |

## Architecture

```
Browser ──HTTP CONNECT──> proxy-relay (127.0.0.1:8080)
                              |
                              |── Header sanitization
                              |── DNS leak prevention
                              |── Connection monitoring
                              |
                              v
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

## License

Private — not for redistribution.
