"""Data models and constants for the CDP capture subsystem."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# NOTE: Do NOT import from proxy_relay.config here — that would create a
# circular import because config.py lazily imports CaptureConfig from this module.
DEFAULT_CAPTURE_DB: Path = Path.home() / ".config" / "proxy-relay" / "capture.db"

DEFAULT_CAPTURE_DOMAINS: frozenset[str] = frozenset({"tidal.com", "qobuz.com"})

DEFAULT_REDACT_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-tidal-token",
        "x-user-auth-token",
        "proxy-authorization",
    }
)

MAX_BODY_BYTES: int = 65_536
COOKIE_POLL_INTERVAL_S: float = 30.0
STORAGE_POLL_INTERVAL_S: float = 60.0


@dataclass(frozen=True)
class CaptureConfig:
    """Configuration for the CDP capture session.

    All fields have defaults matching the module-level constants so
    ``CaptureConfig()`` gives a sane out-of-the-box configuration.

    Attributes:
        db_path: Path to the SQLite capture database.  ``None`` means
            auto-resolve to ``DEFAULT_CAPTURE_DB`` at session start time.
        domains: Set of domain suffixes to capture traffic for.
        redact_headers: Lowercase header names whose values should be
            partially redacted in stored payloads.
        max_body_bytes: Maximum UTF-8 bytes of request/response body stored.
        cookie_poll_interval_s: Seconds between ``Network.getAllCookies`` polls.
        storage_poll_interval_s: Seconds between localStorage/sessionStorage polls.
    """

    db_path: Path | None = None
    domains: frozenset[str] = field(default_factory=lambda: DEFAULT_CAPTURE_DOMAINS)
    redact_headers: frozenset[str] = field(default_factory=lambda: DEFAULT_REDACT_HEADERS)
    max_body_bytes: int = MAX_BODY_BYTES
    cookie_poll_interval_s: float = COOKIE_POLL_INTERVAL_S
    storage_poll_interval_s: float = STORAGE_POLL_INTERVAL_S

    def resolved_db_path(self) -> Path:
        """Return the effective database path, falling back to DEFAULT_CAPTURE_DB."""
        return self.db_path if self.db_path is not None else DEFAULT_CAPTURE_DB
