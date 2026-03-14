# Proxy-Relay Flow Diagram

proxy-relay is a local HTTP CONNECT proxy that forwards all traffic through an upstream SOCKS5 proxy managed by proxy-st. It provides three main flows: **Server Lifecycle** (start, accept, handle connections), **Connection Handling** (CONNECT tunnels, plain HTTP forwarding, health checks), and **Browse Supervision** (auto-start server, launch Chromium with proxy and timezone spoofing, auto-rotate upstream).

Key security properties:
- **DNS leak prevention**: All hostnames are resolved remotely via SOCKS5 (`rdns=True`, ATYP=0x03). Local DNS is never touched.
- **Header sanitization**: Privacy-leaking headers (`X-Forwarded-For`, `Via`, `Proxy-Authorization`, etc.) are stripped from forwarded HTTP requests.
- **Timezone spoofing**: The `TZ` environment variable is set on Chromium to match the proxy exit country, defeating JavaScript timezone fingerprinting.

```mermaid
flowchart TD
    classDef entry fill:#3b82f6,color:#fff,stroke:#1e40af
    classDef success fill:#22c55e,color:#fff,stroke:#15803d
    classDef error fill:#ef4444,color:#fff,stroke:#b91c1c
    classDef config fill:#f97316,color:#fff,stroke:#c2410c
    classDef external fill:#a855f7,color:#fff,stroke:#7e22ce

    subgraph Server["Server Lifecycle"]
        CLI_START["proxy-relay start --profile NAME"]:::entry
        LOAD_CFG["RelayConfig.load"]:::config
        UPSTREAM["UpstreamManager<br/>proxy-st config + SessionStore"]:::external
        BUILD_URL["proxy_st.url.build_url<br/>sticky SOCKS5 session"]:::external
        TZ_CHECK{"TZ mismatch?"}
        TZ_WARN["warn: TZ outside country"]:::error
        START_SRV["asyncio.start_server"]
        PID_STATUS["Write PID + status files"]:::config
        SIGNALS["SIGTERM=stop SIGUSR1=rotate"]
        ACCEPT["Accept loop"]:::success

        CLI_START --> LOAD_CFG --> UPSTREAM --> BUILD_URL --> TZ_CHECK
        TZ_CHECK -->|mismatch| TZ_WARN --> START_SRV
        TZ_CHECK -->|ok| START_SRV
        START_SRV --> PID_STATUS --> SIGNALS --> ACCEPT
    end

    subgraph Connections["Connection Handling"]
        CLIENT["Client request"]:::entry
        DISPATCH{"Method + path?"}
        HEALTH["GET /__health<br/>JSON response"]:::success
        CONNECT["CONNECT host:port"]
        HTTP_FWD["Plain HTTP request"]

        TUNNEL["tunnel.py<br/>python-socks rdns=True<br/>SOCKS5 remote DNS"]
        RELAY["Bidirectional relay"]
        SANITIZE["sanitizer.py<br/>Strip leaky headers"]:::config
        FWD_SOCKS["forwarder.py<br/>Forward via SOCKS5"]

        MONITOR["monitor.py<br/>Record outcome"]
        ROLL_WIN{"Error threshold<br/>exceeded?"}
        AUTO_ROT["Auto-rotate upstream"]:::error

        CLIENT --> DISPATCH
        DISPATCH -->|/__health| HEALTH
        DISPATCH -->|CONNECT| CONNECT --> TUNNEL --> RELAY
        DISPATCH -->|plain HTTP| HTTP_FWD --> SANITIZE --> FWD_SOCKS
        RELAY --> MONITOR
        FWD_SOCKS --> MONITOR
        MONITOR --> ROLL_WIN
        ROLL_WIN -->|exceeded| AUTO_ROT --> UPSTREAM
        ROLL_WIN -->|ok| ACCEPT
    end

    subgraph Browse["Browse Supervisor"]
        CLI_BROWSE["proxy-relay browse --profile NAME"]:::entry
        SRV_CHECK{"Server running?"}
        AUTO_START["auto_start_server<br/>subprocess port=0"]
        WAIT_READY["wait_for_server_ready<br/>poll status file"]
        REUSE["Reuse existing server"]:::success
        HC["health_check<br/>GET /__health"]:::success
        FIND_CHROME["find_chromium"]
        TZ_SPOOF["TZ = country timezone"]:::config
        LAUNCH["Launch Chromium<br/>--proxy-server --user-data-dir"]
        POLL_RELAY["Thread: poll relay<br/>every 2s"]
        ROT_LOOP["Thread: SIGUSR1<br/>every N min"]
        CHROME_EXIT{"Chromium exited?"}
        AUTO_STOP["auto_stop_server<br/>SIGTERM then SIGKILL"]:::error

        CLI_BROWSE --> SRV_CHECK
        SRV_CHECK -->|no| AUTO_START --> WAIT_READY --> HC
        SRV_CHECK -->|yes| REUSE --> HC
        HC --> FIND_CHROME --> TZ_SPOOF --> LAUNCH
        LAUNCH --> POLL_RELAY
        LAUNCH --> ROT_LOOP
        POLL_RELAY --> CHROME_EXIT
        CHROME_EXIT -->|auto-started| AUTO_STOP
        CHROME_EXIT -->|relay died| AUTO_STOP
    end

    ACCEPT --> CLIENT
    AUTO_ROT -.-> BUILD_URL
    HC -.-> HEALTH
    ROT_LOOP -.-> SIGNALS
```
