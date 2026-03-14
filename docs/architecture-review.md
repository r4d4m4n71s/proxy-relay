# Architecture Review — 2026-03-14

Cross-project architect review (2 passes: code analysis + documentation drift).

## Summary

- Pass 1 (code): 20 findings (0 high, 6 medium, 14 low)
- Pass 2 (docs): 11 findings (1 high, 3 medium, 7 low)
- Total: 31 findings

## Findings

### HIGH

#### Doc findings

| ID | Finding | File | Status |
|----|---------|------|--------|
| D04 | No Python library usage documentation anywhere — tidal-dl consumes 8 symbols with no docs | `README.md`, `docs/` (absent) | open |

---

### MEDIUM

#### Code findings

| ID | Finding | File | Status |
|----|---------|------|--------|
| F04 | Profile name unsanitized in PID/status paths — `../` path traversal via --profile | `pidfile.py:43-55` | done |
| F08 | `health_check` uses `urlopen` which respects env proxy vars — should disable | `browse.py:370` | open |
| F12 | `forward_http_request` reads entire response into memory (10 MiB max) | `forwarder.py:91-98` | open |
| F14 | No warning when `server.host` binds to non-loopback (0.0.0.0) | `config.py:187` | open |
| F18 | No Content-Length body size limit in `_handle_http` | `handler.py:254-268` | open |

#### Doc findings

| ID | Finding | File | Status |
|----|---------|------|--------|
| D06 | `browse` command never calls `configure_logging()` — config log level silently ignored | `cli.py:461-584` | done |
| D07 | `_do_rotate()` does not update status file — docs say it does, code doesn't | `server.py:177-194` | done |
| D09 | tidal-dl imports `proxy_relay.tz` outside public API — undocumented cross-project dependency | `__init__.py` (missing export) | done |

---

### LOW

#### Code findings

| ID | Finding | File | Status |
|----|---------|------|--------|
| F01 | `__main__.py` missing `from __future__ import annotations` | `__main__.py:1` | open |
| F02 | `tomlkit` declared as runtime dep but never imported | `pyproject.toml:14` | open |
| F05 | `open_browser_tab` does not catch OSError | `browse.py:242-246` | open |
| F07 | Double UpstreamManager creation in `_cmd_start` | `cli.py:226-260` | open |
| F09 | Content-Length uses string len not byte len | `handler.py:338-345` | open |
| F10 | Duplicate `_send_error` in `handler.py` and `forwarder.py` | `handler.py:353`, `forwarder.py:173` | open |
| F13 | `configure_logging` not idempotent across level changes | `logger.py:33-48` | open |
| F17 | `open_browser_tab` error path untested | `tests/test_browse.py` | open |
| F19 | DNS leak test doesn't document browse.py exclusion | `tests/test_dns_leak.py` | open |
| F20 | Upstream hostname logged at INFO level | `upstream.py:153-158` | open |

#### Doc findings

| ID | Finding | File | Status |
|----|---------|------|--------|
| D01 | README CLI table omits `profile-clean` command | `README.md:50-58` | open |
| D02 | CLAUDE.md test count stale (245 vs 276) | `CLAUDE.md:10` | open |
| D03 | CLAUDE.md `__init__.py` description omits browser API exports | `CLAUDE.md:47` | open |
| D05 | Chromium candidate list in docs incomplete (missing -stable variants) | `docs/reference.md:226,318` | open |
| D08 | Flow diagram omits `profile-clean` | `docs/diagrams/proxy-relay-flow.md` | open |
| D10 | Diagram shows monitoring on HTTP forwarding path but code doesn't | `docs/diagrams/proxy-relay-flow.md:57` | open |
| D11 | `__version__` not mentioned in CLAUDE.md | `CLAUDE.md` | open |
