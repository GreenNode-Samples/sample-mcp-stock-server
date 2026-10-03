# Chạy MCP server trên GreenNode vServer (trong VPC khách hàng)

Dùng khi MCP server phải nằm **trong VPC của khách hàng** (không mở ra Internet) và
được **MCP Gateway chế độ Private** gọi vào qua kết nối private tới AgentBase.
Cùng một image với bản Agent Runtime — chỉ khác nơi chạy.

```
Agent → MCP Gateway (Private, AgentBase VPC 172.30.0.0/16)
      → kết nối private → VPC khách hàng → vServer (subnet private)
      → [Caddy :8443 TLS] → stock-mcp :8080 → 24hMoney
```

## 1. Chuẩn bị mạng

1. Tạo **vServer** (Ubuntu/Debian có Docker + Docker Compose plugin) trong **subnet private**
   của VPC khách hàng. Không cần Floating/Public IP cho inbound.
2. VPC đó phải **đã được kết nối private với AgentBase** (chỉ những VPC này mới hiện khi tạo
   Private gateway). Chưa có → liên hệ GreenNode support để kích hoạt.
3. **Security group** của vServer — inbound:

   | Giao thức | Cổng | Nguồn | Ghi chú |
   |---|---|---|---|
   | TCP | `8080` (hoặc `8443` nếu dùng TLS) | `172.30.0.0/16` | Dải của AgentBase VPC (MCP Gateway) / private connection |
   | TCP | `22` | `xx.xx.x.x/xx` | SSH từ dải quản trị của bạn |

   Không mở `8080` cho `0.0.0.0/0`. Outbound cần ra được `api-finance-t19.24hmoney.vn:443`
   (qua NAT Gateway của VPC) — hoặc đặt `STOCK_API_BASE_URL` trỏ tới proxy/mirror nội bộ.
4. VPC phải bật **DNS resolution** (yêu cầu của Private gateway).

> Nếu nguồn của request tới vServer có bị NAT hay không (có thể không phải `172.30.0.0/16`)
> → xác nhận với GreenNode, rồi chỉnh security group cho khớp.

## 2. Chạy container

```bash
git clone <repo> && cd greennode-agentbase-sample-mcp-stock-server/deploy/vserver
cp .env.example .env && chmod 600 .env
# sửa .env: MCP_API_KEYS=$(openssl rand -hex 32)  — GHI LẠI, dùng lại ở Access Control

docker compose up -d --build        # build từ Dockerfile của repo
# hoặc dùng image trên vCR: đặt MCP_IMAGE=vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 trong .env
#   docker login vcr.vngcloud.vn && docker compose pull && docker compose up -d

docker compose ps                   # STATUS phải là "healthy"
curl -s http://localhost:8080/health
```

Compose đã có `restart: unless-stopped` và healthcheck `GET /health`
(image slim không có `curl` nên healthcheck dùng `python`).

## 3. TLS (khuyến nghị)

Connector URL mẫu trong tài liệu là `https://xx.xx.x.x:8443/mcp`. Nếu endpoint của connector
yêu cầu HTTPS, chạy Caddy phía trước:

```bash
# .env
BIND_ADDR=127.0.0.1          # chỉ Caddy ra ngoài
CADDY_SITE=xx.xx.x.x         # IP private của vServer (hoặc hostname nội bộ)
TLS_PORT=8443

docker compose --profile tls up -d --build
curl -ks https://xx.xx.x.x:8443/health
```

`Caddyfile` mặc định dùng `tls internal` (CA nội bộ của Caddy). Gateway cần **tin** CA đó —
xác nhận với GreenNode cách dùng CA/cert tuỳ chỉnh cho connector. Cách chắc hơn: dùng cert do
CA doanh nghiệp (mà gateway tin) cấp, mount vào `./certs` và bật dòng `tls /certs/server.crt /certs/server.key`.
Nếu thích nginx: `proxy_pass http://127.0.0.1:8080;` với `proxy_buffering off;` và `listen 8443 ssl;`.

Nhớ mở security group cho cổng `8443` thay vì `8080`.

## 4. Nối vào MCP Gateway

1. **Gateway**: tạo gateway với Network mode **Private** (chọn VPC + Subnet của vServer; **không đổi
   được sau khi tạo**).
2. **Access Control**: tạo API Key provider (vd `stock-mcp-key`) với đúng giá trị `MCP_API_KEYS`.
3. **Connector**: Endpoint `https://xx.xx.x.x:8443/mcp` (hoặc `http://xx.xx.x.x:8080/mcp` nếu không TLS),
   Outbound Auth = **API Key**, header `X-Api-Key`, provider `stock-mcp-key`.
4. **Policy Group**: giữ nguyên action `stock__<tool>` như ở README gốc.

Kiểm tra nhanh từ một host khác trong VPC: `../onprem/check_connectivity.sh` (cũng dùng được cho vServer).

## Cập nhật / xoay key

```bash
docker compose up -d --build            # sau khi pull code mới
# xoay key: MCP_API_KEYS="key_cu,key_moi" → docker compose up -d → đổi key ở Access Control → bỏ key_cu
```
