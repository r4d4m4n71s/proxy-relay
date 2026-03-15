# Traffic Capture & Analysis — Research Document

> **Date:** 2026-03-15
> **Status:** Research / Pre-design
> **Purpose:** Evaluate approaches for capturing and analyzing user interactions with TIDAL/Qobuz web interfaces through proxy-relay, to inform future tidal-dl security improvements.

---

## Table of Contents

- [Goal](#goal)
- [What We Want to Capture](#what-we-want-to-capture)
- [mitmproxy vs proxy-st vs proxy-relay — Context](#mitmproxy-vs-proxy-st-vs-proxy-relay--context)
- [The Core Challenge: HTTPS](#the-core-challenge-https)
- [TLS Fingerprint Problem](#tls-fingerprint-problem)
- [Proxy Visibility Limits](#proxy-visibility-limits)
- [Latency Analysis](#latency-analysis)
- [Approaches Evaluated](#approaches-evaluated)
  - [Option A: Embed mitmproxy in proxy-relay](#option-a-embed-mitmproxy-as-a-library-in-proxy-relay)
  - [Option B: mitmproxy as separate process](#option-b-mitmproxy-as-a-separate-process-in-the-chain)
  - [Option C: mitmproxy standalone for research](#option-c-mitmproxy-standalone-for-research-sessions)
  - [Option D: Chrome DevTools Protocol via browse](#option-d-chrome-devtools-protocol-cdp-via-proxy-relay-browse)
- [Recommendation](#recommendation)
- [Next Steps](#next-steps)

---

## Goal

Detect and track user interactions with TIDAL and Qobuz web interfaces, capturing:

- **Cookies**: store/lifecycle/key-values, Set-Cookie headers, JS-set cookies
- **Requests**: API endpoints, headers, auth tokens, request bodies
- **Responses**: API response schemas, status codes, timing
- **Stored data**: localStorage, sessionStorage, IndexedDB, service worker caches

This evidence feeds into tidal-dl's security research (`docs/tidal-security-research.md`) to understand:

1. How TIDAL/Qobuz authenticate and maintain sessions in the browser
2. What client-side fingerprinting data is collected
3. Cookie lifecycles and token refresh patterns
4. API endpoint discovery and schema mapping
5. Anti-bot detection signals observable from the client side

The captured data would be stored and analyzed via **telemetry-monitor** (SQLite + optional Qdrant).

---

## Primary Scenario — Passive Browser Observation

The primary use case is **not** intercepting tidal-dl traffic. It is observing a **real user browsing session** where the user navigates TIDAL or Qobuz with their personal account through `proxy-relay browse`.

```
User opens proxy-relay browse --capture --profile us-browse
    |
    |-- Navigates to listen.tidal.com, logs in with personal account
    |-- Browses albums, plays tracks, manages playlists, checks settings
    |-- CDP silently observes everything: API calls, cookies, tokens, storage
    |
    +-- After session: captured data in ~/.config/proxy-relay/capture.db
        |
        +-- Analyzed to answer questions like:
            - What API endpoints does the TIDAL web player call?
            - What cookies does TIDAL set, what are their lifetimes?
            - What does a real login flow look like (OAuth, tokens, refresh)?
            - What data does TIDAL store in localStorage/IndexedDB?
            - What headers does the web player send (auth, fingerprint, tracking)?
            - How does the playback event reporting work (WebSocket)?
            - What rate patterns emerge during normal browsing?
            - How does Qobuz's auth + signed-URL flow work in the browser?
```

### Why this matters for tidal-dl

tidal-dl currently uses `tidalapi` (a reverse-engineered Python library) and raw Qobuz API calls. Both are informed by community knowledge, but there are gaps:

| Gap in tidal-dl | What browser observation reveals |
|-----------------|--------------------------------|
| **Auth token lifecycle** — when do tokens refresh? What triggers it? | Exact refresh timing, refresh endpoints, token format evolution |
| **Playback event protocol** — tidal-dl sends synthetic events, but are they accurate? | Real event payloads, timing, WebSocket heartbeats |
| **Cookie-based tracking** — does TIDAL use cookies for anti-bot? | Full cookie inventory, which cookies are checked server-side |
| **Client-side fingerprinting** — what JS fingerprints does TIDAL collect? | localStorage keys like `_dd_s` (DataDome), canvas hashes, WebGL data |
| **API endpoint discovery** — undocumented endpoints for HiRes, Atmos, lyrics | Every API call the web player makes, with full request/response |
| **Rate patterns** — how fast does a real user generate API calls? | Request timing distribution, burst patterns, idle gaps |
| **Qobuz signed URLs** — how does the browser obtain download URLs? | The exact flow: app_id, secret, timestamp, signature construction |

### What the user does vs what the system captures

The user browses **normally** — no special actions needed. The capture is entirely passive:

| User action | What CDP captures | Value for tidal-dl |
|-------------|-------------------|-------------------|
| Opens listen.tidal.com | Initial page load: 30-50 API calls, cookies set, localStorage populated | Full API call graph for session init |
| Logs in | OAuth flow: redirect chain, token exchange, Set-Cookie headers | Auth flow replication |
| Browses an album | `GET /v1/albums/{id}`, `/v1/albums/{id}/tracks`, `/v1/albums/{id}/credits` | Endpoint discovery + response schemas |
| Plays a track | Stream URL request, playback events via WebSocket, progress reporting | Playback event protocol for `events/sender.py` |
| Sits idle for 30 min | Token refresh cycle, heartbeat WebSocket frames, cookie rotation | Session maintenance patterns |
| Searches for an artist | Search API endpoints, autocomplete, result pagination | Search API documentation |
| Adds to playlist | Write API endpoints, CSRF tokens, request signing | Write-path API mapping |
| Navigates to Settings | Account info endpoints, subscription tier detection | Subscription/quality capability detection |

### Separation from tidal-dl usage

This scenario is explicitly separate from tidal-dl's download workflow:

```
proxy-relay browse --capture    <-- THIS: observe real browser behavior
tidal-dl dl <url>              <-- NOT THIS: tidal-dl has its own telemetry
```

The captured data feeds into tidal-dl **indirectly** — through security research analysis, not through runtime integration. The workflow is:

1. **Capture**: user browses TIDAL/Qobuz normally with `--capture`
2. **Analyze**: query `capture.db` with telemetry-monitor dashboards
3. **Document**: update `docs/tidal-security-research.md` with findings
4. **Improve**: apply findings to tidal-dl code (fingerprinting, events, auth, timing)

---

## What We Want to Capture

| Data type | Example | Value for security research |
|-----------|---------|---------------------------|
| HTTP cookies (Set-Cookie) | `_tid_session=abc123; Secure; HttpOnly` | Token lifecycle, domain scope, expiry patterns |
| Request headers | `Authorization: Bearer ...`, `X-Tidal-Token` | Auth mechanism, token format, rotation timing |
| Request URLs + bodies | `POST /v1/sessions`, `GET /v1/tracks/123/streamUrl` | API endpoint discovery, parameter mapping |
| Response bodies | JSON payloads from API calls | Schema documentation, error codes, rate limit headers |
| localStorage | `tidal.playback_session`, `tidal.user_id` | Client-side state, offline capabilities |
| sessionStorage | Session-scoped tokens, UI state | Ephemeral auth data |
| IndexedDB | Cached track metadata, offline content | Content caching strategy |
| WebSocket messages | Real-time playback events | Event protocol, heartbeat patterns |
| JS-computed values | `navigator.userAgent`, canvas fingerprint | Anti-bot fingerprint vectors |

---

## mitmproxy vs proxy-st vs proxy-relay — Context

| | **mitmproxy** | **proxy-st** | **proxy-relay** |
|---|---|---|---|
| **Purpose** | General HTTPS intercepting proxy (debug, security testing, traffic analysis) | Residential proxy profile manager + IProxy provider plugin | Local HTTP/CONNECT tunnel through upstream SOCKS5 |
| **TLS interception** | Yes (core feature — generates CA, re-signs certs on the fly) | No | No |
| **Traffic modification** | Yes (live editing, scripting, replay) | No | No |
| **Privacy/anti-detection** | None (designed to inspect, not hide) | TLS fingerprinting via curl-cffi | DNS leak prevention, header stripping, TZ spoofing, WebRTC protection |
| **Browser integration** | Web UI (mitmweb) for inspection | No | Supervised Chromium launch with isolated profiles |
| **Scripting** | Python addon API with full event hooks | Library API (IProxy interface) | No |

**Key insight:** mitmproxy excels at traffic capture but has zero anti-detection. proxy-relay excels at anti-detection but has zero traffic visibility. The question is how to combine both capabilities without compromising either.

---

## The Core Challenge: HTTPS

TIDAL and Qobuz use HTTPS exclusively. Through proxy-relay's current CONNECT tunnel, traffic is end-to-end encrypted — we can see `CONNECT api.tidal.com:443` but nothing inside the tunnel.

To capture cookies, request bodies, API responses, and auth tokens via a proxy, **TLS interception (MITM)** is required:

1. Generate a local CA certificate and install it on the client (browser/system)
2. Decrypt incoming TLS from the client using the local CA
3. Re-encrypt outgoing TLS toward the upstream server
4. Parse HTTP inside the decrypted stream to extract cookies, headers, bodies
5. Every HTTPS request goes through two TLS handshakes instead of one

---

## TLS Fingerprint Problem

This is the **most critical concern** for any MITM-based approach.

The current proxy stack carefully preserves TLS fingerprints:

```
tidal-dl --> curl-cffi (Chrome TLS fingerprint) --> proxy-st --> SOCKS5 --> TIDAL API
Browser  --> native Chrome TLS                  --> proxy-relay --> SOCKS5 --> TIDAL
```

Inserting a MITM proxy breaks this chain on the **outbound** side:

```
Browser --> MITM proxy (mitmproxy's own TLS fingerprint!) --> SOCKS5 --> TIDAL
```

mitmproxy has its own TLS signature — it does not impersonate Chrome's JA3/JA4 fingerprint. TIDAL's anti-bot systems (DataDome, etc.) could detect that the outbound TLS doesn't match a real browser.

**This undermines the security research itself** — we'd observe behavior under a detectably different fingerprint, meaning TIDAL might respond differently (rate limit, block, serve different content) than it would to a real browser.

---

## Proxy Visibility Limits

Even with full TLS interception, some data lives **inside the browser** and never crosses the network:

| Data type | Visible to MITM proxy? | How to capture instead |
|-----------|----------------------|------------------------|
| HTTP cookies (Set-Cookie headers) | Yes | Parse response headers |
| API requests/responses | Yes | Full body capture |
| Auth tokens in headers | Yes | Parse request headers |
| localStorage / sessionStorage | **No** | Chrome DevTools Protocol (CDP) or browser extension |
| IndexedDB | **No** | CDP or browser extension |
| Service Worker cache | **No** | CDP or browser extension |
| JS-computed fingerprint values | **No** | CDP (Runtime.evaluate) |
| WebSocket messages | Yes (with MITM) | Parse upgraded connections |

A proxy-only approach captures roughly **half the picture**. Browser-internal storage — where TIDAL/Qobuz keep playback state, offline caching, and session persistence — remains invisible.

---

## Latency Analysis

### TLS Interception (MITM) Overhead

| Component | Added latency | Notes |
|-----------|--------------|-------|
| Local TLS handshake (client <-> MITM) | ~1-3ms | Localhost, negligible |
| Decrypt + re-encrypt per request | ~0.5-2ms | CPU-bound, fast on modern hardware |
| HTTP parsing + event emission | ~0.1-0.5ms | Depends on body size |
| **Total per-request (MITM)** | **~2-5ms** | Imperceptible for browsing |
| telemetry-monitor write (async) | ~0ms on hot path | BackgroundWriter, deque-buffered |

**For browsing research**: imperceptible. A page load with 50 requests adds ~100-250ms total — lost in network noise.

**For tidal-dl segment downloads**: more concerning. Hundreds of small requests per track. Extra 3ms/segment x 200 segments = ~600ms per track. Measurable but not blocking.

### CDP Observation Overhead

| Component | Added latency | Notes |
|-----------|--------------|-------|
| Network event subscription | ~0ms on traffic path | Out-of-band WebSocket from CDP |
| Cookie/storage polling | ~0ms on traffic path | Separate CDP commands, async |
| Event serialization + write | ~0ms on traffic path | BackgroundWriter |
| **Total on traffic path** | **~0ms** | CDP observes, doesn't intercept |

CDP adds **zero latency** to the actual network traffic. It's a side-channel observer connected via a separate WebSocket to the browser's debug port.

---

## Approaches Evaluated

### Option A: Embed mitmproxy as a library in proxy-relay

Replace proxy-relay's async TCP server with mitmproxy's proxy engine. Custom addons capture traffic and write to telemetry-monitor.

| Aspect | Assessment |
|--------|-----------|
| Pros | Single process, full HTTP visibility, mature HTTP parsing |
| Cons | Heavy dependency (~50+ transitive packages), replaces proxy-relay's entire core, mitmproxy's TLS fingerprint is detectable, breaks current architecture |
| Latency | ~2-5ms per request |
| Effort | XL — essentially rewrite proxy-relay |
| Risk | High — mitmproxy API changes could break customizations; loses current anti-detection features |

**Verdict:** Rejected. Too invasive, destroys proxy-relay's identity as an anti-detection tool.

### Option B: mitmproxy as a separate process in the chain

```
Browser --> proxy-relay --> mitmproxy (MITM) --> SOCKS5 upstream
```

| Aspect | Assessment |
|--------|-----------|
| Pros | Both tools stay independent, can enable/disable MITM layer |
| Cons | 3 processes to manage, extra network hop, still has TLS fingerprint problem, complex orchestration |
| Latency | ~5-10ms per request (extra hop + MITM) |
| Effort | L — orchestration, config, addon development |
| Risk | Medium — deployment complexity, TLS fingerprint divergence |

**Verdict:** Possible but overcomplicated. Doesn't solve the fingerprint or browser-storage problems.

### Option C: mitmproxy standalone for research sessions

Don't modify proxy-relay. Use mitmproxy separately for API-level research, with a custom Python addon that writes to telemetry-monitor.

```
Research mode:  Browser --> mitmproxy (capture addon) --> SOCKS5 --> TIDAL
Production:     Browser --> proxy-relay --> SOCKS5 --> TIDAL  (unchanged)
```

| Aspect | Assessment |
|--------|-----------|
| Pros | Zero impact on production, mitmproxy excels at this, clean separation |
| Cons | Separate tool to run, TLS fingerprint divergence (acceptable for research), no browser-storage visibility, manual workflow |
| Latency | Zero on production path; ~2-5ms only during active research |
| Effort | S-M — write a mitmproxy addon + telemetry schema |
| Risk | Low — isolated from production code |

**Verdict:** Good for API-level research (raw HTTP captures, endpoint discovery). Not sufficient alone for full browser-state analysis.

### Option D: Chrome DevTools Protocol (CDP) via proxy-relay browse

Instrument the browser itself. proxy-relay already launches Chromium — add an optional `--capture` flag that connects via CDP to observe all network activity and browser state.

```
proxy-relay browse --capture
    |
    |-- Launch Chromium with --remote-debugging-port=9222
    |-- Connect CDP client (websocket to localhost:9222)
    |-- Subscribe to Network.requestWillBeSent, Network.responseReceived, etc.
    |-- Periodically poll cookies, localStorage, IndexedDB
    |-- Write events to telemetry-monitor
    |
    +-- Traffic flows normally: Chromium --> proxy-relay --> SOCKS5 (NO MITM)
```

| Aspect | Assessment |
|--------|-----------|
| Pros | **Captures everything** (network + browser storage), **no TLS interception needed**, zero fingerprint impact, integrates naturally with `proxy-relay browse`, browser sees decrypted traffic natively |
| Cons | Only works for browser sessions (not tidal-dl CLI), CDP protocol is complex, Chromium-only |
| Latency | **~0ms on traffic path** (CDP is out-of-band observation) |
| Effort | M-L — CDP client integration, event schema, capture logic |
| Risk | Low-Medium — CDP API is stable but verbose |

**Verdict: Recommended primary approach.** Captures the full picture with zero impact on traffic or fingerprints.

---

## Recommendation

**Option D (CDP) as the primary approach, Option C (mitmproxy standalone) as a complement.**

### Why CDP is the best fit

1. **Captures what proxies cannot** — localStorage, IndexedDB, JS-computed values, cookie lifecycles including JS-set cookies. This is where most of TIDAL's client-side state lives.

2. **Zero latency on the traffic path** — CDP observes from the side via a WebSocket to port 9222. It doesn't sit in the data path. No MITM, no extra TLS handshake.

3. **TLS fingerprint preserved** — no interception means the browser's native TLS fingerprint reaches TIDAL unchanged. We observe *exactly* what TIDAL sees, which is critical for valid security research.

4. **No changes to proxy-relay's core** — the tunnel logic stays clean and fast. Capture is an optional `--capture` flag on the `browse` command.

5. **Natural integration** — proxy-relay already launches and supervises Chromium. Adding `--remote-debugging-port=9222` is one flag. The CDP client runs alongside the existing supervisor thread.

6. **telemetry-monitor integration** — CDP events feed into `BackgroundWriter` -> SQLite, with a `PROXY_RELAY_SCHEMA` for tables like `requests`, `responses`, `cookies`, `storage_snapshots`.

### When to use mitmproxy (Option C)

- Raw API research with curl/httpx (no browser involved)
- Capturing exact wire-format bytes (TLS record structure, HTTP/2 frames)
- Replaying captured traffic for regression testing
- Situations where CDP is unavailable

### What we explicitly reject

- **Option A** (embed mitmproxy): too invasive, destroys proxy-relay's architecture
- **Option B** (mitmproxy in chain): overcomplicated, doesn't solve the fundamental problems
- **Any always-on capture**: capture must be opt-in (`--capture` flag), never default

---

## Architecture Sketch — CDP Integration

### High-Level Flow

```
proxy-relay browse --capture [--capture-domains tidal.com,qobuz.com]
    |
    |  1. Normal browse startup (server auto-start, health check, TZ resolve)
    |
    |  2. Launch Chromium with additional flag:
    |     --remote-debugging-port=<free-port>
    |
    |  3. Connect CDP client (WebSocket to ws://127.0.0.1:<cdp-port>/json)
    |     |
    |     |-- Network.enable()           --> request/response capture
    |     |-- Network.getCookies()       --> periodic cookie snapshots
    |     |-- Runtime.evaluate()         --> localStorage/sessionStorage polling
    |     |-- IndexedDB.enable()         --> IndexedDB inspection (on-demand)
    |     |-- Network.webSocketCreated() --> WebSocket frame capture
    |     |
    |     +-- Events flow to CaptureCollector --> BackgroundWriter --> SQLite
    |
    |  4. Normal BrowseSupervisor loop (PID poll, auto-rotate)
    |
    |  5. On exit: CDP client disconnects, telemetry flushed, browser closed
```

### Module Design

```
proxy_relay/
    capture/
        __init__.py          Public API: CaptureSession
        cdp_client.py        Low-level CDP WebSocket client (asyncio)
        collector.py         CaptureCollector: filters, transforms, enqueues events
        schema.py            PROXY_RELAY_SCHEMA: telemetry-monitor SchemaDefinition
        storage.py           Capture storage: domain filter, body truncation, sensitive-header redaction
```

**4 new modules** inside a `capture/` subpackage. No changes to existing proxy-relay modules except:

- `browse.py` — add `--remote-debugging-port=<port>` to `_chrome_args()` when `capture=True`
- `cli.py` — add `--capture` and `--capture-domains` flags to `browse` subcommand
- `pyproject.toml` — add `telemetry-monitor` as optional dependency (`[capture]` extra), register entry point

### CDP Client (`cdp_client.py`)

Minimal async WebSocket client — no heavy library dependency. Uses stdlib `asyncio` + a lightweight WebSocket library (`websockets` or `aiohttp`).

```python
class CdpClient:
    """Low-level Chrome DevTools Protocol client over WebSocket."""

    async def connect(self, port: int) -> None:
        """Discover browser WebSocket URL and connect.

        GET http://127.0.0.1:{port}/json/version -> webSocketDebuggerUrl
        Then open a WebSocket to that URL.
        """

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and await the result."""

    async def subscribe(self, event: str, callback: Callable) -> None:
        """Register a callback for a CDP event (e.g., Network.requestWillBeSent)."""

    async def close(self) -> None:
        """Disconnect from the browser."""
```

**Why not pychrome/playwright?**
- `pychrome` is unmaintained (last release 2021)
- `playwright` is 50+ MB and brings its own browser binaries — overkill
- Raw WebSocket + JSON is ~100 lines and zero new dependencies beyond `websockets`

### CDP Domains and Events

| CDP Domain | Events / Methods | What we capture |
|------------|-----------------|-----------------|
| **Network** | `requestWillBeSent` | URL, method, headers (incl. auth tokens), POST body |
| **Network** | `responseReceived` + `getResponseBody` | Status, headers (incl. Set-Cookie), response body (JSON APIs) |
| **Network** | `webSocketFrameSent/Received` | WebSocket message payloads |
| **Network** | `getCookies` (polled) | Full cookie jar: name, value, domain, path, expires, httpOnly, secure |
| **Runtime** | `evaluate` (polled) | `localStorage` and `sessionStorage` key-value dumps |
| **IndexedDB** | `requestDatabaseNames` + `requestData` (on-demand) | Database names, object store contents |
| **Page** | `loadEventFired` | Page navigation timestamps (correlate with requests) |

### Capture Collector (`collector.py`)

```python
class CaptureCollector:
    """Filters, transforms, and enqueues CDP events for telemetry storage.

    Responsibilities:
    - Domain allowlist filtering (only capture tidal.com, qobuz.com, etc.)
    - Sensitive header redaction (mask Authorization values, cookie values in logs)
    - Body truncation (cap response bodies at 64KB to avoid bloat)
    - Transform CDP event format -> telemetry-monitor event format
    - Enqueue to BackgroundWriter

    Does NOT:
    - Make CDP calls (that's CdpClient's job)
    - Write to SQLite directly (that's BackgroundWriter's job)
    - Decide when to poll cookies/storage (that's CaptureSession's job)
    """

    def __init__(
        self,
        writer: BackgroundWriter,
        domain_filter: set[str],   # e.g. {"tidal.com", "qobuz.com", "login.tidal.com"}
        redact_headers: set[str],  # e.g. {"authorization", "cookie", "set-cookie"}
        max_body_bytes: int = 65_536,
    ) -> None: ...

    def on_request(self, params: dict) -> None:
        """Handle Network.requestWillBeSent — extract and enqueue request data."""

    def on_response(self, params: dict, body: str | None) -> None:
        """Handle Network.responseReceived — extract and enqueue response data."""

    def on_cookies(self, cookies: list[dict]) -> None:
        """Handle periodic cookie snapshot — diff against previous, enqueue changes."""

    def on_storage(self, origin: str, storage_type: str, data: dict) -> None:
        """Handle localStorage/sessionStorage poll — diff and enqueue changes."""

    def on_websocket(self, direction: str, params: dict) -> None:
        """Handle WebSocket frame — enqueue payload."""
```

### Capture Session (`__init__.py`)

Orchestrates the CDP client, collector, and polling loops:

```python
class CaptureSession:
    """Top-level capture orchestrator.

    Lifecycle:
    1. start(cdp_port) — connect CDP, enable domains, start event listeners
    2. Runs alongside BrowseSupervisor (separate thread or asyncio task)
    3. stop() — disable domains, disconnect, flush remaining events
    """

    def __init__(
        self,
        capture_domains: set[str],
        cookie_poll_interval_s: float = 30.0,
        storage_poll_interval_s: float = 60.0,
        telemetry_db_path: Path | None = None,  # default: ~/.config/proxy-relay/capture.db
    ) -> None: ...

    async def start(self, cdp_port: int) -> None:
        """Connect to browser, enable capture, start polling loops."""

    async def stop(self) -> None:
        """Stop polling, flush events, disconnect."""
```

**Polling intervals:**
- Cookies: every 30s (catches token refresh cycles, which are typically 15-60 min)
- localStorage/sessionStorage: every 60s (less volatile, reduces noise)
- Requests/responses: real-time via CDP event subscriptions (no polling)

### Telemetry Schema (`schema.py`)

```python
PROXY_RELAY_SCHEMA = SchemaDefinition(
    tables=[
        TableSchema(
            name="http_requests",
            columns=[
                ColumnDef("request_id"),
                ColumnDef("url"),
                ColumnDef("method"),
                ColumnDef("domain"),
                ColumnDef("path"),
                ColumnDef("headers", sql_type="TEXT"),        # JSON-encoded, redacted
                ColumnDef("body_preview", sql_type="TEXT"),    # first 64KB, or null
                ColumnDef("body_size_bytes", sql_type="INTEGER"),
                ColumnDef("initiator_type"),                  # script, parser, other
                ColumnDef("profile"),                         # proxy-st profile name
            ],
            indexes=["timestamp", "domain", "method"],
        ),
        TableSchema(
            name="http_responses",
            columns=[
                ColumnDef("request_id"),                      # FK to http_requests
                ColumnDef("url"),
                ColumnDef("status_code", sql_type="INTEGER"),
                ColumnDef("mime_type"),
                ColumnDef("headers", sql_type="TEXT"),         # JSON-encoded, redacted
                ColumnDef("body_preview", sql_type="TEXT"),    # first 64KB for JSON APIs
                ColumnDef("body_size_bytes", sql_type="INTEGER"),
                ColumnDef("response_ms", sql_type="INTEGER"), # timing from requestWillBeSent to responseReceived
                ColumnDef("profile"),
            ],
            indexes=["timestamp", "status_code", "request_id"],
        ),
        TableSchema(
            name="cookies",
            columns=[
                ColumnDef("domain"),
                ColumnDef("name"),
                ColumnDef("value"),                           # redacted for sensitive cookies
                ColumnDef("path"),
                ColumnDef("expires"),
                ColumnDef("http_only", sql_type="INTEGER"),   # boolean
                ColumnDef("secure", sql_type="INTEGER"),      # boolean
                ColumnDef("same_site"),
                ColumnDef("event_type"),                      # "snapshot" | "set" | "expired"
                ColumnDef("profile"),
            ],
            indexes=["timestamp", "domain", "name"],
        ),
        TableSchema(
            name="storage_snapshots",
            columns=[
                ColumnDef("origin"),                          # e.g. https://listen.tidal.com
                ColumnDef("storage_type"),                    # "localStorage" | "sessionStorage"
                ColumnDef("key"),
                ColumnDef("value", sql_type="TEXT"),           # may be large JSON blobs
                ColumnDef("value_size_bytes", sql_type="INTEGER"),
                ColumnDef("event_type"),                      # "snapshot" | "changed" | "removed"
                ColumnDef("profile"),
            ],
            indexes=["timestamp", "origin", "storage_type"],
        ),
        TableSchema(
            name="websocket_frames",
            columns=[
                ColumnDef("url"),
                ColumnDef("direction"),                       # "sent" | "received"
                ColumnDef("opcode"),                          # "text" | "binary"
                ColumnDef("payload_preview", sql_type="TEXT"), # first 64KB
                ColumnDef("payload_size_bytes", sql_type="INTEGER"),
                ColumnDef("profile"),
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
    ],
    dashboards={
        "requests_by_domain": (
            "SELECT domain, count(*) AS total, "
            "count(DISTINCT path) AS unique_paths "
            "FROM http_requests GROUP BY domain ORDER BY total DESC"
        ),
        "api_endpoints": (
            "SELECT method, domain, path, count(*) AS calls "
            "FROM http_requests "
            "WHERE domain LIKE '%tidal%' OR domain LIKE '%qobuz%' "
            "GROUP BY method, domain, path ORDER BY calls DESC"
        ),
        "response_codes": (
            "SELECT status_code, count(*) AS total "
            "FROM http_responses GROUP BY status_code ORDER BY total DESC"
        ),
        "cookie_inventory": (
            "SELECT domain, name, http_only, secure, same_site, "
            "max(timestamp) AS last_seen "
            "FROM cookies WHERE event_type='snapshot' "
            "GROUP BY domain, name ORDER BY domain, name"
        ),
        "cookie_lifecycle": (
            "SELECT name, domain, event_type, expires, timestamp "
            "FROM cookies WHERE domain LIKE '%tidal%' "
            "ORDER BY name, timestamp"
        ),
        "storage_keys": (
            "SELECT origin, storage_type, key, "
            "length(value) AS value_length, max(timestamp) AS last_seen "
            "FROM storage_snapshots "
            "GROUP BY origin, storage_type, key ORDER BY origin, key"
        ),
        "auth_token_flow": (
            "SELECT r.timestamp, r.method, r.url, "
            "resp.status_code, resp.response_ms "
            "FROM http_requests r "
            "JOIN http_responses resp ON r.request_id = resp.request_id "
            "WHERE r.url LIKE '%/auth/%' OR r.url LIKE '%/session%' "
            "OR r.url LIKE '%/token%' OR r.url LIKE '%/login%' "
            "ORDER BY r.timestamp"
        ),
        "slow_api_calls": (
            "SELECT resp.url, resp.status_code, resp.response_ms, "
            "resp.mime_type, resp.timestamp "
            "FROM http_responses resp "
            "WHERE resp.response_ms > 1000 ORDER BY resp.response_ms DESC LIMIT 50"
        ),
        "websocket_activity": (
            "SELECT url, direction, count(*) AS frames, "
            "sum(payload_size_bytes) AS total_bytes "
            "FROM websocket_frames GROUP BY url, direction"
        ),
        "session_timeline": (
            "SELECT timestamp, 'request' AS type, method || ' ' || url AS detail "
            "FROM http_requests "
            "WHERE domain LIKE '%tidal%' "
            "UNION ALL "
            "SELECT timestamp, 'cookie' AS type, name || '=' || substr(value,1,20) || '...' AS detail "
            "FROM cookies WHERE domain LIKE '%tidal%' AND event_type='set' "
            "ORDER BY timestamp LIMIT 200"
        ),
        # -- Browsing-observation dashboards (primary scenario) --
        "login_flow": (
            "SELECT r.timestamp, r.method, r.url, resp.status_code, resp.response_ms "
            "FROM http_requests r "
            "LEFT JOIN http_responses resp ON r.request_id = resp.request_id "
            "WHERE r.url LIKE '%/auth/%' OR r.url LIKE '%/login%' "
            "OR r.url LIKE '%/oauth%' OR r.url LIKE '%/token%' "
            "OR r.url LIKE '%/sessions%' "
            "ORDER BY r.timestamp"
        ),
        "token_refresh_pattern": (
            "SELECT timestamp, url, method "
            "FROM http_requests "
            "WHERE url LIKE '%refresh%' OR url LIKE '%token%' "
            "ORDER BY timestamp"
        ),
        "request_timing_distribution": (
            "SELECT domain, "
            "count(*) AS total_requests, "
            "CAST(avg(resp.response_ms) AS INTEGER) AS avg_ms, "
            "max(resp.response_ms) AS max_ms, "
            "min(resp.response_ms) AS min_ms "
            "FROM http_requests r "
            "JOIN http_responses resp ON r.request_id = resp.request_id "
            "GROUP BY domain ORDER BY total_requests DESC"
        ),
        "inter_request_gaps": (
            "SELECT domain, "
            "CAST(avg( "
            "  (julianday(r2.timestamp) - julianday(r1.timestamp)) * 86400000 "
            ") AS INTEGER) AS avg_gap_ms "
            "FROM http_requests r1 "
            "JOIN http_requests r2 ON r2.rowid = r1.rowid + 1 "
            "AND r1.domain = r2.domain "
            "WHERE r1.domain LIKE '%tidal%' OR r1.domain LIKE '%qobuz%' "
            "GROUP BY domain"
        ),
        "fingerprint_signals": (
            "SELECT key, length(value) AS size, timestamp "
            "FROM storage_snapshots "
            "WHERE key LIKE '%_dd_%' OR key LIKE '%datadome%' "
            "OR key LIKE '%fingerprint%' OR key LIKE '%canvas%' "
            "OR key LIKE '%webgl%' OR key LIKE '%device%' "
            "ORDER BY timestamp"
        ),
        "playback_events": (
            "SELECT timestamp, direction, substr(payload_preview, 1, 200) AS preview "
            "FROM websocket_frames "
            "WHERE url LIKE '%tidal%' OR url LIKE '%qobuz%' "
            "ORDER BY timestamp LIMIT 100"
        ),
    },
)
```

**Entry point registration** (in `pyproject.toml`):

```toml
[project.optional-dependencies]
capture = ["telemetry-monitor", "websockets>=12.0"]

[project.entry-points."telemetry_monitor.schema"]
proxy-relay = "proxy_relay.capture.schema:PROXY_RELAY_SCHEMA"
```

### Integration with `browse.py`

Changes to existing code are minimal:

**`_chrome_args()`** — add CDP port when capturing:

```python
def _chrome_args(
    chromium_path, profile_dir, *,
    proxy_host=None, proxy_port=None, timezone=None,
    cdp_port=None,  # NEW
):
    cmd = [...]  # existing flags

    if cdp_port is not None:
        cmd.append(f"--remote-debugging-port={cdp_port}")

    return cmd, env
```

**`BrowseSupervisor`** — add optional capture session:

```python
class BrowseSupervisor:
    def __init__(self, ..., capture_session: CaptureSession | None = None):
        self._capture = capture_session

    def run(self) -> int:
        # ... existing startup ...

        if self._capture is not None:
            # Start capture in a background thread running its own asyncio loop
            capture_thread = threading.Thread(
                target=self._run_capture, name="cdp-capture", daemon=True
            )
            capture_thread.start()

        # ... existing main loop (unchanged) ...

        # On exit:
        if self._capture is not None:
            asyncio.run(self._capture.stop())

    def _run_capture(self) -> None:
        """Background thread: run the CDP capture session."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._capture.start(self._cdp_port))
            loop.run_until_complete(self._capture.run_until_stopped())
        except Exception as exc:
            log.warning("CDP capture error: %s", exc)
        finally:
            loop.close()
```

### Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Captured data contains auth tokens | `Authorization` header values redacted to first 10 chars + `...` in storage; full values never written |
| Cookie values may contain session tokens | Cookie values for `httpOnly` cookies stored as SHA-256 hash only (presence tracking, not value extraction) |
| Response bodies may contain PII | Bodies only captured for JSON responses from allowlisted domains; truncated at 64KB |
| Capture DB on disk | Stored at `~/.config/proxy-relay/capture.db` with `chmod 0600`; never committed to git |
| CDP port open on localhost | Bound to `127.0.0.1` only; random port (not fixed 9222) to avoid port conflicts |

### Data Flow Diagram

```
  Chromium (with --remote-debugging-port)
      |
      | CDP WebSocket (ws://127.0.0.1:<port>)
      |
  CdpClient (cdp_client.py)
      |
      |-- Network.requestWillBeSent ----+
      |-- Network.responseReceived -----+
      |-- Network.getCookies (polled) --+---> CaptureCollector (collector.py)
      |-- Runtime.evaluate (polled) ----+        |
      |-- webSocketFrameSent/Received --+        |-- domain filter (allowlist)
      |                                          |-- header redaction
      |                                          |-- body truncation
      |                                          |-- diff detection (cookies, storage)
      |                                          |
      |                                     BackgroundWriter (telemetry-monitor)
      |                                          |
      |                                     SQLite (capture.db)
      |                                          |
      |                                     5 tables: http_requests, http_responses,
      |                                               cookies, storage_snapshots,
      |                                               websocket_frames
      |
      |  (traffic path — completely separate, untouched)
      |
  Chromium --HTTP CONNECT--> proxy-relay --> SOCKS5 --> target
```

### Dependency Impact

| Dependency | Size | Required? | Purpose |
|-----------|------|-----------|---------|
| `websockets` | ~150KB | Yes (capture extra) | CDP WebSocket client |
| `telemetry-monitor` | Already in ecosystem | Yes (capture extra) | BackgroundWriter + SQLite storage |

**No new dependencies for users who don't use `--capture`.** The `capture/` subpackage uses lazy imports — `ImportError` is caught gracefully if telemetry-monitor or websockets aren't installed.

### CLI Usage

**Typical workflow — observe a TIDAL browsing session:**

```bash
# 1. Browse TIDAL normally with capture enabled
proxy-relay browse --capture --profile us-browse
#    -> Opens Chromium, user logs into listen.tidal.com
#    -> Browses, plays music, manages library
#    -> CDP captures everything silently in the background
#    -> User closes browser when done

# 2. Analyze the captured session
telemetry-monitor query --schema proxy-relay --dashboard login_flow
telemetry-monitor query --schema proxy-relay --dashboard api_endpoints
telemetry-monitor query --schema proxy-relay --dashboard cookie_lifecycle
telemetry-monitor query --schema proxy-relay --dashboard fingerprint_signals
telemetry-monitor query --schema proxy-relay --dashboard playback_events
telemetry-monitor query --schema proxy-relay --dashboard token_refresh_pattern

# 3. Explore specific questions
telemetry-monitor query --schema proxy-relay --sql \
  "SELECT url, method, headers FROM http_requests WHERE url LIKE '%streamUrl%'"

telemetry-monitor query --schema proxy-relay --sql \
  "SELECT name, domain, expires, http_only FROM cookies WHERE domain LIKE '%tidal%'"
```

**Qobuz session (same workflow, different site):**

```bash
proxy-relay browse --capture --profile us-browse
#    -> User navigates to qobuz.com, logs in, browses catalog
#    -> Captures Qobuz auth flow, signed URL construction, API schemas

telemetry-monitor query --schema proxy-relay --dashboard auth_token_flow
telemetry-monitor query --schema proxy-relay --sql \
  "SELECT url, headers FROM http_requests WHERE domain LIKE '%qobuz%' AND url LIKE '%get_file_url%'"
```

**Custom domain filter (reduce noise):**

```bash
# Only capture TIDAL domains (ignore ads, analytics, CDNs)
proxy-relay browse --capture --capture-domains tidal.com,login.tidal.com

# Only capture Qobuz
proxy-relay browse --capture --capture-domains qobuz.com,play.qobuz.com
```

**Multiple sessions build a richer dataset:**

```bash
# Session 1: login + browse (observe auth flow)
# Session 2: play 10 tracks (observe playback events + token refresh)
# Session 3: idle for 2 hours (observe session maintenance + heartbeats)
# All captured to the same capture.db — query across all sessions
```

---

## Next Steps

1. **Evaluate `websockets` library** — confirm async compatibility, size, maintenance status
2. **Prototype CDP client** — connect to a running Chromium, subscribe to `Network.requestWillBeSent`, print captured URLs
3. **Implement domain filter** — allowlist matching for `*.tidal.com`, `*.qobuz.com` with wildcard support
4. **Build schema + entry point** — register `PROXY_RELAY_SCHEMA` with telemetry-monitor
5. **Integrate with `browse.py`** — `--capture` flag, CDP port injection, `CaptureSession` lifecycle
6. **Test with live TIDAL session** — validate cookie capture, API endpoint discovery, storage polling
7. **Build analysis dashboards** — SQL queries for auth flow analysis, cookie lifecycle, API mapping
