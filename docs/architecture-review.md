# Architecture Review — proxy-relay

**Last review:** 2026-03-20 (Security Research — PR series: proxy-relay-specific security review)
**Prior review:** 2026-03-16 (Batch G)
**Batch J:** 15 architecture findings resolved (S85 high/medium + S87 low-severity sweep).
**Security research:** 10 findings (2 high, 3 medium, 5 low) — PR-1 through PR-10. Planned for S88.
**Batch G:** 15 findings — all resolved 2026-03-16.
**S82-S83:** All prior findings resolved. No carryover.

Completed items in `docs/architecture-review-done.md`.

---

## Sprint Plan

| Sprint | Items | Theme | Effort | Status |
|--------|-------|-------|--------|--------|
| ~~S88~~ | PR-1 through PR-10 | Security research: WebRTC leaks, DataDome, header sanitization, browser fingerprint | M | done |

---

## Active Findings — Security Research (2026-03-20)

### Context

Full security-focused research review of proxy-relay's attack surface. Each finding
was identified by reading all production modules and cross-referencing against current
web intelligence on DataDome detection, browser fingerprinting, proxy detection, and
header leak vectors. Sources cited inline.

### PR-1 — WebRTC real-IP leak via STUN/UDP bypass

| Field | Value |
|-------|-------|
| **Priority** | 1 |
| **Severity** | high |
| **Effort** | XS |
| **Status** | done |

**Problem:** WebRTC STUN requests use **UDP**, which bypasses the HTTP/SOCKS5 proxy
tunnel entirely. Any website running JavaScript can trigger a STUN request to a Google
STUN server (e.g., `stun.l.google.com:19302`) and receive the user's real public IP in
the ICE candidate response — completely defeating the proxy.

**Current mitigation:** `browse.py:164-165` passes `--disable-webrtc-stun-origin` and
`--enforce-webrtc-ip-permission-check`. Research from 2025-2026 confirms these flags
are **unreliable** — they were designed for privacy permissions UI, not for blocking
STUN traffic. Multiple sources (Chameleon WebRTC 2026, RoundProxies WebRTC guide)
document that real IPs still leak through these flags.

**Affected file:** `proxy_relay/browse.py` — `_chrome_args()` function.

**Fix:** Replace the two unreliable flags with `--webrtc-ip-handling-policy=disable_non_proxied_udp`.
This is a Chrome enterprise policy flag that tells WebRTC to **only use the proxy's IP**
for ICE candidates. WebRTC still functions (video calls work) but never exposes the
local IP. Verify post-fix with `browserleaks.com/webrtc`.

