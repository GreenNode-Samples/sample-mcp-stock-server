# 📈 VN Stock MCP Server — GreenNode AgentBase sample

> **MCP server THUẦN** (không phải agent): không LLM, không memory, không chat.
> Deploy như một **AgentBase runtime**, đăng ký làm **MCP Connector** vào
> **MCP Gateway** → mọi agent trên gateway (được Policy cho phép) gọi tools
> cổ phiếu Việt Nam qua gateway.

[![CI](https://github.com/GreenNode-Sample-AgentBase/greennode-agentbase-mcp-stock-server/actions/workflows/ci.yml/badge.svg)](https://github.com/GreenNode-Sample-AgentBase/greennode-agentbase-mcp-stock-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Vì sao repo này tồn tại?

Hạ tầng GreenNode AgentBase không chỉ chạy *agent* — nó chạy **bất kỳ MCP server
nào** làm một runtime riêng. Đó là mô hình "tool provider":

```
┌─────────────────────────┐      ┌──────────────────────────┐
│  Agent (chatbot, ...)   │      │   VN Stock MCP Server    │
│  travel-buddy / zalo-…  │      │   (repo này — KHÔNG LLM) │
│  LLM + Memory + Policy  │      │  5 tools cổ phiếu VN     │
└───────────┬─────────────┘      └───────────┬──────────────┘
            │  tools/call (MCP JSON-RPC)      │
            ▼                                ▼
      ┌────────────────────────────────────────────┐
      │            MCP Gateway (sample-mcp-gw)     │
      │   inbound auth IAM · Policy Group · proxy  │
      └────────────────────────────────────────────┘
```

- **Agent** = ai *suy nghĩ* (LLM + memory), gọi tool qua gateway
- **MCP server** (repo này) = ai *cầm dữ liệu* — agent nào cũng gọi được,
  không cần biết server nằm ở đâu, credentials do gateway lo

## Tools (5)

| Tool | Mô tả |
|---|---|
| `market_top_stocks(limit, sort)` | Bảng ~90 mã top thị trường. `sort`: `default` / `change_percent` / `value` / `volume` / `foreign_net_buy` |
| `top_gainers(limit)` | Top mã **tăng** mạnh nhất (chỉ % dương) |
| `top_losers(limit)` | Top mã **giảm** sâu nhất (chỉ % âm) |
| `most_active(limit)` | Top **thanh khoản** (giá trị giao dịch, tỷ VND) |
| `stock_quote(symbol)` | Báo giá 1 mã: giá, +/-%, trần/sàn, KL & GT khớp, ngoại mua/bán |

Mỗi kết quả kèm `meta`: nguồn, số mã theo dõi, `last_update` (giờ VN) và cảnh báo
nếu dữ liệu >15 phút cũ (thị trường đóng cửa).

**Nguồn dữ liệu:** API công khai của [24hMoney](https://24hmoney.vn)
(`stock-recommend/top-stock-all`, không cần API key). Đây là API không chính thức
của app — chỉ dùng cho demo/sample, không dùng cho giao dịch thật.

## Cấu trúc repo

```
├── Dockerfile                # image: python:3.12-slim, port 8080, /health
├── requirements.txt          # mcp + uvicorn + httpx
├── src/mcp_server/main.py    # toàn bộ server (~370 dòng, 1 file)
└── tests/                    # 23 test hermetic (không gọi network)
```

## Chạy local

```bash
docker build -t stock-mcp-server .
docker run -p 8080:8080 stock-mcp-server
# health
curl -s http://localhost:8080/health
# thử tool trực tiếp bằng MCP client
python - <<'EOF'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://localhost:8080/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([t.name for t in tools.tools])
            res = await s.call_tool("top_gainers", {"limit": 5})
            print(res.content[0].text)

asyncio.run(main())
EOF
```

## Deploy lên GreenNode AgentBase (3 bước)

> Yêu cầu: đã có [credentials IAM](https://aiplatform.console.vngcloud.vn) và
> repo ảnh trong **Container Registry** (xem skill `agentbase-deploy`).

### 1. Build & push image

```bash
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 .
docker push vcr.vngcloud.vn/<repo>/stock-mcp-server:v1
```

### 2. Tạo runtime (KHÔNG cần env file — không có secret!)

```bash
bash ~/.agents/skills/agentbase/scripts/runtime.sh create \
  --name "stock-mcp-server" \
  --image "vcr.vngcloud.vn/<repo>/stock-mcp-server:v1" \
  --flavor "runtime-s2-general-2x4" \
  --from-cr
# → ACTIVE, tạo endpoint công khai tự động
curl -s https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/health
```

### 3. Đăng ký Connector vào MCP Gateway

Console → **MCP Gateway** → chọn gateway → **Add Custom Connector**:

- **Name**: `stock`
- **Endpoint**: `https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/mcp`
- **Outbound auth**: `No authorization` (server này keyless)

Từ thời điểm này, mọi agent đã gắn gateway (Policy Group cho phép) thấy 5 tools
`market_top_stocks`, `top_gainers`, `top_losers`, `most_active`, `stock_quote`
(bị prefix theo connector) — không cần đổi code agent gì cả.

Hoặc qua API (JSON Merge Patch — **gửi nguyên mảng targets mong muốn**):

```bash
TOKEN=$(bash ~/.agents/skills/agentbase/scripts/get_token.sh)
curl -X PATCH "https://agentbase.api.vngcloud.vn/gateway/api/v1/gateways/<gw>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H 'If-Match: "<resourceVersion>"' \
  -d '{"targets": [ /* targets cũ (giữ nguyên!) + */ {
        "name": "stock",
        "type": "MCP",
        "endpoint": "https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/mcp",
        "outboundAuth": {"type": "NONE"}
  }]}'
```

> ⚠️ `targets` là **thay thế toàn bộ** — nhớ lấy danh sách hiện tại
> (`GET /gateways/<gw>`) và gửi đủ các connector cũ, nếu không sẽ mất chúng.

## Policy (ai được gọi tools nào?)

Gateway đặt trong Policy Group (skill `agentbase-policy`). Action có dạng
`<connector>__<tool>`:

```json
{
  "effect": "allow",
  "principal": "iam:<runtime-identity-của-agent>",
  "actions": ["stock__market_top_stocks", "stock__top_gainers",
               "stock__top_losers", "stock__most_active", "stock__stock_quote"]
}
```

`tools/list` luôn được phép (để agent khám phá tools); `tools/call` mới qua policy.

## Kiến trúc kỹ thuật

- **FastMCP** (`stateless_http=True`) — MCP streamable HTTP tại `/mcp`
- **TTL cache 60s** — 1 call upstream phục vụ nhiều tool call (có `cache_hits`/`upstream_calls` trong `/health`)
- **Retry 3 lần** với backoff cho GET idempotent; lỗi sạch → `{"error": ...}` cho agent đọc
- **Timezone VN** (`Asia/Ho_Chi_Minh`) cho `last_update` + phát hiện dữ liệu hết phiên
- Runtime contract: port `8080`, `GET /health` → 200, `GET /` mô tả server

## Environment variables (tất cả optional)

| Biến | Mặc định | Mô tả |
|---|---|---|
| `STOCK_API_BASE_URL` | `https://api-finance-t19.24hmoney.vn` | Ghi đè nguồn (proxy nội bộ) |
| `HTTP_TIMEOUT_SECONDS` | `10` | Timeout gọi API |
| `CACHE_TTL_SECONDS` | `60` | TTL cache dữ liệu phiên |

## Test

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -v   # 23 tests, hermetic — không gọi network
```

## Tài nguyên liên quan

- Skill `agentbase-deploy` — runtime + Container Registry
- Skill `agentbase-gateway` — connectors, inbound/outbound auth, policy binding
- Skill `agentbase-policy` — viết policy `stock__*__tool`
- Hai sample agent dùng chung hạ tầng: `greennode-agentbase-travel-buddy`,
  `greennode-agentbase-zalo-restaurant`

## License

[MIT](LICENSE) — dữ liệu thuộc về 24hMoney, chỉ dùng cho mục đích demo.
