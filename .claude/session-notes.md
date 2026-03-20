# Session Notes

## 2026-03-20 — WidevineCdm auto-seed + daemon venv migration

**Branch:** `main`

### Accomplished
| File/Area | Change |
|-----------|--------|
| `proxy_relay/browse.py` | `_seed_widevine()`: copies WidevineCdm from snap Chromium dir into new profiles at creation; fallback to sibling profiles; non-fatal try/except |
| `~/.venv/tidal-dl-daemon/` | New isolated daemon venv with editable installs of all 4 projects (replaces pipx) |
| `~/.config/systemd/user/tidal-dl-daemon.service` | ExecStart updated to full daemon venv path |

**Test count:** 729 tests, 0 failures.

### Current state
- New browser profiles get Widevine on first launch — no manual browser restart needed.
- Daemon fully isolated in plain venv, no pipx dependency.

### Next steps
1. Next architecture review when significant new code lands.

---

## 2026-03-19 — Warmup SQLite rewrite, profile validation, telemetry

**Branch:** `main`

### Accomplished
| File/Area | Change |
|-----------|--------|
| `proxy_relay/warmup.py` | CDP removed; SQLite cookie polling; exit IP via health_check(); emit warmup telemetry events |
| `proxy_relay/profile_rules.py` | 5 rules (removed `ip_matches_cookie` — not IP-bound); `ProfileNotPoisoned` + `write_poisoned_marker()`; fixed remediation values |
| `proxy_relay/telemetry.py` | New: lazy BackgroundWriter → `telemetry.db`; 4 tables; `emit()` no-op when telemetry_monitor absent |
| `proxy_relay/cli.py` | Removed `--workspace`; telemetry wiring (browse.start, rule.evaluated, remediation.executed) |
| `tests/unit/test_*` | Updated rule counts (6→5), removed TestIPMatchesCookieRule, added TestPollForDatadome |

**Test count:** 729 tests, 0 failures.

### Current state
- Warmup runs clean (no CDP, no automation signal to DataDome)
- Profile validation: 5 rules, telemetry written on every browse session start
- `~/.config/proxy-relay/telemetry.db` written when telemetry_monitor installed

### Next steps
1. Deploy to pipx + smoke test telemetry.db

---

## 2026-03-18 — S83: docs sweep + F21 rename

**Branch:** `main`

### Accomplished
| File/Area | Change |
|-----------|--------|
| `proxy_relay/config.py` | F21: renamed `proxy_st_profile` → `default_proxy_profile` (field, TOML key, validation, log) |
| `proxy_relay/cli.py` | F21: updated 2 `config.proxy_st_profile` references |
| `tests/unit/test_config.py` | F21: updated TOML string and 2 assertion references |
| `docs/reference.md` | F21: updated 3 occurrences; D04: added Python Library API section; D05: added -stable browser variants |
| `README.md` | D01: added `profile-clean` and `analyze` rows to CLI table |
| `.claude/CLAUDE.md` | D03/D11: expanded `__init__.py` description to list all 17 exports incl. `__version__`; removed `tomlkit` from stack line |
| `docs/diagrams/proxy-relay-flow.md` | D08: added Profile Maintenance subgraph |
| `docs/architecture-review.md` | Marked all 7 S83 findings resolved; no active findings remain |
| `docs/architecture-review-done.md` | Prepended S83 batch (7 items) |

**Test count:** 651 tests, 0 failures.

### Current state
- All architecture review findings resolved (S82 + S83 closed out everything).
- No active findings in `docs/architecture-review.md`.

### Next steps
1. Next architecture review when significant new code lands.

> Running log of sprint sessions. Most recent entry first. Rolling window: 5 entries max.
