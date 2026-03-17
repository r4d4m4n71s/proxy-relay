"""proxy-relay telemetry schema — registered as a telemetry-monitor schema entry point.

This module-level ``PROXY_RELAY_SCHEMA`` object is loaded by the telemetry-monitor
entry point machinery.  If ``telemetry_monitor`` is not installed, the
``ImportError`` raised during module load is caught by the entry point loader,
which is the expected behaviour for an optional dependency.

CaptureSession must NEVER import this module at the top of capture/__init__.py
— always import it lazily inside methods.
"""
from __future__ import annotations

from telemetry_monitor.schema import (
    ColumnDef,
    EventRoute,
    SchemaDefinition,
    TableSchema,
)


def _build_schema() -> SchemaDefinition:
    """Construct and return the PROXY_RELAY_SCHEMA definition."""
    return SchemaDefinition(
        tables=[
            TableSchema(
                name="http_requests",
                columns=[
                    ColumnDef("request_id"),
                    ColumnDef("url"),
                    ColumnDef("method"),
                    ColumnDef("headers"),
                    ColumnDef("post_data"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "url", "method"],
            ),
            TableSchema(
                name="http_responses",
                columns=[
                    ColumnDef("request_id"),
                    ColumnDef("url"),
                    ColumnDef("status", sql_type="INTEGER"),
                    ColumnDef("mime_type"),
                    ColumnDef("headers"),
                    ColumnDef("body"),
                    ColumnDef("response_ms", sql_type="INTEGER"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "url", "status"],
            ),
            TableSchema(
                name="cookies",
                columns=[
                    ColumnDef("domain"),
                    ColumnDef("name"),
                    ColumnDef("value"),
                    ColumnDef("http_only", sql_type="INTEGER"),
                    ColumnDef("secure", sql_type="INTEGER"),
                    ColumnDef("expires", sql_type="REAL"),
                    ColumnDef("path"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "domain", "name"],
            ),
            TableSchema(
                name="storage_snapshots",
                columns=[
                    ColumnDef("origin"),
                    ColumnDef("storage_type"),
                    ColumnDef("key"),
                    ColumnDef("value"),
                    ColumnDef("change_type"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "origin", "key"],
            ),
            TableSchema(
                name="websocket_frames",
                columns=[
                    ColumnDef("request_id"),
                    ColumnDef("url"),
                    ColumnDef("direction"),
                    ColumnDef("payload"),
                    ColumnDef("opcode", sql_type="INTEGER"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "url", "direction"],
            ),
            TableSchema(
                name="page_navigations",
                columns=[
                    ColumnDef("url"),
                    ColumnDef("frame_id"),
                    ColumnDef("transition_type"),
                    ColumnDef("mime_type"),
                    ColumnDef("profile"),
                    ColumnDef("session_id"),
                ],
                indexes=["timestamp", "url"],
            ),
        ],
        routes=[
            EventRoute(prefix="http.request.", table="http_requests", batch=True),
            EventRoute(prefix="http.response.", table="http_responses", batch=True),
            EventRoute(prefix="cookie.", table="cookies", batch=True),
            EventRoute(prefix="storage.", table="storage_snapshots", batch=True),
            EventRoute(prefix="ws.", table="websocket_frames", batch=True),
            EventRoute(prefix="page.", table="page_navigations", batch=True),
        ],
        dashboards={
            "requests_by_domain": (
                "SELECT substr(url, instr(url, '://') + 3,"
                " instr(substr(url, instr(url, '://') + 3), '/') - 1) AS domain,"
                " count(*) AS total"
                " FROM http_requests GROUP BY domain ORDER BY total DESC"
            ),
            "api_endpoints": (
                "SELECT url, method, count(*) AS calls"
                " FROM http_requests GROUP BY url, method ORDER BY calls DESC LIMIT 50"
            ),
            "response_codes": (
                "SELECT status, count(*) AS count"
                " FROM http_responses GROUP BY status ORDER BY status"
            ),
            "cookie_inventory": (
                "SELECT domain, name, count(*) AS snapshots"
                " FROM cookies GROUP BY domain, name ORDER BY domain, name"
            ),
            "cookie_lifecycle": (
                "SELECT timestamp, domain, name, value"
                " FROM cookies ORDER BY timestamp DESC LIMIT 100"
            ),
            "storage_keys": (
                "SELECT origin, storage_type, key, count(*) AS changes"
                " FROM storage_snapshots GROUP BY origin, storage_type, key ORDER BY changes DESC"
            ),
            "auth_token_flow": (
                "SELECT timestamp, url, method"
                " FROM http_requests"
                " WHERE lower(url) LIKE '%/token%' OR lower(url) LIKE '%/oauth%'"
                " OR lower(url) LIKE '%/auth%'"
                " ORDER BY timestamp DESC LIMIT 50"
            ),
            "slow_api_calls": (
                "SELECT url, status, response_ms, timestamp"
                " FROM http_responses WHERE response_ms > 2000"
                " ORDER BY response_ms DESC LIMIT 50"
            ),
            "websocket_activity": (
                "SELECT url, direction, count(*) AS frames"
                " FROM websocket_frames GROUP BY url, direction ORDER BY frames DESC"
            ),
            "session_timeline": (
                "SELECT timestamp,"
                " CASE"
                "  WHEN url LIKE '%http_requests%' THEN 'request'"
                "  WHEN url LIKE '%http_responses%' THEN 'response'"
                "  ELSE 'other' END AS event_type,"
                " url"
                " FROM http_requests"
                " UNION ALL"
                " SELECT timestamp, 'response', url FROM http_responses"
                " ORDER BY timestamp DESC LIMIT 200"
            ),
            "login_flow": (
                "SELECT timestamp, url, method, status"
                " FROM http_requests r"
                " LEFT JOIN http_responses s ON r.request_id = s.request_id"
                " WHERE lower(r.url) LIKE '%login%' OR lower(r.url) LIKE '%signin%'"
                " ORDER BY r.timestamp DESC LIMIT 20"
            ),
            "token_refresh_pattern": (
                "SELECT strftime('%H:%M', timestamp) AS time_bucket,"
                " count(*) AS refreshes"
                " FROM http_requests"
                " WHERE lower(url) LIKE '%refresh%' OR lower(url) LIKE '%token%'"
                " GROUP BY time_bucket ORDER BY time_bucket"
            ),
            "request_timing_distribution": (
                "SELECT"
                " CASE"
                "  WHEN response_ms < 100 THEN '<100ms'"
                "  WHEN response_ms < 500 THEN '100-500ms'"
                "  WHEN response_ms < 1000 THEN '500ms-1s'"
                "  WHEN response_ms < 2000 THEN '1-2s'"
                "  ELSE '>2s' END AS bucket,"
                " count(*) AS count"
                " FROM http_responses GROUP BY bucket ORDER BY bucket"
            ),
            "inter_request_gaps": (
                "SELECT"
                " round((julianday(timestamp) - julianday(lag(timestamp)"
                "  OVER (ORDER BY timestamp))) * 86400, 2) AS gap_s,"
                " url"
                " FROM http_requests ORDER BY timestamp DESC LIMIT 100"
            ),
            "fingerprint_signals": (
                "SELECT url, headers"
                " FROM http_requests"
                " WHERE lower(headers) LIKE '%user-agent%'"
                " OR lower(headers) LIKE '%x-tidal%'"
                " ORDER BY timestamp DESC LIMIT 50"
            ),
            "page_navigation_history": (
                "SELECT timestamp, url, transition_type, frame_id"
                " FROM page_navigations ORDER BY timestamp DESC LIMIT 100"
            ),
            "playback_events": (
                "SELECT timestamp, url, method"
                " FROM http_requests"
                " WHERE lower(url) LIKE '%playback%' OR lower(url) LIKE '%stream%'"
                " OR lower(url) LIKE '%manifest%'"
                " ORDER BY timestamp DESC LIMIT 100"
            ),
        },
    )


PROXY_RELAY_SCHEMA: SchemaDefinition = _build_schema()
