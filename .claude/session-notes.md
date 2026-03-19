# Session Notes

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

---

## 2026-03-18 — S82: code fixes + stale verification

**Branch:** `main`

### Accomplished
| File/Area | Change |
|-----------|--------|
| `pyproject.toml` | F02: removed unused `tomlkit>=0.13.0` from runtime deps |
| `proxy_relay/upstream.py` | F20: demoted upstream hostname log from INFO → DEBUG |
| `tests/unit/test_dns_leak.py` | F19: added comment explaining browse.py exclusion |
| `tests/unit/test_browse.py` | F17: added `test_oserror_does_not_propagate` for `open_browser_tab` |
| `docs/architecture-review.md` | Rewrote: 7 remaining open items targeting S83; 12 stale findings removed |
| `docs/architecture-review-done.md` | Created with S82 batch (16 items: 4 fixed, 12 verified stale) |

**Test count:** 651 tests, 0 failures.

### Current state
- 4 findings fixed in S82; 12 stale findings verified already fixed in prior sprints.
- 7 docs/rename items remained → all resolved in S83.

### Next steps
1. S83 docs sweep (completed same session).

> Running log of sprint sessions. Most recent entry first. Rolling window: 5 entries max.
