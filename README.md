# 📈 VN Stock MCP Server — GreenNode AgentBase sample

> **MCP server THUẦN** (không phải agent): không LLM, không memory, không chat.
> Deploy như một **Agent Runtime** trên AgentBase, đăng ký làm **MCP Connector**
> vào **MCP Gateway** → mọi agent trên gateway (được Policy Group cho phép) gọi
> 13 tools cổ phiếu Việt Nam qua gateway.
>
> Cùng một image có thể chạy ở **3 nơi** — Agent Runtime, vServer/VKS trong VPC khách hàng,
> hoặc on-prem: xem [Triển khai ở 3 nơi](#triển-khai-ở-3-nơi).

[![CI](https://github.com/GreenNode-Samples/sample-mcp-stock-server/actions/workflows/ci.yml/badge.svg)](https://github.com/GreenNode-Samples/sample-mcp-stock-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Vì sao repo này tồn tại?

Hạ tầng GreenNode AgentBase không chỉ chạy *agent* — **Agent Runtime** chạy được
**bất kỳ MCP server nào** như một runtime riêng. Đó là mô hình "tool provider":

```
 Agent (LLM + Memory)
        │  tools/call (MCP JSON-RPC) + IAM/JWT token của agent
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ MCP Gateway                                                  │
 │  ① Inbound Auth (IAM / JWT) — xác thực agent                 │
 │  ② Policy Group — agent này có được gọi stock__<tool> không? │
 │  ③ MCP Connector `stock` — Outbound Auth = API Key           │
 │     (key lấy từ Access Control, gắn vào header X-Api-Key)    │
 └──────────────────────────────┬───────────────────────────────┘
                                │  POST /mcp  + X-Api-Key
                                ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ Agent Runtime: repo này (KHÔNG LLM)                          │
 │  Security Settings (IP Access Control · Inbound Identity)    │
 │  → middleware kiểm tra API key (fail-closed) → 13 tools      │
 └──────────────────────────────┬───────────────────────────────┘
                                ▼
                       24hMoney (API công khai, không chính thức)
```

- **Agent** = ai *suy nghĩ* (LLM + memory), gọi tool qua gateway.
- **MCP server** (repo này) = ai *cầm dữ liệu* — agent nào cũng gọi được, không cần
  biết server nằm ở đâu; credential do gateway lo, agent không bao giờ thấy API key.

## Tools (13)

Mọi tool trả về JSON (chuỗi). Lỗi trả `{"error": "..."}` để agent đọc được, không ném exception.

### Thị trường

Nguồn chung `top-stock-all` (~90 mã thanh khoản cao nhất), cache 60s.

| Tool | Mô tả |
|---|---|
| `market_top_stocks(limit, sort)` | Bảng ~90 mã top thị trường. `sort`: `default` / `change_percent` / `value` / `volume` / `foreign_net_buy` |
| `top_gainers(limit)` | Top mã **tăng** mạnh nhất (chỉ % dương) |
| `top_losers(limit)` | Top mã **giảm** sâu nhất (chỉ % âm) |
| `most_active(limit)` | Top **thanh khoản** (giá trị giao dịch) |
| `stock_quote(symbol)` | Báo giá 1 mã trong nhóm top: giá, +/-%, trần/sàn, KL & GT khớp, ngoại mua/bán |

Kết quả kèm `meta`: nguồn, số mã theo dõi, `last_update` (giờ VN) và cảnh báo nếu
dữ liệu >15 phút cũ (thị trường đóng cửa).

### Doanh nghiệp

Áp dụng cho mọi mã niêm yết HOSE · HNX · UPCOM (~1.6k công ty). `symbol` chỉ nhận `A–Z`, `0–9`, 2–10 ký tự.

| Tool | Mô tả |
|---|---|
| `search_company(query, limit)` | Tìm mã theo tên / mã, không phân biệt dấu & hoa/thường. Xếp hạng: khớp mã → khớp tên, rồi ưu tiên doanh nghiệp lớn (`priority`), sàn HOSE > HNX > UPCOM |
| `company_profile(symbol)` | Tên đầy đủ (VN/EN), sàn niêm yết, mô tả hoạt động |
| `price_history(symbol, days)` | Giá đóng cửa, KL, GT theo ngày (≤ 30 phiên) |
| `foreign_trading(symbol, days)` | Khối ngoại mua/bán theo ngày (≤ 25 phiên) |
| `valuation(symbol)` | P/E, P/B, ROE, ROA, EPS… so với trung bình ngành |
| `dividend_history(symbol, limit)` | Lịch sử cổ tức (tiền mặt / cổ phiếu) |
| `business_plan(symbol)` | Kế hoạch kinh doanh năm & % hoàn thành |
| `company_announcements(symbol, limit)` | Tin công bố thông tin (kèm link PDF) |

### Nguồn dữ liệu & miễn trừ trách nhiệm

Dữ liệu lấy từ các API công khai mà web/app [24hMoney](https://24hmoney.vn) sử dụng
(`api-finance-t19.24hmoney.vn`), không cần API key. Đây là **API không chính thức**,
có thể đổi/chặn bất kỳ lúc nào.

> ⚠️ **Chỉ dùng cho demo/sample.** Không phải khuyến nghị đầu tư, không dùng cho giao
> dịch thật. Dữ liệu có thể trễ hoặc sai.

**Đơn vị:** giá cổ phiếu tính bằng **nghìn VND**; giá trị giao dịch / vốn hoá tính bằng **tỷ VND**.

## Cấu trúc repo

```
├── Dockerfile                # image: python:3.12-slim, port 8080, /health
├── requirements.txt          # mcp + uvicorn + httpx
├── .env.example              # mẫu biến môi trường
├── deploy/                   # triển khai ngoài Agent Runtime (không vào image)
│   ├── vserver/              #   docker compose trên vServer (+ Caddy TLS tuỳ chọn)
│   ├── vks/                  #   manifest Kubernetes cho VKS
│   └── onprem/               #   docker compose on-prem + check_connectivity.sh
├── src/mcp_server/main.py    # toàn bộ server (1 file)
└── tests/                    # 43 test hermetic (không gọi network)
```

## Chạy local

```bash
export KEY=$(openssl rand -hex 32)          # API key cho lần chạy này

docker build -t stock-mcp-server .
docker run --rm -p 8080:8080 -e MCP_API_KEYS="$KEY" stock-mcp-server

# health — luôn mở, không cần key
curl -s http://localhost:8080/health
```

Gọi tool bằng MCP client (Python), truyền key qua header `X-Api-Key`:

```bash
pip install mcp
KEY=$KEY python - <<'PY'
import asyncio, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

KEY = os.environ["KEY"]

async def main():
    async with streamablehttp_client(
        "http://localhost:8080/mcp", headers={"X-Api-Key": KEY}
    ) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print([t.name for t in tools.tools])
            res = await s.call_tool("search_company", {"query": "hoa phat"})
            print(res.content[0].text)

asyncio.run(main())
PY
```

Chạy không Docker: `pip install -r requirements.txt && MCP_API_KEYS=$KEY python src/mcp_server/main.py`.

## Authentication

Có **2 lớp bảo vệ độc lập**; nên bật cả hai khi deploy thật.

### Lớp 1 — API key ở tầng ứng dụng (fail-closed)

Server tự kiểm tra API key trên endpoint `/mcp`:

| Mục | Hành vi |
|---|---|
| Cấu hình | Biến môi trường `MCP_API_KEYS` — danh sách key cách nhau bởi dấu phẩy. (`STOCK_API_KEY` cũ vẫn được nhận) |
| Header hợp lệ | `X-Api-Key: <key>` · `Authorization: Bearer <key>` · `X-Stock-Api-Key: <key>` (cũ) |
| So sánh | Constant-time; key ngắn hơn 24 ký tự bị cảnh báo trong log → dùng `openssl rand -hex 32` |
| Có key nhưng caller thiếu/sai | `401` kèm `WWW-Authenticate` |
| **Chưa cấu hình key nào** | `503` — server **không bao giờ tự mở** (fail-closed) |
| `/health`, `/` | Luôn mở (để runtime health-probe) |

**Xoay vòng key (rotation):** đặt 2 key cùng lúc `MCP_API_KEYS="key_cu,key_moi"` → cập
nhật key ở phía gọi (Access Control / connector) sang `key_moi` → bỏ `key_cu` khỏi
biến môi trường. Không có downtime.

**`ALLOW_ANONYMOUS=true`** chỉ để chạy local khi không muốn đặt key (khi đó `/mcp` mở hoàn toàn nếu
`MCP_API_KEYS` trống). **Tuyệt đối không dùng trên môi trường deploy.** Nếu đã có key thì
key vẫn bắt buộc kể cả khi `ALLOW_ANONYMOUS=true`.

### Lớp 2 — Security Settings của Agent Runtime (tầng platform)

Cấu hình khi tạo Agent Runtime trên console (xem
[tài liệu Create runtime](https://docs.greennode.ai/ai-stack/agent-base/agent-runtime/create-runtime)),
chặn request **trước khi** chạm tới container:

- **IP Access Control** — danh sách CIDR nguồn được phép gọi endpoint của runtime
  (ví dụ chỉ cho phép dải IP egress của MCP Gateway / mạng công ty).
- **Inbound Identity** — cách runtime xác thực người gọi: `IAM Permissions`, `JWT`
  hoặc `No authorization`.

> Lưu ý: connector của MCP Gateway gọi runtime bằng **API Key** (xem bên dưới), nên nếu
> bật Inbound Identity ở runtime, hãy chọn chế độ tương thích với cách gateway gọi vào
> và kiểm thử lại `tools/list`. Lớp 1 (API key) vẫn là lớp bắt buộc của sample này.

## Deploy lên GreenNode AgentBase

> Yêu cầu: đã có [credentials IAM](https://aiplatform.console.vngcloud.vn), repo ảnh
> trong **Container Registry** (vCR), và một **MCP Gateway** (xem skill
> `agentbase-deploy`, `agentbase-gateway`).

### 1. Build & push image lên Container Registry

```bash
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 .
docker push vcr.vngcloud.vn/<repo>/stock-mcp-server:v1
```

### 2. Tạo Agent Runtime

Tạo runtime từ image trên (console hoặc skill `agentbase-deploy`), flavor ví dụ
`runtime-s2-general-2x4`, và đặt biến môi trường:

```bash
export MCP_KEY=$(openssl rand -hex 32)   # GHI LẠI — bước 3 dùng lại cùng giá trị

# env của runtime:
#   MCP_API_KEYS=<giá trị $MCP_KEY>
```

Khi runtime `ACTIVE` (endpoint công khai được tạo tự động):

```bash
curl -s https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/health   # → 200, không cần key
```

Trong **Security Settings** của runtime: đặt **IP Access Control** (CIDR được phép) và
chọn **Inbound Identity** phù hợp (xem [Lớp 2](#lớp-2--security-settings-của-agent-runtime-tầng-platform)).

> ⚠️ **Về secret trong env:** tài liệu AgentBase quy định biến môi trường của runtime
> dành cho cấu hình **không nhạy cảm**. API key là secret, nên nơi lưu đúng là
> **Access Control** (bước 3). Tuy nhiên server này *đọc key từ biến môi trường*
> `MCP_API_KEYS`, nên **trong sample này ta đặt cùng một key ở cả hai nơi** — env của
> runtime (để server kiểm tra) và Access Control (để gateway gắn vào request). Đây là
> đánh đổi của bản demo; với production hãy xoay key định kỳ và hạn chế ai được xem
> cấu hình runtime.

### 3. Lưu key vào Access Control

Console → **Access Control** → tạo **API Key provider** tên `stock-mcp-key`, giá trị là
**đúng key** `$MCP_KEY` ở bước 2 (hoặc dùng skill `agentbase-identity`).

### 4. Đăng ký MCP Connector vào MCP Gateway

Console → **MCP Gateway** → chọn gateway → **Add Custom Connector**:

- **Name**: `stock`
- **Endpoint**: `https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/mcp`
- **Outbound Auth**: **API Key** (flow `2LO` — một key dùng chung cho mọi caller)
  - Header name: `X-Api-Key` (prefix để trống)
  - Provider: `stock-mcp-key`

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
        "outboundAuth": {
          "type": "APIKEY",
          "flow": "2LO",
          "headerName": "X-Api-Key",
          "headerValuePrefix": "",
          "providerName": "stock-mcp-key"
        }
  }]}'
```

> ⚠️ `targets` là **thay thế toàn bộ** — nhớ lấy danh sách hiện tại
> (`GET /gateways/<gw>`) và gửi đủ các connector cũ, nếu không sẽ mất chúng.

### 5. Policy Group — ai được gọi tool nào?

Tool của connector `stock` xuất hiện trên gateway với action dạng `stock__<tool>`.
**Chưa gắn Policy Group nào ⇒ mọi `tools/call` đều bị `403`** (`tools/list` thì luôn được
phép để agent khám phá tools). Ví dụ policy cho phép 1 agent gọi cả 13 tools:

```json
{
  "effect": "allow",
  "principal": "iam:<id-của-agent>",
  "actions": [
    "stock__market_top_stocks", "stock__top_gainers", "stock__top_losers",
    "stock__most_active", "stock__stock_quote",
    "stock__search_company", "stock__company_profile", "stock__price_history",
    "stock__foreign_trading", "stock__valuation", "stock__dividend_history",
    "stock__business_plan", "stock__company_announcements"
  ],
  "resources": ["gateway:<tên-gateway>"]
}
```

Tạo Policy Group chứa policy trên rồi gắn vào gateway (skill `agentbase-policy`).
Không trộn `"*"` với danh sách action cụ thể — chọn một trong hai.

### Luồng đầy đủ

```
Agent → MCP Gateway (Inbound Auth: IAM/JWT)
      → Policy Group (stock__<tool> được phép?)
      → Connector `stock` (Outbound Auth: API Key lấy từ Access Control)
      → Agent Runtime này (/mcp, kiểm tra API key)
      → 24hMoney
```

Không cần đổi code agent: agent chỉ thấy tools `stock__...` qua gateway và IAM token của chính nó.

## Triển khai ở 3 nơi

Cùng **một image** (Dockerfile ở gốc repo) triển khai được ở 3 nơi, ứng với 3 tình huống khách hàng hay hỏi.
Agent Runtime và MCP Gateway chạy trong **AgentBase VPC (`172.30.0.0/16`)** do GreenNode quản lý, không nằm trong
VPC của khách hàng; vì vậy **chế độ mạng của gateway** quyết định nó với tới MCP server ở đâu.

| | (a) Agent Runtime | (b) vServer / VKS trong VPC khách hàng | (c) On-prem (data center) |
|---|---|---|---|
| Khi nào dùng | Nhanh nhất, GreenNode lo hạ tầng | MCP server không được ra Internet, nằm trong VPC riêng | Dữ liệu/server phải ở data center |
| **Gateway Network mode** | **Public** | **Private** (chọn VPC + Subnet của vServer/VKS) | **Private** + **Route CIDRs** gồm CIDR on-prem |
| **Connector URL** | `https://<endpoint>.agentbase-runtime.aiplatform.vngcloud.vn/mcp` | `https://xx.xx.x.x:8443/mcp` | `https://xx.xx.x.x:8443/mcp` (IP MCP host on-prem) |
| **Outbound Auth** | API Key — header `X-Api-Key`, provider trong Access Control | API Key — header `X-Api-Key`, provider trong Access Control | API Key — header `X-Api-Key`, provider trong Access Control |
| Kết nối thêm | — | VPC phải đã kết nối private với AgentBase (liên hệ GreenNode support), bật DNS resolution; security group cho `172.30.0.0/16` | Site-to-Site VPN / Interconnect (phía khách hàng), route hai chiều gồm `172.30.0.0/16`, firewall cho `172.30.0.0/16` |
| Hướng dẫn | [Deploy lên GreenNode AgentBase](#deploy-lên-greennode-agentbase) (ở trên) | [`deploy/vserver/`](deploy/vserver/README.md) · [`deploy/vks/`](deploy/vks/README.md) | [`deploy/onprem/`](deploy/onprem/README.md) |

Lưu ý:

- **Network mode của gateway chọn lúc tạo (bước ⑤ Network & Compute) và không đổi được sau đó.** Gateway **Private**
  giữ lưu lượng trong mạng private, nên MCP server chỉ có trên Internet (như (a)) cần một gateway **Public** riêng.
- Private gateway chỉ liệt kê VPC **đã được kết nối private với AgentBase**; chưa có thì liên hệ GreenNode support để kích hoạt.
- Dù chạy ở đâu, **Policy Group vẫn dùng cùng các action `stock__<tool>`** (vd `stock__search_company`) —
  chỉ đổi Endpoint của connector, không phải viết lại policy hay code agent.
- Phía on-prem, nguồn request có bị NAT hay không (có thể không còn là `172.30.0.0/16`) → xác nhận với GreenNode trước khi mở firewall.
- Địa chỉ `xx.xx.x.x/xx` trong tài liệu là placeholder — thay bằng dải thực của bạn.

## Kiến trúc kỹ thuật

- **FastMCP** (`stateless_http=True`) — MCP streamable HTTP tại `/mcp`.
- **`fetch_api()` chung**: GET có **TTL cache** (dữ liệu phiên 60s; định giá / cổ tức / kế hoạch / tin 15 phút;
  danh bạ công ty 6 giờ) và **retry 3 lần** với backoff; số liệu `cache_hits` / `upstream_calls` có trong `/health`.
- **Validate `symbol`** (`^[A-Z0-9]{2,10}$`) trước khi ghép vào request upstream; `limit`/`days` bị kẹp trong khoảng cho phép.
- **Timezone VN** (`Asia/Ho_Chi_Minh`) cho `last_update` + phát hiện dữ liệu hết phiên.
- Runtime contract: port `8080` (đổi bằng `PORT`), `GET /health` → 200, `GET /` mô tả server.

## Environment variables

| Biến | Mặc định | Mô tả |
|---|---|---|
| `MCP_API_KEYS` | *(bắt buộc)* | API key bảo vệ `/mcp`, nhiều key cách nhau bởi `,` (xoay vòng). Trống ⇒ `/mcp` trả 503 |
| `ALLOW_ANONYMOUS` | *(trống)* | `true` ⇒ cho phép `/mcp` không cần key khi chưa cấu hình key. **Chỉ dùng local dev** |
| `STOCK_API_BASE_URL` | `https://api-finance-t19.24hmoney.vn` | Ghi đè nguồn dữ liệu (proxy/mirror nội bộ) |
| `HTTP_TIMEOUT_SECONDS` | `10` | Timeout gọi API upstream |
| `CACHE_TTL_SECONDS` | `60` | TTL cache dữ liệu phiên (top stocks, giá, ngoại) |
| `SLOW_CACHE_TTL_SECONDS` | `900` | TTL cache dữ liệu chậm đổi (cổ tức, kế hoạch, định giá, tin) |
| `COMPANY_CACHE_TTL_SECONDS` | `21600` | TTL cache danh bạ công ty (6 giờ) |
| `PORT` | `8080` | Cổng lắng nghe |

## Test

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -v   # 43 tests, hermetic — không gọi network
```

Bao gồm: sắp xếp/giới hạn market tools, xếp hạng `search_company`, validate symbol, 13 tool đã đăng ký,
và middleware auth fail-closed (503 / 401 / 3 kiểu header / xoay vòng 2 key).

## Tài nguyên liên quan

- Skill `agentbase-deploy` — Agent Runtime + Container Registry
- Skill `agentbase-identity` — Access Control: lưu API key provider
- Skill `agentbase-gateway` — MCP Connector, inbound/outbound auth, gắn Policy Group
- Skill `agentbase-policy` — viết policy `stock__<tool>`
- Hai sample agent dùng chung hạ tầng: `sample-travel-buddy`,
  `sample-zalo-restaurant`

## License

[MIT](LICENSE) — dữ liệu thuộc về 24hMoney, chỉ dùng cho mục đích demo.
