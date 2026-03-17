# Plan C: Capture Report Quality (analyzer improvements)

## Goal
Improve the quality and usefulness of capture analysis reports by collapsing URL bloat,
fixing JSON key extraction, filtering CORS preflight noise, upgrading timing precision,
auditing fingerprint headers, and adding session filtering.
Implements F-RL13, F-RL14, F-RL15, F-RL16, F-RL17, F-RL22 (6 items).

All changes are confined to `analyzer.py` — the analysis module reads from the DB in
read-only mode and has no dependencies on `collector.py` or other capture modules.

## Files touched
- `proxy_relay/capture/analyzer.py` — all 6 items: URL collapsing, JSON key fix, OPTIONS filtering, millisecond timing, fingerprint header extraction, session_id filter
- `tests/unit/test_analyzer.py` — new tests for all 6 items

## Implementation steps

### F-RL13: URL bloat — collapse CDN/image URLs by pattern
1. In `_analyze_api_surface()`, before accumulating endpoints, normalize paths:
   - Collapse `resources.tidal.com` image paths: `/images/{id}/{w}x{h}.jpg` -> `/images/{id}/{WxH}.jpg` (one entry per pattern)
   - Collapse CDN segment URLs: paths matching `/{hash}/{segment-N}.m4s` or similar -> `/{hash}/{segments}` (N calls)
   - Use a helper `_normalize_path(domain: str, path: str) -> str` that applies pattern-based collapsing.
2. Also collapse query parameters from URLs before grouping — strip `?token=...` and other per-request params that create unique URLs.
3. The endpoint key becomes `f"{domain}|{normalized_path}|{method}"`.

### F-RL14: JSON keys always empty in report
1. **Root cause analysis**: In `_analyze_api_surface()`, `json_keys` are only populated when `verbose=True` (line 408). Additionally, the body value from the DB may be empty or truncated. Check:
   - The `body` column in `http_responses` stores the response body set by `collector.on_response()`.
   - The collector stores `body_truncated` which is `_truncate(body or "", max_body_bytes)`.
   - The issue may be that bodies are not being fetched for many responses (the `_make_response_handler` only fetches for `should_capture_body(mime_type)` and `collector.matches_domain(url)`).
2. **Fix in analyzer**: Even when `verbose=False`, extract JSON keys for the first response of each endpoint (lightweight — just the top-level key set). This provides useful API surface mapping without requiring `--verbose`.
3. Move JSON key extraction from the accumulation loop to a post-processing step: for each endpoint, fetch one sample body from the DB and extract keys. This avoids parsing every response body during accumulation.
4. **IMPORTANT**: If bodies are genuinely empty in the DB (collector issue), note this as a blocker for Plan B to address in `collector.py`. The analyzer can only work with what's in the DB.

### F-RL15: OPTIONS preflight noise
1. In `_analyze_api_surface()`, filter out rows where `method == "OPTIONS"` from the main endpoint listing.
2. Add a separate summary line in the report: "Filtered N CORS preflight (OPTIONS) requests".
3. In `AnalysisReport`, add `options_filtered: int = 0` field to track filtered count.
4. In `print_report()` and `write_report()`, include the filtered count.

### F-RL16: Millisecond timing precision in analysis
1. In `_analyze_behavior()`, the inter-request gap calculation uses `datetime.fromisoformat()` which already preserves sub-second precision IF the stored timestamps have it.
2. The timestamps come from telemetry_monitor's BackgroundWriter, which uses `datetime.now().isoformat()` — this includes microseconds.
3. The issue is in the report output: `gap_p50` and `gap_p95` are printed with only 2 decimal places (line 222). For sub-second gaps, show milliseconds: `{gap * 1000:.0f}ms` when gap < 1.0s.
4. In `_session_duration()`, verify we parse timestamps with full precision.
5. In `_analyze_rate_limits()`, the SQLite `datetime()` function may truncate — verify and fix if needed.

### F-RL17: Missing fingerprint headers in audit
1. In `_FINGERPRINT_HEADERS`, `accept-language` and `accept-encoding` are already listed (line 41). The issue is that these headers may not be present in the stored `headers` string because the collector's `_redact_headers()` or `_headers_to_str()` drops them.
2. **Check**: `_redact_headers` only redacts headers in `redact_names` (authorization, cookie, etc.). `accept-language` and `accept-encoding` are NOT in the redact list, so they should pass through.
3. **Real issue**: The `_parse_headers()` function in `analyzer.py` uses `": "` as the delimiter (line 619). If a header is stored as `Accept-Encoding:gzip` (no space after colon), it won't be parsed. Fix: split on `:` and strip both key and value, handling the case where no space follows the colon.
4. Also ensure case-insensitive matching: `_FINGERPRINT_HEADERS` contains lowercase names, and `_parse_headers` preserves original case. The comparison at line 485 lowercases: `lower_name = name.lower()`. This should work. Verify in a test.

### F-RL22: Session filter in analyzer
1. Add optional `session_id: str | None = None` parameter to `analyze()`.
2. If `session_id` is provided, add `WHERE session_id = ?` clause to all queries.
3. This requires the `session_id` column to exist in the DB (added by Plan B). If the column doesn't exist (old DB), gracefully skip the filter and log a warning.
4. Add `--session` CLI argument to the `analyze` subparser in `cli.py`.

**IMPORTANT**: F-RL22 adds a `--session` flag to `cli.py`, which is also touched by Plan A (F-RL19, F-RL26). However, Plan A touches `_cmd_browse()`, `_cmd_status()`, and `build_parser()` for status/browse subcommands. Plan C only adds a flag to the `analyze` subparser and modifies `_cmd_analyze()`. The sections don't overlap: Plan A works on browse/status commands, Plan C on the analyze command. **However**, since both modify `cli.py`, we must assign it to only one plan.

**Resolution**: F-RL22's CLI change (`--session` on analyze) is moved to Plan A's `cli.py` scope. Plan C implements the `analyze()` function's session_id filtering in `analyzer.py`. Plan A passes the `--session` arg through to `analyze()`. This keeps `cli.py` in Plan A only.

Actually, re-reading the rules: `cli.py` is already in Plan A. Plan C MUST NOT touch it. So:
- Plan A adds the `--session` arg to `analyze` subparser and passes it to `analyze()`.
- Plan C adds the `session_id` parameter to `analyze()` and implements the filtering logic.
- This is a clean split: Plan A owns CLI surface, Plan C owns analyzer internals.

## Acceptance criteria
- [ ] CDN/image URLs are collapsed by pattern in API surface analysis
- [ ] JSON keys are extracted for API endpoints (at least one sample per endpoint)
- [ ] OPTIONS preflight requests are filtered from the main endpoint listing with a count summary
- [ ] Sub-second timing gaps are displayed in milliseconds
- [ ] `accept-language` and `accept-encoding` appear in fingerprint audit when present
- [ ] `analyze()` accepts optional `session_id` parameter for filtering
- [ ] Session filter gracefully handles DBs without `session_id` column
- [ ] All existing tests pass
- [ ] New tests cover URL collapsing, JSON key extraction, OPTIONS filtering, timing precision, header parsing, session filtering

## Dependencies
- F-RL22 session filter depends on Plan B adding `session_id` column to the DB schema. However, the analyzer gracefully handles missing columns, so Plan C can be implemented and tested independently.
- F-RL22 CLI flag (`--session`) is implemented by Plan A in `cli.py`.
