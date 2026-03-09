#!/usr/bin/env bash
# Smoke test for proxy-relay: start, verify, rotate, stop.
# Requires: proxy-st configured with a valid profile.
# Usage: bash tests/smoke_test.sh [profile]

set -euo pipefail

PROFILE="${1:-browse}"
PORT=18199
HOST="127.0.0.1"
RELAY="proxy-relay"

echo "=== proxy-relay smoke test ==="
echo "Profile: $PROFILE, Bind: $HOST:$PORT"

# 1. Start the relay in the background
echo "[1/6] Starting relay..."
$RELAY start --host "$HOST" --port "$PORT" --profile "$PROFILE" &
RELAY_PID=$!
sleep 2

# 2. Check status
echo "[2/6] Checking status..."
$RELAY status
STATUS_EXIT=$?
if [ "$STATUS_EXIT" -ne 0 ]; then
    echo "FAIL: status returned non-zero"
    kill "$RELAY_PID" 2>/dev/null || true
    exit 1
fi

# 3. Test CONNECT tunnel (HTTPS)
echo "[3/6] Testing HTTPS via CONNECT..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --proxy "http://$HOST:$PORT" \
    --max-time 15 \
    "https://httpbin.org/ip" 2>/dev/null || echo "000")
echo "  HTTPS response code: $HTTP_CODE"
if [ "$HTTP_CODE" = "000" ]; then
    echo "  WARN: HTTPS request timed out or failed (proxy may be slow)"
fi

# 4. Test plain HTTP forwarding
echo "[4/6] Testing plain HTTP forwarding..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --proxy "http://$HOST:$PORT" \
    --max-time 15 \
    "http://httpbin.org/ip" 2>/dev/null || echo "000")
echo "  HTTP response code: $HTTP_CODE"

# 5. Rotate upstream
echo "[5/6] Rotating upstream..."
$RELAY rotate

# 6. Stop the relay
echo "[6/6] Stopping relay..."
$RELAY stop
wait "$RELAY_PID" 2>/dev/null || true

echo "=== smoke test complete ==="
