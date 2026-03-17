"""Post-capture traffic analyzer — extracts security intelligence from capture.db.

Runs 6 analysis modules against a CDP capture database and produces a
structured report.  Uses ``sqlite3`` directly in read-only mode — no
dependency on ``telemetry-monitor``.

Public API
----------
- :func:`analyze` — run all analysis modules, return :class:`AnalysisReport`
- :func:`print_report` — print console summary
- :func:`write_report` — write detailed markdown report file
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from proxy_relay.logger import get_logger

log = get_logger(__name__)

# ── Key classification patterns ──────────────────────────────────────────

_AUTH_PATTERNS = ("token", "auth", "credential", "session", "login", "oauth", "jwt")
_TRACKING_PATTERNS = ("_dd", "datadome", "analytics", "tracking", "pixel", "_ga", "_gid")
_PREF_PATTERNS = ("pref", "setting", "theme", "lang", "locale", "consent", "cookie_policy")

# ── Auth URL patterns for flow detection ─────────────────────────────────

_AUTH_URL_PATTERNS = ("%/token%", "%/oauth%", "%/auth%", "%/login%", "%/signin%", "%/refresh%")

# ── Fingerprint headers of interest ──────────────────────────────────────

_FINGERPRINT_HEADERS = frozenset({
    "user-agent", "accept", "accept-language", "accept-encoding",
    "x-tidal-token", "x-tidal-sessionid",
})


# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class EndpointInfo:
    """A single API endpoint discovered in captured traffic."""

    domain: str
    path: str
    method: str
    status_codes: list[int]
    call_count: int
    json_keys: list[str] = field(default_factory=list)


@dataclass
class AuthEvent:
    """A single auth-related request/response pair."""

    timestamp: str
    url: str
    method: str
    status: int
    response_ms: int


@dataclass
class CookieLifecycle:
    """Cookie mutation timeline."""

    domain: str
    name: str
    snapshots: int
    first_seen: str
    last_seen: str


@dataclass
class FingerprintVector:
    """A header fingerprint extracted from captured requests."""

    header_name: str
    values: list[str]
    consistency: float  # 0.0–1.0


@dataclass
class RateLimitEvent:
    """A detected rate-limit or challenge-response event."""

    timestamp: str
    url: str
    status: int
    preceding_request_count: int


@dataclass
class AnalysisReport:
    """Complete analysis results."""

    db_path: str
    analyzed_at: str
    total_requests: int = 0
    total_responses: int = 0
    session_duration_s: float = 0.0

    api_surface: dict[str, list[EndpointInfo]] = field(default_factory=dict)
    auth_events: list[AuthEvent] = field(default_factory=list)
    cookie_lifecycle: list[CookieLifecycle] = field(default_factory=list)
    fingerprint_vectors: list[FingerprintVector] = field(default_factory=list)
    rate_limit_events: list[RateLimitEvent] = field(default_factory=list)
    behavioral_baseline: dict[str, Any] = field(default_factory=dict)
    options_filtered: int = 0
    storage_intelligence: list[dict[str, Any]] = field(default_factory=list)


# ── Public API ───────────────────────────────────────────────────────────


def analyze(
    db_path: Path,
    *,
    verbose: bool = False,
    session_id: str | None = None,
) -> AnalysisReport:
    """Run all analysis modules against a capture database.

    Opens the database in read-only mode and runs each analysis function.

    Args:
        db_path: Path to the capture.db SQLite file.
        verbose: If True, include full JSON key inventories.
        session_id: If given, restrict analysis to this capture session.

    Returns:
        Populated :class:`AnalysisReport`.

    Raises:
        FileNotFoundError: If *db_path* does not exist.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Capture database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Detect whether session_id column exists (Plan B adds it)
        session_filter = _build_session_filter(conn, session_id)

        report = AnalysisReport(
            db_path=str(db_path),
            analyzed_at=datetime.now(UTC).isoformat(),
        )

        # Basic counts
        report.total_requests = _count(conn, "http_requests", session_filter)
        report.total_responses = _count(conn, "http_responses", session_filter)
        report.session_duration_s = _session_duration(conn, session_filter)

        # Analysis sections
        surface, options_filtered = _analyze_api_surface(conn, verbose, session_filter)
        report.api_surface = surface
        report.options_filtered = options_filtered
        report.auth_events = _analyze_auth_flow(conn, session_filter)
        report.cookie_lifecycle = _analyze_cookies(conn, session_filter)
        report.fingerprint_vectors = _analyze_fingerprints(conn, session_filter)
        report.rate_limit_events = _analyze_rate_limits(conn, session_filter)
        report.behavioral_baseline = _analyze_behavior(conn, session_filter)
        report.storage_intelligence = _analyze_storage(conn, session_filter)

        return report
    finally:
        conn.close()


