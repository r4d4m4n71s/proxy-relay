# Architecture Review — proxy-relay

**Last review:** 2026-03-16 (Batch G)
**Prior review:** 2026-03-14 (31 findings: 4 done, 27 open — pre-existing doc/code items)
**Batch G:** 15 findings (1 high, 6 medium, 8 low) — all resolved 2026-03-16.
**S82 (2026-03-18):** 16 findings verified done (12 already fixed, 4 resolved in sprint). 11 remain open.

Completed items in `docs/architecture-review-done.md`.

## Active Findings

### HIGH

| ID | Finding | File | Status |
|----|---------|------|--------|
| D04 | No Python library usage documentation — tidal-dl consumes 15 symbols with no usage docs | `README.md`, `docs/reference.md` | open → S83 |

### LOW — code

| ID | Finding | File | Status |
|----|---------|------|--------|
| F21 | `proxy_st_profile` config key leaks provider name — rename to `default_proxy_profile` (hard rename, no shim needed — private single-user project) | `config.py`, `cli.py`, `docs/reference.md`, `tests/unit/test_config.py` | backlog → S83 |

### LOW — docs

| ID | Finding | File | Status |
|----|---------|------|--------|
| D01 | README CLI table omits `profile-clean` and `analyze` commands | `README.md` | open → S83 |
| D03 | CLAUDE.md `__init__.py` description omits Browser API + Timezone exports | `.claude/CLAUDE.md` | open → S83 |
| D05 | Chromium candidate list in docs incomplete (missing -stable variants) | `docs/reference.md` | open → S83 |
| D08 | Flow diagram omits `profile-clean` | `docs/diagrams/proxy-relay-flow.md` | open → S83 |
| D11 | `__version__` not mentioned in CLAUDE.md | `.claude/CLAUDE.md` | open → S83 |
