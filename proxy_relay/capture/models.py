"""Data models and constants for the CDP capture subsystem."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# NOTE: Do NOT import from proxy_relay.config here — that would create a
# circular import because config.py lazily imports CaptureConfig from this module.
DEFAULT_TELEMETRY_DIR: Path = Path.home() / ".config" / "proxy-relay" / "telemetry"
DEFAULT_CAPTURE_DIR: Path = DEFAULT_TELEMETRY_DIR / "capture"
DEFAULT_CAPTURE_DB: Path = DEFAULT_CAPTURE_DIR / "capture.db"
DEFAULT_REPORT_DIR: Path = DEFAULT_TELEMETRY_DIR / "reports"

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

DEFAULT_REDACT_POST_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "_password",
        "client_secret",
        "secret",
        "g-recaptcha-response",
        "recaptcha",
    }
)

MAX_BODY_BYTES: int = 65_536
INDEXEDDB_PAGE_SIZE: int = 100

JSON_MIME_TYPES: frozenset[str] = frozenset({
    "application/json",
    "text/json",
    "application/vnd.api+json",
    "application/hal+json",
})


def is_json_mime(mime_type: str) -> bool:
    """Return True if *mime_type* indicates a JSON response body."""
    mt = mime_type.lower().split(";")[0].strip()
    return mt in JSON_MIME_TYPES or mt.endswith("+json")


# MIME prefixes that are binary — never worth fetching the body for analysis.
_BINARY_MIME_PREFIXES: tuple[str, ...] = (
    "image/",
    "audio/",
    "video/",
    "font/",
    "application/octet-stream",
    "application/wasm",
)


def should_capture_body(mime_type: str) -> bool:
    """Return True if the response body is worth capturing.

    Captures JSON, text, XML, and empty/unknown MIME types (which often
    indicate API responses where CDP didn't report a Content-Type).
    Skips binary content (images, audio, video, fonts, wasm).
    """
    if not mime_type:
        return True  # empty MIME — likely an API response, worth trying
    mt = mime_type.lower().split(";")[0].strip()
    if mt.startswith(_BINARY_MIME_PREFIXES):
        return False
    return True
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
        report_dir: Directory for analysis report files.  ``None`` means
            auto-resolve to ``DEFAULT_REPORT_DIR``.
        auto_analyze: If ``True``, run console analysis after capture stops.
        auto_report: If ``True``, write a markdown report file after capture stops.
        redact_post_fields: Lowercase field names whose values are replaced with
            ``"[REDACTED]"`` in stored POST bodies (JSON and URL-encoded forms).
            Protects passwords and reCAPTCHA tokens from being stored in plaintext
            in the capture DB (F-RL28).
        max_cdp_reconnects: Maximum CDP reconnect attempts before giving up (F-RL18).
        cdp_reconnect_delay_s: Initial delay in seconds before the first reconnect.
        cdp_reconnect_backoff_factor: Multiplicative factor applied after each reconnect.
        cdp_reconnect_max_delay_s: Upper bound on the inter-reconnect delay.
        rotate_db: If ``True``, rename the existing DB file before opening a new session
            so each session gets a fresh database (F-RL21).
        min_rotate_kb: Skip rotation if the existing DB is smaller than this
            threshold in KiB — near-empty captures from short sessions are
            overwritten instead of archived.
        max_db_size_mb: Purge rotated DBs larger than this size in MiB (F-RL23).
        max_db_age_days: Purge rotated DBs older than this many days (F-RL23).
        max_db_count: Keep at most this many rotated DBs per profile.  When
            exceeded, the oldest files are deleted first.  0 means unlimited.
        max_report_count: Keep at most this many report files.  Oldest are
            deleted first when exceeded.  0 means unlimited.
        max_report_age_days: Purge report files older than this many days.
    """

    db_path: Path | None = None
    domains: frozenset[str] = field(default_factory=lambda: DEFAULT_CAPTURE_DOMAINS)
    redact_headers: frozenset[str] = field(default_factory=lambda: DEFAULT_REDACT_HEADERS)
    max_body_bytes: int = MAX_BODY_BYTES
    cookie_poll_interval_s: float = COOKIE_POLL_INTERVAL_S
    storage_poll_interval_s: float = STORAGE_POLL_INTERVAL_S
    report_dir: Path | None = None
    auto_analyze: bool = True
    auto_report: bool = False
    redact_post_fields: frozenset[str] = field(
        default_factory=lambda: DEFAULT_REDACT_POST_FIELDS
    )
    # CDP reconnect backoff (F-RL18)
    max_cdp_reconnects: int = 50
    cdp_reconnect_delay_s: float = 2.0
    cdp_reconnect_backoff_factor: float = 1.5
    cdp_reconnect_max_delay_s: float = 60.0
    # DB rotation and purge (F-RL21/F-RL23)
    rotate_db: bool = True
    min_rotate_kb: int = 256
    max_db_size_mb: int = 500
    max_db_age_days: int = 7
    max_db_count: int = 20
    # Report purge
    max_report_count: int = 20
    max_report_age_days: int = 30

    def resolved_db_path(self) -> Path:
        """Return the effective database path, falling back to DEFAULT_CAPTURE_DB."""
        return self.db_path if self.db_path is not None else DEFAULT_CAPTURE_DB

    def resolved_report_dir(self) -> Path:
        """Return the effective report directory, falling back to DEFAULT_REPORT_DIR."""
        return self.report_dir if self.report_dir is not None else DEFAULT_REPORT_DIR