def print_report(report: AnalysisReport) -> None:
    """Print a section-based console summary."""
    dur = _format_duration(report.session_duration_s)
    print("\n=== Capture Analysis ===")
    print(f"Database: {report.db_path}")
    print(f"Session duration: {dur} | Requests: {report.total_requests}"
          f" | Responses: {report.total_responses}")

    # API Surface
    total_endpoints = sum(len(eps) for eps in report.api_surface.values())
    opts = (f", {report.options_filtered} OPTIONS filtered"
            if report.options_filtered else "")
    print(f"\n--- API Surface ({len(report.api_surface)} domains,"
          f" {total_endpoints} endpoints{opts}) ---")
    for domain, endpoints in report.api_surface.items():
        total_calls = sum(e.call_count for e in endpoints)
        print(f"  {domain} ({len(endpoints)} endpoints, {total_calls} calls)")
        for ep in endpoints[:10]:
            statuses = ",".join(str(s) for s in ep.status_codes)
            print(f"    {ep.method:<5} {ep.path:<40} {ep.call_count:>4} calls  [{statuses}]")
        if len(endpoints) > 10:
            print(f"    ... and {len(endpoints) - 10} more")

    # Auth Flow
    print(f"\n--- Auth Flow ({len(report.auth_events)} events) ---")
    for evt in report.auth_events[:10]:
        ts = evt.timestamp.split("T")[-1][:8] if "T" in evt.timestamp else evt.timestamp
        print(f"    {ts}  {evt.method} {evt.url}  -> {evt.status} ({evt.response_ms}ms)")
    if len(report.auth_events) > 1:
        _print_token_refresh_interval(report.auth_events)

    # Fingerprint Audit
    print("\n--- Fingerprint Audit ---")
    for fv in report.fingerprint_vectors:
        pct = f"{fv.consistency * 100:.0f}%"
        vals = f"{len(fv.values)} value{'s' if len(fv.values) != 1 else ''}"
        status = "" if fv.consistency >= 0.95 else "  WARNING: inconsistent"
        print(f"    {fv.header_name:<20} {vals:<12} ({pct} consistent){status}")

    # Rate Limits
    print("\n--- Rate Limits ---")
    if report.rate_limit_events:
        for evt in report.rate_limit_events[:5]:
            print(f"    {evt.timestamp}  {evt.url}  status={evt.status}"
                  f"  (preceded by {evt.preceding_request_count} req in 60s)")
    else:
        baseline = report.behavioral_baseline
        rate = baseline.get("requests_per_second_avg", 0)
        print(f"    No 429/403 detected. Observed avg rate: {rate:.1f} req/s")

    # Behavioral Baseline
    print("\n--- Behavioral Baseline ---")
    baseline = report.behavioral_baseline
    if baseline.get("gap_p50") is not None:
        print(f"    Inter-request gap: p50={_format_gap(baseline['gap_p50'])},"
              f" p95={_format_gap(baseline.get('gap_p95', 0))}")
    if baseline.get("requests_per_minute_avg") is not None:
        print(f"    Requests/min: avg={baseline['requests_per_minute_avg']:.1f},"
              f" peak={baseline.get('requests_per_minute_peak', 0)}")

    # Storage
    total_keys = len(report.storage_intelligence)
    origins = len({s["origin"] for s in report.storage_intelligence})
    print(f"\n--- Storage ({total_keys} keys across {origins} origins) ---")
    for entry in report.storage_intelligence[:15]:
        cls = entry.get("classification", "unknown")
        print(f"    {entry['key']:<30} ({cls:<10}) {entry['mutations']} mutations")
    if total_keys > 15:
        print(f"    ... and {total_keys - 15} more")
    print()