**Sources:**
- [Chameleon: WebRTC leak prevention 2026](https://chameleonmode.com/webrtc/)
- [RoundProxies: WebRTC leak guide](https://roundproxies.com/blog/webrtc-leaks/)

---

### PR-2 — CDP (Chrome DevTools Protocol) is a DataDome detection signal

| Field | Value |
|-------|-------|
| **Priority** | 1 |
| **Severity** | high |
| **Effort** | S |
| **Status** | done |

**Problem:** When `proxy-relay browse --capture` is used, `browse.py:179` passes
`--remote-debugging-port=PORT` to Chromium, enabling CDP. DataDome's client-side
JavaScript specifically detects CDP artifacts:

1. **`Runtime.enable` detection** — DataDome detects when CDP's `Runtime.enable` has
   been called, which happens automatically when any CDP client connects.
2. **Console message buffering** — Chrome changes its console message handling when
   DevTools/CDP are connected, and DataDome fingerprints this behavioral change.
3. **`$cdc_` reference** — CDP injects `document.$cdc_asdjflasutopfhvcZLmcfl_` into
   the page DOM, which DataDome checks for.

This means `--capture` sessions have **elevated bot detection risk** even with a valid
datadome cookie.

**What we do correctly:** `warmup.py` deliberately avoids CDP — it launches Chromium
WITHOUT `--remote-debugging-port` and polls the Cookies SQLite file directly. This is
the correct approach for trust-critical sessions.

**Affected files:** `proxy_relay/browse.py`, `proxy_relay/capture/__init__.py` (usage
context), CLI help text.

**Fix:**
1. Add CLI warning when `--capture` is used without an existing datadome cookie.
2. Document the two-phase workflow: warmup first (no CDP → builds trust), then capture
   can ride on the established trust.
3. Log a warning at capture session start: "CDP active — elevated DataDome detection risk."

**Sources:**
- [DataDome: CDP Signal detection](https://datadome.co/threat-research/how-new-headless-chrome-the-cdp-signal-are-impacting-bot-detection/)
- [ZenRows: AutomationControlled flag analysis](https://www.zenrows.com/blog/disable-blink-features-automationcontrolled)

---

### PR-3 — Missing proxy-revealing headers in sanitizer

| Field | Value |
|-------|-------|
| **Priority** | 2 |
| **Severity** | medium |
| **Effort** | XS |
| **Status** | done |

**Problem:** `sanitizer.py` strips 8 known proxy-revealing headers (`X-Forwarded-For`,
`Via`, `Proxy-Authorization`, etc.) and 8 hop-by-hop headers. But several additional
headers used by CDNs and reverse proxies to communicate client IP are not stripped:

| Header | Used by |
|--------|---------|
| `X-Proxy-Connection` | Non-standard, some legacy clients |
| `Client-IP` | Various load balancers |
| `True-Client-IP` | Cloudflare |
| `CF-Connecting-IP` | Cloudflare |
| `X-Cluster-Client-IP` | Cluster/LB setups |
| `X-Original-Forwarded-For` | Reverse proxy chains |
| `X-ProxyUser-Ip` | Google internal proxies |

If any of these headers exist in the request (from browser extensions, VPN software, or
upstream proxy layers), they pass through unstripped and could reveal the real IP to
TIDAL/Qobuz servers.

**Affected file:** `proxy_relay/sanitizer.py` — `_STRIP_HEADERS` frozenset.

**Fix:** Add all 7 headers to `_STRIP_HEADERS`. Zero risk — stripping headers that
shouldn't exist has no side effects.

**Sources:**
- [GitHub: IP bypass headers reference](https://gist.github.com/kaimi-/6b3c99538dce9e3d29ad647b325007c1)

---

### PR-4 — Snap Chromium TLS fingerprint divergence

| Field | Value |
|-------|-------|
| **Priority** | 2 |
| **Severity** | medium |
| **Effort** | S |
| **Status** | done |

**Problem:** Anti-bot systems fingerprint the TLS handshake (JA3/JA4 hash) and
cross-reference it against the claimed User-Agent. Snap-packaged Chromium
(`/snap/bin/chromium`) is compiled separately from Google Chrome — different compiler
flags, different BoringSSL version — producing a **different TLS fingerprint** than
native Chrome. But the User-Agent still claims "Chrome/132".

DataDome cross-references:
- UA says "Chrome 132" → expected JA3 hash: `abc123`
- Actual TLS fingerprint from Snap build → JA3 hash: `xyz789`
- **Mismatch → elevated bot score**

**Affected file:** `proxy_relay/browse.py` — `find_chromium()` function.

**Fix:**
1. Detect Snap binary in `find_chromium()` (path contains `/snap/`).
2. Log a warning: "Snap Chromium detected — native Chrome/Brave recommended for lower
   detection risk (TLS fingerprint divergence)."
3. Document in README's browser selection section.

**Sources:**
- [Chameleon: Browser fingerprinting 2026](https://chameleonmode.com/browser-detection-fingerprinting-2026/)
- [Fingerprint.com: Device Intelligence Report 2026](https://fingerprint.com/blog/device-intelligence-report-2026/)

---

### PR-9 — No IPv6 leak prevention

| Field | Value |
|-------|-------|
| **Priority** | 2 |
| **Severity** | medium |
| **Effort** | XS |
| **Status** | done |

**Problem:** If the system has IPv6 connectivity, Chromium may make connections via
IPv6 (DNS AAAA records, direct connections, WebRTC). The SOCKS5 proxy only tunnels
IPv4. An IPv6 connection bypasses the proxy entirely, revealing the real IPv6 address.

Even if IPv4 and IPv6 addresses differ, correlation is possible: "User claims Colombia
(IPv4 via proxy) but has an IPv6 address from a German ISP" → geo-jump detection.

**Affected file:** `proxy_relay/browse.py` — `_chrome_args()` function.

**Fix:** Add `--disable-ipv6` to `_chrome_args()`. This tells Chromium to never resolve
or connect via IPv6. All traffic goes through IPv4, which is properly tunneled.

**Note:** The related flag `--host-resolver-rules` was previously removed because it
triggered DataDome's JS challenge (see MEMORY.md). `--disable-ipv6` is less invasive
and does not interfere with DNS resolution behavior.

---

### PR-5 — AutomationControlled yellow info bar

| Field | Value |
|-------|-------|
| **Priority** | 3 |
| **Severity** | low |
| **Effort** | XS |
| **Status** | done |

**Problem:** `browse.py` passes `--disable-blink-features=AutomationControlled` to
prevent `navigator.webdriver = true` (a clear bot signal). Starting with Chrome 2026
builds, this flag triggers a persistent yellow info bar: "You are using an unsupported
command-line flag."

Risks: (1) visually confusing to the user, (2) DataDome's client-side JS could
theoretically detect the info bar DOM element.

**Affected file:** `proxy_relay/browse.py` — `_chrome_args()` function.

**Fix:** Add `--disable-infobars` to suppress the yellow bar. The
`AutomationControlled` flag continues to work — just no visual warning about it.

---

### PR-6 — Health endpoint accessible without authentication

| Field | Value |
|-------|-------|
| **Priority** | 3 |
| **Severity** | low |
| **Effort** | S |
| **Status** | done |

**Problem:** `handler.py:71` responds to `GET /__health` with JSON containing the exit
IP and upstream status. The server binds to `127.0.0.1` by default (safe), but if
`server.host` is set to `0.0.0.0`, the health endpoint is accessible to any network
peer, exposing proxy infrastructure details.

**Affected file:** `proxy_relay/handler.py` — health check handler.

**Fix:** Add a client IP check in the health handler: if the server is bound to a
non-loopback address, reject health requests from non-loopback clients with 403.
Alternatively, always restrict `/__health` to loopback source IPs.

---

### PR-7 — Status file contains upstream URL

| Field | Value |
|-------|-------|
| **Priority** | 4 |
| **Severity** | low |
| **Effort** | XS |
| **Status** | done |

**Problem:** `pidfile.py` writes the masked upstream URL (host:port visible, password
masked) to `.status.json`. File permissions are 0o600 (owner-only), but on shared
systems with permissive home directory permissions (0o755), other users could read it.

**Affected file:** `proxy_relay/pidfile.py` — `write_status()`.

**Fix:** Document the existing mitigation (0o600 permissions) with an inline comment.
The risk is already low — no code change strictly needed.

---

### PR-8 — DataDome cookie server-side revocation not detected

| Field | Value |
|-------|-------|
| **Priority** | 3 |
| **Severity** | low |
| **Effort** | S |
| **Status** | done |

**Problem:** `profile_rules.py`'s `DatadomeCookieNotExpired` rule checks the cookie's
`expires_utc` in Chromium's SQLite. But DataDome can **revoke a cookie server-side**
before its client-side expiry — e.g., after detecting suspicious behavior or IP
rotation. The client still has the cookie, it looks valid locally, but the server
rejects it on next use.

A profile can pass validation (cookie exists, not expired) but still get blocked.

**Affected file:** `proxy_relay/profile_rules.py` — `DatadomeCookieNotExpired` rule.

**Fix:** Add a "freshness" heuristic: if the datadome cookie is older than 7 days, add
a warning to the validation report suggesting re-warmup. Document that IP rotation may
invalidate existing cookies regardless of expiry.

**Sources:**
- [DataDome: Cookie storage and session management](https://docs.datadome.co/docs/cookie-session-storage)
- [DataDome: Cookies policy](https://datadome.co/cookies-policy/)

---

### PR-10 — Potential credential leak in SOCKS5 error messages

| Field | Value |
|-------|-------|
| **Priority** | 3 |
| **Severity** | low |
| **Effort** | S |
| **Status** | done |

**Problem:** When a SOCKS5 connection fails, `python-socks` raises an exception.
`tunnel.py:117-118` wraps it in a `TunnelError` with the original exception message.
If `python-socks` includes the SOCKS5 URL (containing credentials) in the error
message, those credentials would appear in log files.

**Affected file:** `proxy_relay/tunnel.py` — exception wrapping.

**Fix:** Audit `python-socks` exception messages. If credentials are included, sanitize
the exception message (strip content after `@`) before wrapping in `TunnelError`. If
not included, add a comment documenting the audit result.

---

## Backlog — New Items

### K1 — Configurable browser feature flags

| Field | Value |
|-------|-------|
| **Id** | K1 |
| **Priority** | 3 |
| **Severity** | medium |
| **Effort** | M |
| **Status** | backlog |

**Problem:** Browser launch flags (Widevine, WebRTC policy, disable-infobars, etc.)
are hardcoded in `browse.py`. Users cannot enable/disable features per profile without
editing source code.

**Proposed solution:** Add a `browser_flags` list to `ProfileConfig`:

```toml
[profiles.medellin]
browser_flags = ["--enable-widevine", "--disable-background-networking"]
```

`browse.py` appends these flags to the Chromium launch command. Default profile
provides sensible defaults (current hardcoded flags). Named profiles can override.

**Affected files:** `proxy_relay/config.py` (ProfileConfig), `proxy_relay/browse.py`
(flag construction), `tests/unit/test_browse.py`, `tests/unit/test_config.py`.

---

### K2 — Telemetry logging for migration not-found tracks

| Field | Value |
|-------|-------|
| **Id** | K2 |
| **Priority** | 4 |
| **Severity** | low |
| **Effort** | S |
| **Status** | backlog |

**Problem:** `playlist-migrate` and `library-migrate` print not-found tracks to
the console but don't persist them. Once the terminal session is closed, the
information is lost.

**Proposed solution:** When telemetry is enabled, emit `migration.not_found` events
with ISRC, artist, title, source provider, and timestamp. These can be queried later
via `telemetry-monitor` dashboards or direct SQLite queries.

**Affected files:** `tidal_dl/commands/library_migrate.py`, `tidal_dl/commands/playlist_migrate.py`,
`tidal_dl/telemetry/schema.py` (new event route).

---

## Column Definitions

| Column | Values / Description |
|--------|----------------------|
| **Id** | Prefix = series (PR = security research, J-RL = Batch J architecture). |
| **Priority** | 1 (critical) to 5 (nice-to-have). |
| **Severity** | `critical` / `high` / `medium` / `low`. |
| **Effort** | `XS` (< 15 min) / `S` (15-60 min) / `M` (1-3 hrs) / `L` (3-8 hrs) / `XL` (8+ hrs). |
| **Status** | `backlog` / `planned` / `in-progress` / `done`. |
