"""Tests for proxy_relay.monitor — ConnectionMonitor quality tracking."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from proxy_relay.config import MonitorConfig


def _make_monitor_config(
    *,
    enabled: bool = True,
    slow_threshold_ms: float = 2000.0,
    error_threshold_count: int = 5,
    window_size: int = 100,
) -> MonitorConfig:
    """Build a MonitorConfig with the given values."""
    return MonitorConfig(
        enabled=enabled,
        slow_threshold_ms=slow_threshold_ms,
        error_threshold_count=error_threshold_count,
        window_size=window_size,
    )


# ---------------------------------------------------------------------------
# ConnectionOutcome enum
# ---------------------------------------------------------------------------
class TestConnectionOutcome:
    """Verify ConnectionOutcome enum values exist."""

    def test_success_value(self):
        from proxy_relay.monitor import ConnectionOutcome

        assert hasattr(ConnectionOutcome, "SUCCESS")

    def test_tunnel_error_value(self):
        from proxy_relay.monitor import ConnectionOutcome

        assert hasattr(ConnectionOutcome, "TUNNEL_ERROR")

    def test_timeout_value(self):
        from proxy_relay.monitor import ConnectionOutcome

        assert hasattr(ConnectionOutcome, "TIMEOUT")

    def test_reset_value(self):
        from proxy_relay.monitor import ConnectionOutcome

        assert hasattr(ConnectionOutcome, "RESET")


# ---------------------------------------------------------------------------
# ConnectionRecord dataclass
# ---------------------------------------------------------------------------
class TestConnectionRecord:
    """Verify ConnectionRecord is frozen and has expected fields."""

    def test_record_is_frozen(self):
        from proxy_relay.monitor import ConnectionOutcome, ConnectionRecord

        record = ConnectionRecord(
            timestamp=1000.0,
            outcome=ConnectionOutcome.SUCCESS,
            latency_ms=42.0,
            target="example.com:443",
        )
        with pytest.raises(AttributeError):
            record.latency_ms = 99.0  # type: ignore[misc]

    def test_record_fields(self):
        from proxy_relay.monitor import ConnectionOutcome, ConnectionRecord

        record = ConnectionRecord(
            timestamp=1000.0,
            outcome=ConnectionOutcome.TUNNEL_ERROR,
            latency_ms=150.0,
            target="example.com:443",
            error_message="connection refused",
        )
        assert record.timestamp == 1000.0
        assert record.outcome == ConnectionOutcome.TUNNEL_ERROR
        assert record.latency_ms == 150.0
        assert record.target == "example.com:443"
        assert record.error_message == "connection refused"

    def test_record_default_error_message(self):
        from proxy_relay.monitor import ConnectionOutcome, ConnectionRecord

        record = ConnectionRecord(
            timestamp=1.0,
            outcome=ConnectionOutcome.SUCCESS,
            latency_ms=10.0,
            target="host:80",
        )
        assert record.error_message == ""


# ---------------------------------------------------------------------------
# ConnectionMonitor — recording successes
# ---------------------------------------------------------------------------
class TestMonitorRecordSuccess:
    """Test ConnectionMonitor.record_success behavior."""

    @pytest.mark.asyncio
    async def test_record_success_stores_in_window(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        await monitor.record_success(latency_ms=42.0, target="example.com:443")

        stats = monitor.get_stats()
        assert stats.total_connections >= 1

    @pytest.mark.asyncio
    async def test_record_success_no_errors(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        await monitor.record_success(latency_ms=10.0, target="host:80")

        stats = monitor.get_stats()
        assert stats.total_errors == 0

    @pytest.mark.asyncio
    async def test_disabled_monitor_is_noop(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config(enabled=False)
        monitor = ConnectionMonitor(cfg)

        await monitor.record_success(latency_ms=10.0, target="host:80")

        stats = monitor.get_stats()
        assert stats.total_connections == 0

    @pytest.mark.asyncio
    async def test_enabled_property(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg_on = _make_monitor_config(enabled=True)
        cfg_off = _make_monitor_config(enabled=False)

        assert ConnectionMonitor(cfg_on).enabled is True
        assert ConnectionMonitor(cfg_off).enabled is False


# ---------------------------------------------------------------------------
# ConnectionMonitor — recording errors
# ---------------------------------------------------------------------------
class TestMonitorRecordError:
    """Test ConnectionMonitor.record_error behavior."""

    @pytest.mark.asyncio
    async def test_record_error_increments_error_count(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=10)
        monitor = ConnectionMonitor(cfg)

        await monitor.record_error(
            ConnectionOutcome.TUNNEL_ERROR, target="host:443", error_message="refused"
        )

        stats = monitor.get_stats()
        assert stats.total_errors >= 1
        assert stats.window_error_count >= 1

    @pytest.mark.asyncio
    async def test_record_error_no_callback_does_not_crash(self):
        """Monitor with no rotate_callback should not crash on error."""
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=1)
        monitor = ConnectionMonitor(cfg)

        # Should not raise even though threshold is exceeded
        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

    @pytest.mark.asyncio
    async def test_disabled_monitor_error_is_noop(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(enabled=False)
        monitor = ConnectionMonitor(cfg)

        await monitor.record_error(ConnectionOutcome.TIMEOUT, target="host:443")

        stats = monitor.get_stats()
        assert stats.total_errors == 0


# ---------------------------------------------------------------------------
# ConnectionMonitor — error threshold triggers rotation
# ---------------------------------------------------------------------------
class TestMonitorAutoRotate:
    """Test that error threshold triggers rotate_callback."""

    @pytest.mark.asyncio
    async def test_error_threshold_triggers_callback(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=3)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        for _ in range(3):
            await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        callback.assert_called()

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_trigger_callback(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=5)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        for _ in range(4):
            await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_clears_after_rotation(self):
        """After rotation, the window error count resets."""
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=2)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        # Hit threshold -- triggers rotation
        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")
        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        callback.assert_called()

        # After rotation, window error count should be reset
        assert monitor.window_error_count == 0

    @pytest.mark.asyncio
    async def test_threshold_zero_triggers_on_every_error(self):
        """error_threshold_count=0 means trigger rotation on every single error.

        The implementation uses ``error_count >= threshold``, so with
        threshold=0 every error (count >= 1 >= 0) triggers rotation.
        After each rotation the window clears, so the next error triggers again.
        """
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=0)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")
        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        # Each error triggers rotation (window cleared after each)
        assert callback.call_count == 2

    @pytest.mark.asyncio
    async def test_threshold_one_triggers_on_first_error(self):
        """error_threshold_count=1 should trigger on the very first error."""
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=1)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        callback.assert_called_once()


# ---------------------------------------------------------------------------
# ConnectionMonitor — rolling window eviction
# ---------------------------------------------------------------------------
class TestMonitorRollingWindow:
    """Test that the rolling window evicts old records at max size."""

    @pytest.mark.asyncio
    async def test_window_evicts_at_max_size(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg, window_size=5)

        for i in range(10):
            await monitor.record_success(latency_ms=float(i), target="host:443")

        stats = monitor.get_stats()
        assert stats.window_size == 5

    @pytest.mark.asyncio
    async def test_window_size_one(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg, window_size=1)

        await monitor.record_success(latency_ms=100.0, target="a:443")
        await monitor.record_success(latency_ms=200.0, target="b:443")

        stats = monitor.get_stats()
        assert stats.window_size == 1

    @pytest.mark.asyncio
    async def test_default_window_size_is_100(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        stats = monitor.get_stats()
        assert stats.window_size == 100


# ---------------------------------------------------------------------------
# ConnectionMonitor — get_stats aggregation
# ---------------------------------------------------------------------------
class TestMonitorStats:
    """Test get_stats returns correct aggregates."""

    @pytest.mark.asyncio
    async def test_avg_latency(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        await monitor.record_success(latency_ms=100.0, target="host:443")
        await monitor.record_success(latency_ms=200.0, target="host:443")
        await monitor.record_success(latency_ms=300.0, target="host:443")

        stats = monitor.get_stats()
        assert stats.avg_latency_ms == pytest.approx(200.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_p95_latency(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        # 20 records at 100ms, then 1 slow one at 5000ms
        for _ in range(20):
            await monitor.record_success(latency_ms=100.0, target="host:443")
        await monitor.record_success(latency_ms=5000.0, target="host:443")

        stats = monitor.get_stats()
        # p95 should be high due to the outlier
        assert stats.p95_latency_ms >= 100.0

    @pytest.mark.asyncio
    async def test_stats_empty_window(self):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config()
        monitor = ConnectionMonitor(cfg)

        stats = monitor.get_stats()
        assert stats.total_connections == 0
        assert stats.total_errors == 0
        assert stats.avg_latency_ms == 0.0
        assert stats.p95_latency_ms == 0.0

    @pytest.mark.asyncio
    async def test_total_rotations_incremented(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=1)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        stats = monitor.get_stats()
        assert stats.total_rotations >= 1

    @pytest.mark.asyncio
    async def test_last_rotation_time_updated(self):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=1)
        callback = AsyncMock()
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        stats_before = monitor.get_stats()
        assert stats_before.last_rotation_time == 0.0

        await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        stats_after = monitor.get_stats()
        assert stats_after.last_rotation_time > 0.0


# ---------------------------------------------------------------------------
# ConnectionMonitor — slow threshold logging
# ---------------------------------------------------------------------------
class TestMonitorSlowThreshold:
    """Test that slow connections log a warning."""

    @pytest.mark.asyncio
    async def test_slow_connection_logs_warning(self, caplog):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config(slow_threshold_ms=100.0)
        monitor = ConnectionMonitor(cfg)

        with caplog.at_level(logging.WARNING, logger="proxy_relay.monitor"):
            await monitor.record_success(latency_ms=500.0, target="slow.host:443")

        assert any("slow" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_fast_connection_no_warning(self, caplog):
        from proxy_relay.monitor import ConnectionMonitor

        cfg = _make_monitor_config(slow_threshold_ms=2000.0)
        monitor = ConnectionMonitor(cfg)

        with caplog.at_level(logging.WARNING, logger="proxy_relay.monitor"):
            await monitor.record_success(latency_ms=50.0, target="fast.host:443")

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_records) == 0


# ---------------------------------------------------------------------------
# MonitorStats dataclass
# ---------------------------------------------------------------------------
class TestMonitorStatsDataclass:
    """Test MonitorStats has expected fields."""

    def test_monitor_stats_fields(self):
        from proxy_relay.monitor import MonitorStats

        stats = MonitorStats(
            total_connections=10,
            total_errors=2,
            total_rotations=1,
            window_size=5,
            window_error_count=0,
            avg_latency_ms=150.0,
            p95_latency_ms=300.0,
            last_rotation_time=0.0,
        )
        assert stats.total_connections == 10
        assert stats.total_errors == 2
        assert stats.total_rotations == 1
        assert stats.window_size == 5
        assert stats.window_error_count == 0
        assert stats.avg_latency_ms == 150.0
        assert stats.p95_latency_ms == 300.0
        assert stats.last_rotation_time == 0.0

    def test_monitor_stats_defaults(self):
        from proxy_relay.monitor import MonitorStats

        stats = MonitorStats()
        assert stats.total_connections == 0
        assert stats.total_errors == 0
        assert stats.total_rotations == 0
        assert stats.window_size == 100
        assert stats.window_error_count == 0
        assert stats.avg_latency_ms == 0.0
        assert stats.p95_latency_ms == 0.0
        assert stats.last_rotation_time == 0.0


# ---------------------------------------------------------------------------
# ConnectionMonitor — rotation callback exception handling
# ---------------------------------------------------------------------------
class TestMonitorRotationCallbackError:
    """Test that a failing rotate_callback does not crash the monitor."""

    @pytest.mark.asyncio
    async def test_callback_exception_is_caught(self, caplog):
        from proxy_relay.monitor import ConnectionMonitor, ConnectionOutcome

        cfg = _make_monitor_config(error_threshold_count=1)
        callback = AsyncMock(side_effect=RuntimeError("rotation failed"))
        monitor = ConnectionMonitor(cfg, rotate_callback=callback)

        with caplog.at_level(logging.ERROR, logger="proxy_relay.monitor"):
            await monitor.record_error(ConnectionOutcome.TUNNEL_ERROR, target="host:443")

        # Callback was called and raised, but monitor did not crash
        callback.assert_called_once()
        assert any("rotation failed" in r.message for r in caplog.records)

        # Rotation count should still increment
        stats = monitor.get_stats()
        assert stats.total_rotations == 1
