# Architecture Review — proxy-relay — Completed Items

---

## S83 — 2026-03-18 (docs sweep + F21 rename)

7 items resolved.

| ID | Finding | Resolution |
|----|---------|-----------|
| F21 | `proxy_st_profile` config key leaks provider name | Hard rename to `default_proxy_profile` in `config.py`, `cli.py`, `docs/reference.md`, `tests/unit/test_config.py` |
| D01 | README CLI table omits `profile-clean` and `analyze` commands | Added both rows to CLI reference table in `README.md` |
| D03 | CLAUDE.md `__init__.py` description omits Browser API + Timezone exports | Updated module map entry to list all 17 exported symbols |
| D04 | No Python library usage documentation | Added "Python Library API" section to `docs/reference.md` with tables for all 17 public symbols |
| D05 | Chromium candidate list in docs incomplete (missing -stable variants) | Added `-stable` variants for chromium, google-chrome, brave-browser, microsoft-edge, vivaldi |
| D08 | Flow diagram omits `profile-clean` | Added "Profile Maintenance" subgraph to `docs/diagrams/proxy-relay-flow.md` |
| D11 | `__version__` not mentioned in CLAUDE.md | Added to `__init__.py` description in module map |

---

## S82 — 2026-03-18 (code fixes + stale verification)

16 items resolved: 4 fixed in S82, 12 verified already fixed (stale).

### Fixed in S82

| ID | Finding | Resolution |
|----|---------|-----------|
| F02 | `tomlkit` declared as runtime dep but never imported | Removed from `pyproject.toml` dependencies |
| F17 | `open_browser_tab` error path untested | Added `test_oserror_does_not_propagate` in `test_browse.py` |
| F19 | DNS leak test doesn't document browse.py exclusion | Added explanatory comment to `test_dns_leak.py` |
| F20 | Upstream hostname logged at INFO level | Demoted to DEBUG in `upstream.py` |

### Verified already fixed (stale — resolved in prior sprints)

| ID | Finding | How resolved |
|----|---------|-------------|
| F01 | `__main__.py` missing `from __future__ import annotations` | Added in prior sprint |
| F05 | `open_browser_tab` does not catch OSError | Fixed in prior sprint (`browse.py:268`) |
| F07 | Double UpstreamManager creation in `_cmd_start` | Fixed in prior sprint (single creation in `_run()`) |
| F08 | `health_check` uses `urlopen` respecting env proxy vars | Fixed — now uses `ProxyHandler({})` to bypass env proxies |
| F09 | Content-Length uses string len not byte len | Fixed — extracted to `response.py`, uses `len(body_bytes)` |
| F10 | Duplicate `_send_error` in `handler.py` and `forwarder.py` | Fixed — extracted to shared `response.py` |
| F12 | `forward_http_request` reads entire response into memory | Fixed — response now streamed in 8 KiB chunks (100 MiB cap) |
| F13 | `configure_logging` not idempotent across level changes | Fixed — uses `_CONFIGURE_LOCK`, updates level on re-entry |
| F14 | No warning when `server.host` binds to non-loopback | Fixed — warning added at `config.py:199-204` |
| F18 | No Content-Length body size limit in `_handle_http` | Fixed — `_MAX_BODY_SIZE` enforced with 413 response |
| D02 | CLAUDE.md test count stale | Fixed — CLAUDE.md shows correct count (650) |
| D10 | Diagram shows monitoring on HTTP forwarding path but code doesn't | Fixed — code now monitors HTTP forwarding path, diagram matches |

---

## Batch G — 2026-03-16

15 findings (1 high, 6 medium, 8 low) — all resolved same session.
See tidal-dl `docs/architecture-review-done.md` for full Batch G table.

---

## Prior sprints (pre-Batch G)

| ID | Finding | Sprint |
|----|---------|--------|
| F04 | Profile name unsanitized in PID/status paths — path traversal via --profile | S75 |
| D06 | `browse` command never calls `configure_logging()` | S75 |
| D07 | `_do_rotate()` does not update status file | S75 |
| D09 | tidal-dl imports `proxy_relay.tz` outside public API | S80 |