def write_report(report: AnalysisReport, output_dir: Path | None = None) -> Path:
    """Write a detailed markdown report file.

    Args:
        report: The analysis report to write.
        output_dir: Directory for the report file.  Defaults to
            ``~/.config/proxy-relay/``.

    Returns:
        Path to the written report file.
    """
    if output_dir is None:
        from proxy_relay.capture.models import DEFAULT_REPORT_DIR

        output_dir = DEFAULT_REPORT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"capture-report-{ts}.md"

    lines: list[str] = []
    lines.append("# Capture Analysis Report")
    lines.append("")
    lines.append(f"- **Database:** `{report.db_path}`")
    lines.append(f"- **Analyzed:** {report.analyzed_at}")
    lines.append(f"- **Duration:** {_format_duration(report.session_duration_s)}")
    lines.append(f"- **Requests:** {report.total_requests} |"
                 f" **Responses:** {report.total_responses}")
    if report.options_filtered:
        lines.append(f"- **OPTIONS filtered:** {report.options_filtered}")
    lines.append("")

    # API Surface
    lines.append("## API Surface Map")
    lines.append("")
    for domain, endpoints in report.api_surface.items():
        lines.append(f"### {domain} ({len(endpoints)} endpoints)")
        lines.append("")
        lines.append("| Method | Path | Calls | Status Codes | JSON Keys |")
        lines.append("|--------|------|-------|-------------|-----------|")
        for ep in endpoints:
            statuses = ", ".join(str(s) for s in ep.status_codes)
            keys = ", ".join(ep.json_keys[:5]) if ep.json_keys else "-"
            lines.append(f"| {ep.method} | `{ep.path}` | {ep.call_count} | {statuses} | {keys} |")
        lines.append("")

    # Auth Flow
    lines.append("## Auth Flow")
    lines.append("")
    if report.auth_events:
        lines.append("| Timestamp | Method | URL | Status | Response (ms) |")
        lines.append("|-----------|--------|-----|--------|--------------|")
        for evt in report.auth_events:
            lines.append(f"| {evt.timestamp} | {evt.method} | `{evt.url}` |"
                         f" {evt.status} | {evt.response_ms} |")
    else:
        lines.append("No auth-related requests detected.")
    lines.append("")

    # Fingerprint Audit
    lines.append("## Fingerprint Audit")
    lines.append("")
    lines.append("| Header | Distinct Values | Consistency |")
    lines.append("|--------|----------------|-------------|")
    for fv in report.fingerprint_vectors:
        lines.append(f"| {fv.header_name} | {len(fv.values)} | {fv.consistency * 100:.0f}% |")
    lines.append("")

    # Rate Limits
    lines.append("## Rate Limit Detection")
    lines.append("")
    if report.rate_limit_events:
        lines.append("| Timestamp | URL | Status | Preceding Requests (60s) |")
        lines.append("|-----------|-----|--------|-------------------------|")
        for evt in report.rate_limit_events:
            lines.append(f"| {evt.timestamp} | `{evt.url}` | {evt.status} |"
                         f" {evt.preceding_request_count} |")
    else:
        lines.append("No 429/403 responses detected.")
    lines.append("")

    # Behavioral Baseline
    lines.append("## Behavioral Baseline")
    lines.append("")
    baseline = report.behavioral_baseline
    for key, val in baseline.items():
        if isinstance(val, float):
            lines.append(f"- **{key}:** {val:.2f}")
        else:
            lines.append(f"- **{key}:** {val}")
    lines.append("")

    # Storage Intelligence
    lines.append("## Storage Intelligence")
    lines.append("")
    lines.append("| Origin | Type | Key | Classification | Mutations |")
    lines.append("|--------|------|-----|---------------|-----------|")
    for entry in report.storage_intelligence:
        lines.append(f"| {entry['origin']} | {entry['storage_type']} |"
                     f" `{entry['key']}` | {entry['classification']} | {entry['mutations']} |")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Analysis report written to %s", path)
    return path


