"""Tests for proxy_relay.capture.schema — PROXY_RELAY_SCHEMA telemetry schema."""
from __future__ import annotations

import pytest

telemetry_monitor = pytest.importorskip(
    "telemetry_monitor",
    reason="telemetry-monitor not installed; skipping schema tests",
)


class TestSchemaStructure:
    """Verify PROXY_RELAY_SCHEMA has the correct tables and routes."""

    @pytest.fixture(autouse=True)
    def schema(self):
        from proxy_relay.capture.schema import PROXY_RELAY_SCHEMA

        self._schema = PROXY_RELAY_SCHEMA
        return PROXY_RELAY_SCHEMA

    def test_schema_has_five_tables(self):
        assert len(self._schema.tables) == 5, (
            f"Expected 5 tables, got {len(self._schema.tables)}: "
            f"{[t.name for t in self._schema.tables]}"
        )

    def test_schema_table_names(self):
        names = {t.name for t in self._schema.tables}
        expected = {
            "http_requests",
            "http_responses",
            "cookies",
            "storage_snapshots",
            "websocket_frames",
        }
        assert names == expected, f"Table names mismatch. Got: {names}"

    def test_schema_has_five_routes(self):
        assert len(self._schema.routes) == 5, (
            f"Expected 5 routes, got {len(self._schema.routes)}"
        )

    def test_schema_route_prefixes(self):
        prefixes = {r.prefix for r in self._schema.routes}
        assert "http.request." in prefixes
        assert "http.response." in prefixes
        assert "cookie." in prefixes
        assert "storage." in prefixes
        assert "ws." in prefixes

    def test_schema_validates(self):
        """PROXY_RELAY_SCHEMA.validate() must succeed without raising SchemaError."""
        from telemetry_monitor.schema import SchemaError

        try:
            self._schema.validate()
        except SchemaError as exc:
            pytest.fail(f"Schema validation failed: {exc}")

    def test_schema_dashboards_count(self):
        """Schema must define at least 10 dashboards."""
        assert len(self._schema.dashboards) >= 10, (
            f"Expected at least 10 dashboards, got {len(self._schema.dashboards)}"
        )

    def test_all_routes_reference_valid_tables(self):
        """Every route's table name must exist in the schema tables."""
        table_names = {t.name for t in self._schema.tables}
        for route in self._schema.routes:
            assert route.table in table_names, (
                f"Route prefix={route.prefix!r} references unknown table {route.table!r}"
            )


class TestSchemaColumns:
    """Verify expected columns exist in each table."""

    @pytest.fixture(autouse=True)
    def schema(self):
        from proxy_relay.capture.schema import PROXY_RELAY_SCHEMA

        self._tables = {t.name: t for t in PROXY_RELAY_SCHEMA.tables}
        return PROXY_RELAY_SCHEMA

    def _column_names(self, table_name: str) -> set[str]:
        table = self._tables[table_name]
        return {col.name for col in table.columns}

    def test_http_requests_columns(self):
        cols = self._column_names("http_requests")
        required = {"request_id", "url", "method", "domain", "path", "headers"}
        missing = required - cols
        assert not missing, f"http_requests missing columns: {missing}"

    def test_http_responses_columns(self):
        cols = self._column_names("http_responses")
        required = {"request_id", "url", "status_code", "headers", "response_ms"}
        missing = required - cols
        assert not missing, f"http_responses missing columns: {missing}"

    def test_cookies_columns(self):
        cols = self._column_names("cookies")
        required = {"domain", "name", "value", "http_only", "secure", "event_type"}
        missing = required - cols
        assert not missing, f"cookies missing columns: {missing}"

    def test_storage_snapshots_columns(self):
        cols = self._column_names("storage_snapshots")
        required = {"origin", "storage_type", "key", "value", "event_type"}
        missing = required - cols
        assert not missing, f"storage_snapshots missing columns: {missing}"

    def test_websocket_frames_columns(self):
        cols = self._column_names("websocket_frames")
        required = {"url", "direction", "payload_preview", "payload_size_bytes"}
        missing = required - cols
        assert not missing, f"websocket_frames missing columns: {missing}"
