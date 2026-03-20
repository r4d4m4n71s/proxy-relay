"""Optional telemetry for proxy-relay warmup and browse sessions.

Writes lifecycle events to ~/.config/proxy-relay/telemetry.db when
telemetry-monitor is installed.  All public functions are no-ops when
the package is absent — callers never need to guard with try/except.

Tables
------
- warmup_events   — one row per warmup lifecycle event (start/complete/failed/poisoned)
- browse_events   — one row per browse session start / validation outcome
- rule_results    — one row per rule evaluated during a browse session
- remediations    — one row per remediation action executed

All tables share a ``run_id`` UUID that links related rows within a single
browse session or warmup run.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_writer: Any = None
_writer_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _build_schema() -> Any:
    """Build the SchemaDefinition for warmup/browse telemetry."""
    from telemetry_monitor.schema import ColumnDef, EventRoute, SchemaDefinition, TableSchema

    return SchemaDefinition(
        tables=[
            TableSchema(
                name="warmup_events",
                columns=[
                    ColumnDef("profile"),
                    ColumnDef("run_id"),
                    ColumnDef("event_type"),
                    ColumnDef("exit_ip"),
                    ColumnDef("country"),
                    ColumnDef("lang"),
                    ColumnDef("timezone"),
                    ColumnDef("elapsed_s", sql_type="REAL"),
                    ColumnDef("reason"),
                    ColumnDef("account_email"),
                ],
                indexes=["timestamp", "profile", "event_type"],
            ),
            TableSchema(
                name="browse_events",
                columns=[
                    ColumnDef("profile"),
                    ColumnDef("run_id"),
                    ColumnDef("event_type"),
                    ColumnDef("exit_ip"),
                    ColumnDef("country"),
                    ColumnDef("lang"),
                    ColumnDef("timezone"),
                    ColumnDef("url"),
                ],
                indexes=["timestamp", "profile", "event_type"],
            ),
            TableSchema(
                name="rule_results",
                columns=[
                    ColumnDef("profile"),
                    ColumnDef("run_id"),
                    ColumnDef("rule_name"),
                    ColumnDef("passed", sql_type="INTEGER"),
                    ColumnDef("skipped", sql_type="INTEGER"),
                    ColumnDef("reason"),
                    ColumnDef("remediation"),
                    ColumnDef("exit_ip"),
                ],
                indexes=["timestamp", "profile", "rule_name"],
            ),
            TableSchema(
                name="remediations",
                columns=[
                    ColumnDef("profile"),
                    ColumnDef("run_id"),
                    ColumnDef("action"),
                    ColumnDef("old_ip"),
                    ColumnDef("new_ip"),
                ],
                indexes=["timestamp", "profile", "action"],
            ),
        ],
        routes=[
            EventRoute(prefix="warmup.", table="warmup_events"),
            EventRoute(prefix="browse.", table="browse_events"),
            EventRoute(prefix="rule.", table="rule_results"),
            EventRoute(prefix="remediation.", table="remediations"),
        ],
    )


# ---------------------------------------------------------------------------
# Writer lifecycle
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    return Path.home() / ".config" / "proxy-relay"


def _init_writer() -> Any | None:
    """Initialize BackgroundWriter writing to telemetry.db.

    Returns None (and logs a debug message) if telemetry_monitor is absent
    or if any setup step fails.
    """
    try:
        from telemetry_monitor.storage.sqlite import SqliteStore
        from telemetry_monitor.writer import BackgroundWriter
    except ImportError:
        log.debug("telemetry_monitor not installed — telemetry disabled")
        return None

    try:
        schema = _build_schema()
        db_path = _config_dir() / "telemetry.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        store = SqliteStore(db_path=db_path, schema=schema)
        store.connect()

        writer = BackgroundWriter(
            flush_interval_s=5.0,
            batch_size=50,
            sqlite_store=store,
            routes=schema.routes,
        )
        writer.start()
        log.debug("Telemetry writer started, db=%s", db_path)
        return writer
    except Exception as exc:
        log.debug("Telemetry init failed: %s", exc)
        return None


def _get_writer() -> Any | None:
    global _writer, _initialized
    if not _initialized:
        with _writer_lock:
            if not _initialized:
                _writer = _init_writer()
                _initialized = True
    return _writer


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit(name: str, **fields: Any) -> None:
    """Emit a telemetry event.  No-op if telemetry_monitor is not installed.

    Args:
        name: Event name used for routing (e.g. ``"warmup.complete"``).
        **fields: Column values for the target table row.
    """
    writer = _get_writer()
    if writer is None:
        return
    try:
        writer.enqueue(name, fields)
    except Exception as exc:
        log.debug("Telemetry emit error (%s): %s", name, exc)


def new_run_id() -> str:
    """Return a new UUID string to correlate events within one session."""
    return str(uuid.uuid4())