# ── Session filter helpers ───────────────────────────────────────────────

# Type alias: (where_clause_fragment, params_tuple)
_SessionFilter = tuple[str, tuple[str, ...]]


def _build_session_filter(
    conn: sqlite3.Connection, session_id: str | None
) -> _SessionFilter:
    """Build a reusable WHERE clause fragment for session_id filtering.

    Returns ``("", ())`` when no filter is needed, or
    ``("AND session_id = ?", (session_id,))`` when filtering is active and
    the column exists.  Gracefully degrades on old DBs without the column.

    Note: Call sites that join multiple tables must qualify ``session_id``
    with the appropriate alias using :func:`_qualify_filter`.
    """
    if session_id is None:
        return ("", ())
    # Check if the session_id column exists in http_requests
    try:
        conn.execute("SELECT session_id FROM http_requests LIMIT 0")
    except sqlite3.OperationalError:
        log.warning(
            "session_id column not found in DB — ignoring --session filter"
        )
        return ("", ())
    return ("AND session_id = ?", (session_id,))


def _qualify_filter(sf: _SessionFilter, alias: str) -> _SessionFilter:
    """Qualify bare ``session_id`` in a filter fragment with a table alias."""
    frag, params = sf
    if not frag:
        return sf
    return (frag.replace("AND session_id", f"AND {alias}.session_id"), params)


# ── URL normalization for CDN/image collapsing (F-RL13) ─────────────────

# Patterns that collapse highly variable path segments
_CDN_IMAGE_RE = re.compile(
    r"^(/images/[^/]+/)\d+x\d+\.jpg$"
)
_CDN_SEGMENT_RE = re.compile(
    r"^(/[a-f0-9]{16,}/)([\w-]+-\d+\.m4[sa])$"
)
# Strip per-request query params that make URLs unique
_STRIP_QUERY_PARAMS = frozenset({"token", "nonce", "ts", "sig", "signature", "t"})


def _normalize_path(domain: str, path: str, query: str) -> str:
    """Collapse CDN/image URL paths into representative patterns.

    Returns a normalized path suitable for grouping.
    """
    # Collapse resources.tidal.com image sizes
    m = _CDN_IMAGE_RE.match(path)
    if m:
        return f"{m.group(1)}{{WxH}}.jpg"

    # Collapse CDN segment URLs (e.g. /{hash}/segment-42.m4s -> /{hash}/{segments})
    m = _CDN_SEGMENT_RE.match(path)
    if m:
        return f"{m.group(1)}{{segments}}"

    return path


# ── Internal analysis functions ──────────────────────────────────────────


def _count(
    conn: sqlite3.Connection,
    table: str,
    session_filter: _SessionFilter = ("", ()),
) -> int:
    """Return row count for a table (safe — table name is never user input)."""
    where_frag, params = session_filter
    where = f"WHERE 1=1 {where_frag}" if where_frag else ""
    cursor = conn.execute(f"SELECT count(*) FROM {table} {where}", params)  # noqa: S608
    return cursor.fetchone()[0]


