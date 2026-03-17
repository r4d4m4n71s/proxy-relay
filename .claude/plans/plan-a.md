# Plan A: Status & Lifecycle (CLI + pidfile + server)

## Goal
Improve server status reporting, multi-profile visibility, and lifecycle cleanup.
Implements F-RL19, F-RL24, F-RL25, F-RL26, F-RL27 (5 items).

F-RL18 (CDP reconnect backoff) moved to Plan B — it touches `capture/__init__.py` and
`capture/models.py` which are owned by Plan B.

## Files touched
- `proxy_relay/cli.py` — F-RL19: add `websockets` pre-check in `_cmd_browse` before capture setup; F-RL26: add `--all` flag to `status` subparser, implement `_cmd_status_all()` scanning `*.status.json` + PID validation
- `proxy_relay/pidfile.py` — F-RL24: add `pid`, `started_at`, `last_updated` fields to `write_status()`; F-RL25: register atexit cleanup for status files in `write_status()`; F-RL26: add `scan_all_status()` function returning list of live profile statuses; F-RL27: add `read_live_status()` combining `read_status()` + `is_process_running()`
- `proxy_relay/server.py` — F-RL24: pass new timestamp fields when calling `write_status()`, store `_started_at` on `start()`; F-RL25: register status file atexit in `start()`
- `tests/unit/test_cli.py` — new tests for F-RL19 (websockets pre-check), F-RL26 (`--all` flag)
- `tests/unit/test_pidfile.py` — new tests for F-RL24 (pid/timestamps in status), F-RL25 (atexit registration for status), F-RL26 (`scan_all_status`), F-RL27 (`read_live_status`)
- `tests/unit/test_server.py` — new tests for F-RL24 (started_at field), F-RL25 (atexit for status)

## Implementation steps

### F-RL19: `browse --capture` pre-check for websockets
1. In `_cmd_browse()`, before the existing `is_capture_available()` check at line 645, add an early check that runs at CLI parse time (before any server work). Actually, the current check at line 645 already does this — but the error message could be clearer. The item says "fails cryptically if `websockets` not installed". Review the current flow: `is_capture_available()` checks both `websockets` and `telemetry_monitor`. If False, it prints a message and returns 1. This already works. The fix is: move the check earlier (before server auto-start), so we don't start a server only to fail on missing deps. Currently the check is at line 641, which is AFTER the server auto-start block (line 576-596). Move the `--capture` dependency check to just after config loading (before the server start section).

### F-RL24: PID + timestamps in status file
1. Add `pid: int` parameter to `write_status()` in `pidfile.py` (default `os.getpid()`).
2. Add `started_at: str` parameter (ISO timestamp).
3. Add `last_updated: str` — always set to `datetime.now(UTC).isoformat()` inside `write_status()`.
4. Include all three in the JSON output.
5. In `server.py`, store `self._started_at = datetime.now(UTC).isoformat()` in `start()`.
6. Pass `pid=os.getpid()` and `started_at=self._started_at` to all `write_status()` calls via `_update_status_file()`.

### F-RL25: atexit cleanup for status files
1. In `pidfile.py`, extend the atexit pattern from `write_pid()` to `write_status()`: maintain a `_status_atexit_registered: set[Path]` and register `_remove_status_file(path)` on first write.
2. The `_remove_status_file` function calls `path.unlink(missing_ok=True)`.
3. In `server.py`, the `stop()` method already removes the status file (line 181). The atexit handler is a safety net for crashes/SIGKILL.

### F-RL26: `status --all` for multi-profile
1. Add `--all` flag to the `status` subparser in `build_parser()`.
2. In `pidfile.py`, add `scan_all_status(config_dir: Path = CONFIG_DIR) -> list[dict]`:
   - Glob `config_dir / "*.status.json"`
   - For each, extract profile name from filename
   - Read status, check if PID is alive, include in result
   - Clean up stale files (dead PID)
3. In `cli.py`, if `args.all` is set, call `scan_all_status()` and print an aggregate table (or JSON if `--json`).

### F-RL27: `read_live_status()` helper
1. Add `read_live_status(profile: str) -> dict | None` to `pidfile.py`:
   - Combines `read_status(status_path_for(profile))` with `is_process_running(read_pid(pid_path_for(profile)))`.
   - Returns the status dict augmented with `"running": True, "pid": N` if alive.
   - Returns `None` if not running or no status file.
2. Note: `read_status_if_alive()` already exists (line 300) and does something very similar. `read_live_status()` should be a simpler API that returns `dict | None` instead of a 3-tuple. Consider making it a thin wrapper over `read_status_if_alive()`.

## Acceptance criteria
- [ ] `browse --capture` fails immediately with clear error message if websockets not installed (before server auto-start)
- [ ] Status JSON contains `pid`, `started_at`, `last_updated` fields
- [ ] Status file is removed on atexit (even if server crashes without calling `stop()`)
- [ ] `proxy-relay status --all` scans all profiles, shows aggregate table
- [ ] `proxy-relay status --all --json` outputs JSON array of all live profiles
- [ ] `read_live_status()` returns combined status+liveness in a single call
- [ ] All existing tests pass
- [ ] New tests cover each F-RL item

## Dependencies
- None
