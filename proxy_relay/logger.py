"""Logging configuration for proxy-relay."""
from __future__ import annotations

import logging
import sys
import threading

_CONFIGURED = False
_CONFIGURE_LOCK = threading.Lock()


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

    On the first call, installs a ``StreamHandler`` on the ``proxy_relay``
    root logger.  Subsequent calls with a *different* level update the root
    logger level immediately (the handler is reused).  Repeated calls with
    the same level are no-ops.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
    """
    global _CONFIGURED  # noqa: PLW0603

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger("proxy_relay")

    with _CONFIGURE_LOCK:
        if _CONFIGURED:
            if root.level != numeric_level:
                root.warning(
                    "configure_logging called again with level=%s (was %s) — updating",
                    level.upper(),
                    logging.getLevelName(root.level),
                )
                root.setLevel(numeric_level)
            return

        _CONFIGURED = True
        root.setLevel(numeric_level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