def _session_duration(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> float:
    """Compute session duration from earliest to latest request timestamp."""
    where_frag, params = session_filter
    where = f"WHERE 1=1 {where_frag}" if where_frag else ""
    cursor = conn.execute(
        f"SELECT min(timestamp), max(timestamp) FROM http_requests {where}",
        params,
    )
    row = cursor.fetchone()
    if row is None or row[0] is None or row[1] is None:
        return 0.0
    try:
        t_min = datetime.fromisoformat(str(row[0]))
        t_max = datetime.fromisoformat(str(row[1]))
        return (t_max - t_min).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def _analyze_api_surface(
    conn: sqlite3.Connection,
    verbose: bool,
    session_filter: _SessionFilter = ("", ()),
) -> tuple[dict[str, list[EndpointInfo]], int]:
    """Extract unique endpoints grouped by domain.

    Returns ``(api_surface_dict, options_filtered_count)``.
    """
    qf_frag, qf_params = _qualify_filter(session_filter, "r")
    where = f"WHERE 1=1 {qf_frag}" if qf_frag else ""
    cursor = conn.execute(
        f"SELECT r.url, r.method, s.status, s.body"  # noqa: S608
        f" FROM http_requests r"
        f" LEFT JOIN http_responses s ON r.request_id = s.request_id"
        f" {where}"
        f" ORDER BY r.url",
        qf_params,
    )

    # Accumulate per endpoint
    endpoints: dict[str, dict[str, Any]] = {}  # "domain|norm_path|method" -> info
    options_filtered = 0
    sample_bodies: dict[str, str] = {}  # key -> first non-empty body (F-RL14)

    for row in cursor:
        url = row[0] or ""
        method = row[1] or "GET"
        status = row[2] or 0
        body = row[3] or ""

        # F-RL15: filter OPTIONS preflight requests
        if method == "OPTIONS":
            options_filtered += 1
            continue

        parsed = urlparse(url)
        domain = parsed.hostname or ""
        raw_path = parsed.path or "/"

        # F-RL13: normalize CDN/image paths
        norm_path = _normalize_path(domain, raw_path, parsed.query)

        key = f"{domain}|{norm_path}|{method}"
        if key not in endpoints:
            endpoints[key] = {
                "domain": domain,
                "path": norm_path,
                "method": method,
                "status_codes": set(),
                "call_count": 0,
                "json_keys": set(),
            }
        info = endpoints[key]
        info["call_count"] += 1
        if status:
            info["status_codes"].add(status)

        # Verbose: accumulate all JSON keys
        if verbose and body:
            keys = _extract_json_keys(body)
            info["json_keys"].update(keys)

        # F-RL14: store first body sample for non-verbose key extraction
        if body and key not in sample_bodies:
            sample_bodies[key] = body

    # Group by domain
    result: dict[str, list[EndpointInfo]] = {}
    for key, info in endpoints.items():
        # F-RL14: extract JSON keys from sample body even in non-verbose mode
        json_keys: set[str] = info["json_keys"]
        if not verbose and key in sample_bodies:
            json_keys = _extract_json_keys(sample_bodies[key])

        ep = EndpointInfo(
            domain=info["domain"],
            path=info["path"],
            method=info["method"],
            status_codes=sorted(info["status_codes"]),
            call_count=info["call_count"],
            json_keys=sorted(json_keys),
        )
        result.setdefault(ep.domain, []).append(ep)

    # Sort each domain's endpoints by call count descending
    for domain in result:
        result[domain].sort(key=lambda e: e.call_count, reverse=True)

    return result, options_filtered


def _analyze_auth_flow(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> list[AuthEvent]:
    """Find auth-related requests and reconstruct token lifecycle."""
    conditions = " OR ".join(f"lower(r.url) LIKE '{p}'" for p in _AUTH_URL_PATTERNS)
    qf_frag, qf_params = _qualify_filter(session_filter, "r")
    extra = f" {qf_frag}" if qf_frag else ""
    cursor = conn.execute(
        f"SELECT r.timestamp, r.url, r.method, s.status, s.response_ms"  # noqa: S608
        f" FROM http_requests r"
        f" LEFT JOIN http_responses s ON r.request_id = s.request_id"
        f" WHERE ({conditions}){extra}"
        f" ORDER BY r.timestamp",
        qf_params,
    )

    events: list[AuthEvent] = []
    for row in cursor:
        events.append(AuthEvent(
            timestamp=str(row[0] or ""),
            url=str(row[1] or ""),
            method=str(row[2] or ""),
            status=int(row[3] or 0),
            response_ms=int(row[4] or 0),
        ))
    return events


def _analyze_cookies(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> list[CookieLifecycle]:
    """Map cookie mutation frequency from cookie snapshots."""
    where_frag, params = session_filter
    where = f"WHERE 1=1 {where_frag}" if where_frag else ""
    cursor = conn.execute(
        f"SELECT domain, name, count(*) as snapshots,"  # noqa: S608
        f" min(timestamp) as first_seen, max(timestamp) as last_seen"
        f" FROM cookies"
        f" {where}"
        f" GROUP BY domain, name"
        f" ORDER BY snapshots DESC",
        params,
    )

    result: list[CookieLifecycle] = []
    for row in cursor:
        result.append(CookieLifecycle(
            domain=str(row[0] or ""),
            name=str(row[1] or ""),
            snapshots=int(row[2] or 0),
            first_seen=str(row[3] or ""),
            last_seen=str(row[4] or ""),
        ))
    return result


def _analyze_fingerprints(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> list[FingerprintVector]:
    """Extract and score header fingerprint consistency."""
    where_frag, params = session_filter
    base_where = "WHERE headers != ''"
    where = f"{base_where} {where_frag}" if where_frag else base_where
    cursor = conn.execute(
        f"SELECT headers FROM http_requests {where}",  # noqa: S608
        params,
    )

    header_values: dict[str, list[str]] = {}  # lowercase_name -> [values]
    for row in cursor:
        headers_str = row[0] or ""
        parsed = _parse_headers(headers_str)
        for name, value in parsed.items():
            lower_name = name.lower()
            if lower_name in _FINGERPRINT_HEADERS:
                header_values.setdefault(lower_name, []).append(value)

    result: list[FingerprintVector] = []
    for header_name, values in sorted(header_values.items()):
        if not values:
            continue
        distinct = sorted(set(values))
        # Consistency = fraction of requests using the most common value
        most_common_count = max(values.count(v) for v in distinct)
        consistency = most_common_count / len(values) if values else 0.0
        result.append(FingerprintVector(
            header_name=header_name,
            values=distinct,
            consistency=round(consistency, 3),
        ))
    return result


def _analyze_rate_limits(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> list[RateLimitEvent]:
    """Detect 429/403 responses and compute preceding request volume."""
    sf_frag, sf_params = _qualify_filter(session_filter, "s")
    base_where = "WHERE s.status IN (429, 403)"
    where = f"{base_where} {sf_frag}" if sf_frag else base_where
    cursor = conn.execute(
        f"SELECT s.timestamp, s.url, s.status"  # noqa: S608
        f" FROM http_responses s"
        f" {where}"
        f" ORDER BY s.timestamp",
        sf_params,
    )

    req_frag, req_params = session_filter  # unqualified — single-table query
    events: list[RateLimitEvent] = []
    for row in cursor:
        ts = str(row[0] or "")
        url = str(row[1] or "")
        status = int(row[2] or 0)

        # Count requests in the 60s window before this event
        preceding = 0
        if ts:
            count_cursor = conn.execute(
                f"SELECT count(*) FROM http_requests"
                f" WHERE timestamp BETWEEN datetime(?, '-60 seconds') AND ?"
                f" {req_frag}",
                (ts, ts, *req_params),
            )
            preceding = count_cursor.fetchone()[0]

        events.append(RateLimitEvent(
            timestamp=ts,
            url=url,
            status=status,
            preceding_request_count=preceding,
        ))
    return events


def _analyze_behavior(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> dict[str, Any]:
    """Compute inter-request timing distribution and session patterns."""
    where_frag, params = session_filter
    where = f"WHERE 1=1 {where_frag}" if where_frag else ""
    cursor = conn.execute(
        f"SELECT timestamp FROM http_requests {where} ORDER BY timestamp",
        params,
    )
    timestamps: list[datetime] = []
    for row in cursor:
        ts_str = str(row[0] or "")
        if ts_str:
            try:
                timestamps.append(datetime.fromisoformat(ts_str))
            except (ValueError, TypeError):
                pass

    result: dict[str, Any] = {}
    if len(timestamps) < 2:
        return result

    # Inter-request gaps
    gaps = [
        (timestamps[i + 1] - timestamps[i]).total_seconds()
        for i in range(len(timestamps) - 1)
    ]
    gaps = [g for g in gaps if g >= 0]  # filter out any negative (clock skew)

    if gaps:
        sorted_gaps = sorted(gaps)
        result["gap_min"] = sorted_gaps[0]
        result["gap_max"] = sorted_gaps[-1]
        result["gap_mean"] = statistics.mean(gaps)
        result["gap_p50"] = sorted_gaps[len(sorted_gaps) // 2]
        p95_idx = min(int(len(sorted_gaps) * 0.95), len(sorted_gaps) - 1)
        result["gap_p95"] = sorted_gaps[p95_idx]

    # Session duration and rate
    duration = (timestamps[-1] - timestamps[0]).total_seconds()
    if duration > 0:
        result["requests_per_second_avg"] = round(len(timestamps) / duration, 2)
        result["requests_per_minute_avg"] = round(len(timestamps) / duration * 60, 1)

    # Peak requests per minute (sliding 60s window)
    if duration > 0:
        peak_rpm = 0
        for i, ts in enumerate(timestamps):
            window_end = ts.timestamp() + 60
            count = sum(1 for t in timestamps[i:] if t.timestamp() <= window_end)
            peak_rpm = max(peak_rpm, count)
        result["requests_per_minute_peak"] = peak_rpm

    return result


def _analyze_storage(
    conn: sqlite3.Connection,
    session_filter: _SessionFilter = ("", ()),
) -> list[dict[str, Any]]:
    """Inventory and classify storage keys by purpose."""
    where_frag, params = session_filter
    where = f"WHERE 1=1 {where_frag}" if where_frag else ""
    cursor = conn.execute(
        f"SELECT origin, storage_type, key, count(*) as mutations"  # noqa: S608
        f" FROM storage_snapshots"
        f" {where}"
        f" GROUP BY origin, storage_type, key"
        f" ORDER BY mutations DESC",
        params,
    )

    result: list[dict[str, Any]] = []
    for row in cursor:
        key_name = str(row[2] or "")
        result.append({
            "origin": str(row[0] or ""),
            "storage_type": str(row[1] or ""),
            "key": key_name,
            "mutations": int(row[3] or 0),
            "classification": _classify_storage_key(key_name),
        })
    return result


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_headers(headers_str: str) -> dict[str, str]:
    """Parse newline-separated ``Name: Value`` headers into a dict.

    Handles both ``Name: Value`` and ``Name:Value`` (no space after colon).
    """
    result: dict[str, str] = {}
    for line in headers_str.split("\n"):
        if ":" in line:
            name, _, value = line.partition(":")
            name = name.strip()
            value = value.strip()
            if name:
                result[name] = value
    return result


def _extract_json_keys(body: str) -> set[str]:
    """Extract top-level keys from a JSON body string."""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return set(data.keys())
    except (json.JSONDecodeError, ValueError):
        pass
    return set()


def _classify_storage_key(key: str) -> str:
    """Classify a storage key by name pattern."""
    lower = key.lower()
    for pattern in _AUTH_PATTERNS:
        if pattern in lower:
            return "auth"
    for pattern in _TRACKING_PATTERNS:
        if pattern in lower:
            return "tracking"
    for pattern in _PREF_PATTERNS:
        if pattern in lower:
            return "prefs"
    return "other"


def _format_gap(seconds: float) -> str:
    """Format an inter-request gap with appropriate precision.

    Sub-second gaps are displayed in milliseconds for clarity.
    """
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m"


def _print_token_refresh_interval(events: list[AuthEvent]) -> None:
    """Print estimated token refresh interval from auth events."""
    refresh_events = [e for e in events if "token" in e.url.lower() or "refresh" in e.url.lower()]
    if len(refresh_events) >= 2:
        timestamps: list[datetime] = []
        for evt in refresh_events:
            try:
                timestamps.append(datetime.fromisoformat(evt.timestamp))
            except (ValueError, TypeError):
                pass
        if len(timestamps) >= 2:
            gaps = [
                (timestamps[i + 1] - timestamps[i]).total_seconds()
                for i in range(len(timestamps) - 1)
            ]
            avg_gap = statistics.mean(gaps) if gaps else 0
            print(f"    Token refresh interval: ~{_format_duration(avg_gap)}")
