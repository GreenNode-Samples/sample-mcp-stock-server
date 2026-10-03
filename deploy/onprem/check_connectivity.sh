#!/usr/bin/env bash
# Kiểm tra kết nối tới MCP server (on-prem hoặc bất kỳ đâu) — chạy từ một host trong VPC khách hàng.
# Chỉ cần bash + curl.
#
# Dùng:
#   MCP_API_KEY=<key> ./check_connectivity.sh <host> [port] [scheme]
#   MCP_API_KEY=<key> ./check_connectivity.sh xx.xx.x.x 8443 https
#
# Biến môi trường: MCP_API_KEY (bắt buộc cho bước 3), INSECURE=1 (bỏ qua xác thực cert, khi dùng cert nội bộ),
#                  TIMEOUT (giây, mặc định 5)
set -u

HOST="${1:-}"
PORT="${2:-8080}"
SCHEME="${3:-http}"
TIMEOUT="${TIMEOUT:-5}"

if [[ -z "$HOST" || "$HOST" == "-h" || "$HOST" == "--help" ]]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: cần cài curl"; exit 2; }

BASE="${SCHEME}://${HOST}:${PORT}"
CURL_OPTS=(-sS --max-time "$TIMEOUT")
[[ "${INSECURE:-0}" == "1" ]] && CURL_OPTS+=(-k)

fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '      -> %s\n' "$2"; fails=$((fails + 1)); }

echo "Mục tiêu: ${BASE}/mcp"
echo "-----------------------------------------------"

# ---- Bước 1: TCP reachability (bash /dev/tcp, không cần nc) ----
tcp_ok=0
if command -v timeout >/dev/null 2>&1; then
  timeout "$TIMEOUT" bash -c "exec 3<>/dev/tcp/${HOST}/${PORT}" 2>/dev/null && tcp_ok=1
else
  # macOS không có `timeout`: dùng curl để thử bắt tay TCP
  curl -sS --max-time "$TIMEOUT" --connect-timeout "$TIMEOUT" -o /dev/null "http://${HOST}:${PORT}/" 2>/dev/null
  rc=$?
  # rc 7 (refused) / 28 (timeout) / 6 (DNS) = không tới được; các mã khác = đã kết nối được TCP
  [[ $rc -ne 7 && $rc -ne 28 && $rc -ne 6 ]] && tcp_ok=1
fi
if [[ $tcp_ok -eq 1 ]]; then
  pass "[1/3] TCP ${HOST}:${PORT} reachable"
else
  fail "[1/3] TCP ${HOST}:${PORT} không kết nối được" \
       "kiểm tra route (VPN/Interconnect), firewall/security group cho nguồn 172.30.0.0/16, và dịch vụ đang listen"
fi

# ---- Bước 2: GET /health (không cần key) ----
if [[ $tcp_ok -eq 1 ]]; then
  body=$(curl "${CURL_OPTS[@]}" -o - -w '\n%{http_code}' "${BASE}/health" 2>&1)
  code=$(printf '%s' "$body" | tail -n1)
  if [[ "$code" == "200" ]]; then
    pass "[2/3] GET /health -> 200"
  else
    fail "[2/3] GET /health -> ${code:-không có phản hồi}" \
         "nếu là lỗi TLS: đặt INSECURE=1 hoặc cài CA nội bộ; kiểm tra scheme/port (${SCHEME}:${PORT})"
  fi
else
  fail "[2/3] GET /health bỏ qua (bước 1 thất bại)"
fi

# ---- Bước 3: tools/list có xác thực ----
if [[ -z "${MCP_API_KEY:-}" ]]; then
  fail "[3/3] tools/list bỏ qua" "đặt MCP_API_KEY=<key> rồi chạy lại"
elif [[ $tcp_ok -ne 1 ]]; then
  fail "[3/3] tools/list bỏ qua (bước 1 thất bại)"
else
  resp=$(curl "${CURL_OPTS[@]}" -X POST "${BASE}/mcp" \
    -H "X-Api-Key: ${MCP_API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
    -w '\n%{http_code}' 2>&1)
  code=$(printf '%s' "$resp" | tail -n1)
  out=$(printf '%s' "$resp" | sed '$d')
  case "$code" in
    200)
      if printf '%s' "$out" | grep -q '"tools"'; then
        n=$(printf '%s' "$out" | grep -o '"name"' | wc -l | tr -d ' ')
        pass "[3/3] POST /mcp tools/list -> 200 (~${n} mục name; kỳ vọng 13 tools)"
      else
        fail "[3/3] POST /mcp -> 200 nhưng không thấy \"tools\" trong phản hồi" "${out:0:200}"
      fi ;;
    401) fail "[3/3] POST /mcp -> 401" "API key sai / thiếu — so lại với MCP_API_KEYS của server" ;;
    503) fail "[3/3] POST /mcp -> 503" "server chưa cấu hình MCP_API_KEYS (fail-closed)" ;;
    *)   fail "[3/3] POST /mcp -> ${code:-không có phản hồi}" "${out:0:200}" ;;
  esac
fi

echo "-----------------------------------------------"
if [[ $fails -eq 0 ]]; then
  echo "KẾT QUẢ: PASS (3/3)"
  exit 0
fi
echo "KẾT QUẢ: FAIL (${fails} bước lỗi)"
exit 1
