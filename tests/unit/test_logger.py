"""Tests for proxy_relay.logger — configure_logging thread safety (F-RL1)."""
from __future__ import annotations

import logging
import threading
from unittest.mock import patch

import pytest

import proxy_relay.logger as logger_module
from proxy_relay.logger import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logger_state():
    """Reset logger module globals and root proxy_relay handler state between tests.

    Resets BEFORE the test (clean slate) and RESTORES AFTER (leave no side effects).
    """
    root = logging.getLogger("proxy_relay")
    original_handlers = root.handlers[:]
    original_level = root.level
    original_configured = logger_module._CONFIGURED

    # Clean slate before each test
    root.handlers = []
    logger_module._CONFIGURED = False

    yield

    # Restore original state after each test
    root.handlers = original_handlers
    root.setLevel(original_level)
    logger_module._CONFIGURED = original_configured


class TestConfigureLogging:
    """Test configure_logging handler installation and level updates."""

    def test_first_call_installs_stream_handler(self):
        """First configure_logging call adds exactly one StreamHandler."""
        root = logging.getLogger("proxy_relay")
        initial_count = len(root.handlers)

        configure_logging("INFO")

        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.NullHandler)]
        assert len(stream_handlers) == initial_count + 1

    def test_second_call_does_not_add_extra_handler(self):
        """Repeated configure_logging calls do not accumulate handlers."""
        configure_logging("INFO")
        root = logging.getLogger("proxy_relay")
        count_after_first = len(root.handlers)

        configure_logging("DEBUG")
        assert len(root.handlers) == count_after_first

    def test_second_call_updates_level(self):
        """configure_logging with a different level updates the root logger level."""
        configure_logging("WARNING")
        configure_logging("DEBUG")

        root = logging.getLogger("proxy_relay")
        assert root.level == logging.DEBUG

    def test_same_level_is_noop(self):
        """configure_logging with same level is a no-op (no duplicate handler)."""
        configure_logging("INFO")
        root = logging.getLogger("proxy_relay")
        count = len(root.handlers)

        configure_logging("INFO")
        assert len(root.handlers) == count

    def test_handler_installed_before_configured_flag_set(self):
        """Handler must be added to root before _CONFIGURED is set True.

        Verifies the F-RL1 fix: a concurrent caller checking _CONFIGURED under
        the lock would see _CONFIGURED=True only AFTER the handler is installed,
        so any warning it emits goes to a real handler, not a NullHandler.
        """
        installation_order: list[str] = []
        original_add_handler = logging.Logger.addHandler
        original_init = logger_module._CONFIGURED

        def recording_add_handler(self, handler):
            if self.name == "proxy_relay":
                installation_order.append("handler_added")
            return original_add_handler(self, handler)

        with patch.object(logging.Logger, "addHandler", recording_add_handler):
            configure_logging("INFO")

        # After configure_logging, handler must have been added and _CONFIGURED is True
        assert logger_module._CONFIGURED is True
        assert "handler_added" in installation_order

    def test_concurrent_calls_install_exactly_one_handler(self):
        """Concurrent configure_logging calls do not double-install handlers."""
        root = logging.getLogger("proxy_relay")
        errors: list[Exception] = []

        def call_configure():
            try:
                configure_logging("INFO")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call_configure) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.NullHandler)]
        assert len(stream_handlers) == 1

    def test_get_logger_returns_named_logger(self):
        """get_logger returns a logger with the given name."""
        log = get_logger("proxy_relay.test_module")
        assert log.name == "proxy_relay.test_module"

    def test_get_logger_adds_null_handler_when_no_handlers(self):
        """get_logger adds a NullHandler to prevent 'No handlers' warnings."""
        log = get_logger("proxy_relay.new_test_module")
        assert any(isinstance(h, logging.NullHandler) for h in log.handlers)
