# Plan B: Capture Lifecycle (session_id, DB rotation, purge, reconnect backoff)

## Goal
Add session identity tracking, database rotation, size-based purging, and configurable
CDP reconnect backoff to the capture subsystem.
Implements F-RL18, F-RL20, F-RL21, F-RL23 (4 items).

F-RL18 was moved here from Theme A because it touches `capture/__init__.py` and
`capture/models.py`, which are already owned by this plan.

## Files touched
- `proxy_relay/capture/__init__.py` — F-RL18: replace hardcoded `max_reconnects=50` and `reconnect_delay=2.0` with `CaptureConfig` fields + exponential backoff; F-RL20: generate UUID `session_id` in `start()`, pass to `CaptureCollector`; F-RL21: rotate existing DB before opening new session; F-RL23: enforce size cap and age-based purge on session start
- `proxy_relay/capture/models.py` — F-RL18: add `max_cdp_reconnects`, `cdp_reconnect_delay_s`, `cdp_reconnect_backoff_factor` fields to `CaptureConfig`; F-RL20: no changes needed (session_id is runtime, not config); F-RL21: add `rotate_db: bool` field (default True); F-RL23: add `max_db_size_mb: int` and `max_db_age_days: int` fields
- `proxy_relay/capture/schema.py` — F-RL20: add `session_id` column to all 6 table schemas
- `proxy_relay/capture/collector.py` — F-RL20: accept `session_id` param in `__init__`, include in every emitted event payload
- `tests/unit/test_capture_session.py` — new tests for F-RL18 (backoff config), F-RL20 (session_id propagation), F-RL21 (DB rotation), F-RL23 (size cap)
- `tests/unit/test_capture_models.py` — new tests for new `CaptureConfig` fields
- `tests/unit/test_capture_schema.py` — verify `session_id` column in all tables
- `tests/unit/test_collector.py` — verify `session_id` in all emitted payloads

## Implementation steps

### F-RL18: Configurable CDP reconnect with exponential backoff
1. Add to `CaptureConfig` in `models.py`:
   - `max_cdp_reconnects: int = 50` (preserves current default)
   - `cdp_reconnect_delay_s: float = 2.0` (preserves current default)
   - `cdp_reconnect_backoff_factor: float = 1.5` (new: enables exponential backoff)
   - `cdp_reconnect_max_delay_s: float = 60.0` (new: caps the backoff)
2. In `capture/__init__.py`, replace `run_until_stopped()` hardcoded values:
   - `max_reconnects = self._config.max_cdp_reconnects`
   - Initial `reconnect_delay = self._config.cdp_reconnect_delay_s`
   - After each failed reconnect: `reconnect_delay = min(reconnect_delay * self._config.cdp_reconnect_backoff_factor, self._config.cdp_reconnect_max_delay_s)`
   - After each successful reconnect: reset `reconnect_delay = self._config.cdp_reconnect_delay_s`

### F-RL20: session_id in capture schema
1. In `schema.py`, add `ColumnDef("session_id")` to all 6 `TableSchema` definitions (http_requests, http_responses, cookies, storage_snapshots, websocket_frames, page_navigations).
2. In `collector.py`, add `session_id: str = ""` parameter to `__init__`. Store as `self._session_id`.
3. In every `on_*` method, add `"session_id": self._session_id` to each payload dict.
4. In `capture/__init__.py` `start()`:
   - `import uuid`
   - `self._session_id = str(uuid.uuid4())`
   - Pass `session_id=self._session_id` to `CaptureCollector(...)` constructor.
   - Log the session_id at INFO level.

### F-RL21: DB rotation on session start
1. In `capture/__init__.py` `start()`, before opening the DB:
   - Check if `self._config.rotate_db` is True (default).
   - If the DB file already exists, rename it to `capture-{ISO-timestamp}.db`.
   - Use `datetime.now(UTC).strftime("%Y%m%dT%H%M%S")` for the timestamp.
   - Log the rotation.
2. Add `rotate_db: bool = True` to `CaptureConfig` in `models.py`.

### F-RL23: DB size cap and age-based purge
1. Add to `CaptureConfig` in `models.py`:
   - `max_db_size_mb: int = 500` (default: 500 MB total across all capture DBs)
   - `max_db_age_days: int = 30` (default: purge DBs older than 30 days)
2. In `capture/__init__.py` `start()`, after DB rotation but before opening new DB:
   - Call `_purge_old_dbs()` — a new private method.
   - `_purge_old_dbs()` scans the capture directory for `capture-*.db` files.
   - Delete any older than `max_db_age_days`.
   - If total size of remaining DBs exceeds `max_db_size_mb`, delete oldest first until under limit.
   - Log each deletion.

## Acceptance criteria
- [ ] CDP reconnect uses exponential backoff with configurable max delay
- [ ] Reconnect delay resets on successful reconnection
- [ ] All 6 capture tables have `session_id` column
- [ ] Every emitted event includes `session_id` field
- [ ] Session start logs the UUID session_id
- [ ] Existing capture.db is rotated to timestamped name on new session start
- [ ] Rotated DBs older than `max_db_age_days` are purged
- [ ] Rotated DBs exceeding `max_db_size_mb` total are purged (oldest first)
- [ ] All existing tests pass
- [ ] New tests cover session_id propagation, DB rotation, purge logic, backoff config

## Dependencies
- None (Plan A does not touch any capture/ files)
