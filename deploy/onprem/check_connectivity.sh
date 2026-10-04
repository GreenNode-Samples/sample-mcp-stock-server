#!/usr/bin/env bash
# Check connectivity to the MCP server (on-prem or anywhere) - run it from a host in the customer's VPC.
# Needs only bash + curl.
#
# Usage:
#   MCP_API_KEY=<key> ./check_connectivity.sh <host> [port] [scheme]
#   MCP_API_KEY=<key> ./check_connectivity.sh xx.xx.x.x 8443 https
#
# Environment: MCP_API_KEY (required for step 3), INSECURE=1 (skip certificate verification, for internal certs),
#              TIMEOUT (whole seconds, default 5)
#
# curl ignores http_proxy / https_proxy here (the target is on a private network), and the API key is
# passed to curl on stdin, so it never appears in the process list.
set -u

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

HOST="${1:-}"
PORT="${2:-8080}"
SCHEME="${3:-http}"
TIMEOUT="${TIMEOUT:-5}"

if [[ -z "$HOST" || "$HOST" == "-h" || "$HOST" == "--help" ]]; then
  usage
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required"; exit 2; }
[[ "$TIMEOUT" =~ ^[0-9]+$ ]] || { echo "FAIL: TIMEOUT must be a whole number of seconds"; exit 2; }

BASE="${SCHEME}://${HOST}:${PORT}"
CURL_OPTS=(-sS --noproxy '*' --max-time "$TIMEOUT")
if [[ "${INSECURE:-0}" == "1" ]]; then
  CURL_OPTS+=(-k)
fi

fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() {
  printf 'FAIL  %s\n' "$1"
  if [[ -n "${2:-}" ]]; then
    printf '      -> %s\n' "$2"
  fi
  fails=$((fails + 1))
}

echo "Target: ${BASE}/mcp"
echo "-----------------------------------------------"

# ---- Step 1: TCP reachability ----
# Plain bash /dev/tcp. HOST and PORT are passed to the inner bash as arguments (never spliced into the command
# string) and a small watchdog enforces TIMEOUT, so no `timeout` binary is needed (macOS has none).
# Only exit code 0 counts as success.
tcp_probe() {
  bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$1" "$2" 2>/dev/null &
  local pid=$! i
  for ((i = 0; i < TIMEOUT * 10; i++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      return $?
    fi
    sleep 0.1
  done
  kill "$pid" 2>/dev/null
  wait "$pid" 2>/dev/null
  return 1
}
tcp_ok=0
if tcp_probe "$HOST" "$PORT"; then
  tcp_ok=1
fi
if [[ $tcp_ok -eq 1 ]]; then
  pass "[1/3] TCP ${HOST}:${PORT} reachable"
else
  fail "[1/3] TCP ${HOST}:${PORT} cannot connect" \
       "check the route (VPN/Interconnect), the firewall / security group for source 172.30.0.0/16, and that the service is listening"
fi

# ---- Step 2: GET /health (no key needed) ----
if [[ $tcp_ok -eq 1 ]]; then
  body=$(curl "${CURL_OPTS[@]}" -o - -w '\n%{http_code}' "${BASE}/health" 2>&1)
  code=$(printf '%s' "$body" | tail -n1)
  if [[ "$code" == "200" ]]; then
    pass "[2/3] GET /health -> 200"
  else
    fail "[2/3] GET /health -> ${code:-no response}" \
         "for a TLS error set INSECURE=1 or install the internal CA; check the scheme and port (${SCHEME}:${PORT})"
  fi
else
  fail "[2/3] GET /health skipped (step 1 failed)"
fi

# ---- Step 3: authenticated tools/list ----
if [[ -z "${MCP_API_KEY:-}" ]]; then
  fail "[3/3] tools/list skipped" "set MCP_API_KEY=<key> and run again"
elif [[ $tcp_ok -ne 1 ]]; then
  fail "[3/3] tools/list skipped (step 1 failed)"
else
  # Quote the key for a curl config file (backslash and double quote are escaped).
  key_escaped="${MCP_API_KEY//\\/\\\\}"
  key_escaped="${key_escaped//\"/\\\"}"
  resp=$(printf 'header = "X-Api-Key: %s"\n' "$key_escaped" | curl "${CURL_OPTS[@]}" -K - -X POST "${BASE}/mcp" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    -w '\n%{http_code}' 2>&1)
  code=$(printf '%s' "$resp" | tail -n1)
  out=$(printf '%s' "$resp" | sed '$d')
  case "$code" in
    200)
      if printf '%s' "$out" | grep -q '"tools"'; then
        pass "[3/3] POST /mcp tools/list -> 200 (tools listed)"
      else
        fail "[3/3] POST /mcp -> 200 but no \"tools\" in the response" "${out:0:200}"
      fi ;;
    401) fail "[3/3] POST /mcp -> 401" "API key wrong or missing - compare it with MCP_API_KEYS on the server" ;;
    503) fail "[3/3] POST /mcp -> 503" "the server has no valid MCP_API_KEYS (missing, a placeholder, or shorter than 32 characters) - see its log" ;;
    *)   fail "[3/3] POST /mcp -> ${code:-no response}" "${out:0:200}" ;;
  esac
fi

echo "-----------------------------------------------"
if [[ $fails -eq 0 ]]; then
  echo "RESULT: PASS (3/3)"
  exit 0
fi
echo "RESULT: FAIL (${fails} failed steps)"
exit 1
