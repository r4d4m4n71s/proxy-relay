# proxy-relay -- Claude Code Context

## Project
Local HTTP/CONNECT proxy that tunnels traffic through upstream SOCKS5 proxies via proxy-st (IProyal).
Stack: Python >=3.12, asyncio, python-socks, proxy-st, tomlkit.
Repo: https://github.com/r4d4m4n71s/proxy-relay (private)
All persistent state: `~/.config/proxy-relay/` (config.toml, PID files, status files, browser profiles)

## Project status
**300 tests on `main`** | 16 production modules, 15 test files. S63 complete: async I/O refactor in `server.py` (C4-17).

### Async I/O refactor (S63 — C4-17)
4 blocking I/O paths in `server.py` are now wrapped with `asyncio.to_thread()`:
- `_update_status_file_async()` — async wrapper around sync status file write
- `_on_connection` finally block — uses async status write
- `_do_rotate()` — wraps `upstream_manager.rotate()`
- `start()` — wraps `get_upstream()` and `write_pid()`

## Global rule overrides

| Global rule | This project |
|-------------|-------------|
| `pydantic` for data models | `dataclasses` -- no pydantic dependency |
| Async-first I/O (`httpx`) | Async via `asyncio` + `python-socks[asyncio]`; no httpx |
| Line length: 88 (Black) | `line-length = 100`, linter is `ruff` (see `[tool.ruff]` in pyproject.toml) |
| Python 3.10+ | `requires-python = ">=3.12"`, ruff target `py312` |

## Critical rules

### Security
- Proxy credentials live in proxy-st config (`~/.config/proxy-st/config.toml`) -- never committed
- `config.toml` is outside the repo (`~/.config/proxy-relay/`); no credential files in the repo
- PID/status files get `chmod 0o600`
- **DNS leak prevention**: hostnames are NEVER resolved locally; always passed as strings to SOCKS5 (`rdns=True`)

### Code conventions
- Every non-init module: `from __future__ import annotations` as the first import
- Every module with I/O or side effects: `from proxy_relay.logger import get_logger` / `log = get_logger(__name__)`
- Config reading: `tomllib` (stdlib, read-only). Config writing: `tomlkit` (preserves comments)
- Lazy imports for proxy-st (`proxy_st.config`, `proxy_st.url`, `proxy_st.session_store`) -- always inside methods

### Architecture
- Dependency direction: cli -> server/browse -> handler/tunnel/forwarder/monitor -> upstream/sanitizer/tz -> config/logger/exceptions
- Server is async (`asyncio.start_server`); browse supervisor is threaded (subprocess + threading)
- Signal-based IPC: SIGTERM (stop), SIGUSR1 (rotate)

### Git workflow
- **NEVER push directly to `main`**. Always use feature branches.
- Commit format: `<type>(<scope>): <summary>`

## Module map
```
proxy_relay/
    __init__.py         Lazy public API: ProxyServer, RelayConfig, UpstreamManager, run_server
    __main__.py         python -m proxy_relay entry point
    cli.py              CLI: start, stop, status, rotate, browse subcommands (argparse)
    config.py           TOML config loader, RelayConfig + section dataclasses
    exceptions.py       Exception hierarchy: ProxyRelayError -> ConfigError, UpstreamError, TunnelError, BrowseError
    logger.py           get_logger() + configure_logging()
    server.py           ProxyServer: async TCP accept loop, health check, signal handlers, PID/status files
    handler.py          Connection dispatcher: CONNECT tunnel, plain HTTP forward, health endpoint
    tunnel.py           SOCKS5 tunnel via python-socks (rdns=True), bidirectional relay
    forwarder.py        Plain HTTP request forwarding through SOCKS5
    upstream.py         UpstreamManager: proxy-st integration, URL building, session rotation
    monitor.py          ConnectionMonitor: rolling-window quality tracking, auto-rotation trigger
    sanitizer.py        Header sanitization: strip X-Forwarded-For, Via, Proxy-Authorization, etc.
    pidfile.py          PID/status file ops: profile-scoped .pid and .status.json
    tz.py               Timezone mismatch detection + country-to-IANA mapping (40+ countries)
    browse.py           Chromium discovery, health check client, BrowseSupervisor, auto-start/stop server
```

## Tests
- Run: `.venv/bin/pytest tests/ -v --tb=short`
- Install: `uv venv && uv pip install -e ".[dev]"`
- All tests in `tests/unit/`; pytest-asyncio with `asyncio_mode = "auto"`

## Docs (load on demand)
- @README.md -- features, quick start, architecture overview, CLI reference
- @docs/reference.md -- full config parameter reference with explanations
