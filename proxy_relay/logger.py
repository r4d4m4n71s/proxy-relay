"""Logging configuration for proxy-relay."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for the proxy-relay package.

    Args:
        name: Logger name, typically ``__name__``.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def configure_logging(level: str = "INFO") -> None:
    """Configure root proxy_relay logger with console output.

    Idempotent — only configures once per process. Subsequent calls are no-ops.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
    """
    global _CONFIGURED  # noqa: PLW0603
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("proxy_relay")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
