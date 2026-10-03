# Chạy MCP server on-prem (data center của khách hàng)

Dùng khi dữ liệu/nghiệp vụ phải nằm trong **data center** của khách hàng. MCP Gateway (chế độ
**Private**) đi vào qua VPC của khách hàng, rồi qua **Site-to-Site VPN** hoặc **Interconnect** tới
MCP server on-prem. Cùng image với các bản Agent Runtime / vServer / VKS.

```
Agent → MCP Gateway (Private, AgentBase VPC 172.30.0.0/16)
      → kết nối private → VPC khách hàng (Route CIDRs có dải on-prem)
      → Site-to-Site VPN / Interconnect (phía khách hàng) → firewall DC
      → stock-mcp :8080 (hoặc :8443 sau reverse proxy TLS) → 24hMoney
```

## Chạy container

```bash
cd deploy/onprem
cp .env.example .env && chmod 600 .env     # MCP_API_KEYS, BIND_ADDR=<IP nội bộ>
docker compose up -d --build
docker compose ps                          # healthy
curl -s http://<BIND_ADDR>:8080/health
```

`BIND_ADDR` là IP của interface nội bộ — container **không** lắng nghe trên mọi interface.

## Checklist mạng

### 1. Quy hoạch CIDR

- [ ] CIDR on-prem (`xx.xx.x.x/xx`) **không trùng** CIDR VPC khách hàng (`xx.xx.x.x/xx`) và **không trùng**
      `172.30.0.0/16` (dải AgentBase VPC).
- [ ] Ghi lại IP của MCP host on-prem (`xx.xx.x.x`) và cổng (`8080`/`8443`).

### 2. Kết nối VPC ↔ data center (phía khách hàng)

- [ ] Đã dựng **Site-to-Site VPN** hoặc **Interconnect** giữa VPC khách hàng và data center, trạng thái `UP`.
- [ ] VPC khách hàng đã được **kết nối private với AgentBase** (chỉ VPC này mới hiện khi tạo Private gateway;
      liên hệ GreenNode support để kích hoạt) và **bật DNS resolution**.

### 3. Route — hai chiều

- [ ] **Gateway → on-prem**: khi tạo Private gateway (bước Network & Compute) chọn VPC + Subnet và khai báo
      **Route CIDRs** gồm CIDR on-prem `xx.xx.x.x/xx`. Network mode **không đổi được sau khi tạo**
      (có thể sửa Route CIDRs, xem skill `agentbase-gateway` — change VPC routes).
- [ ] **VPC khách hàng**: route table có `xx.xx.x.x/xx (on-prem)` → VPN/Interconnect.
- [ ] **On-prem → gateway (chiều về)**: router/firewall DC có route `172.30.0.0/16` → đường hầm VPN/Interconnect,
      nếu không gói trả về của MCP server sẽ không quay lại gateway.

### 4. Firewall

- [ ] Cho phép nguồn **`172.30.0.0/16`** → `xx.xx.x.x:8080` (hoặc `:8443`) TCP.
- [ ] Nguồn thấy ở phía data center có thể là `172.30.0.0/16` hoặc đã bị NAT ở VPC — **xác nhận với GreenNode**
      rồi mở đúng dải đó.
- [ ] Ngoài ra chặn mọi nguồn khác tới cổng MCP.
- [ ] Outbound của MCP host ra `api-finance-t19.24hmoney.vn:443` (hoặc proxy/mirror qua `STOCK_API_BASE_URL`).

### 5. TLS

- [ ] Connector URL nên là HTTPS: `https://xx.xx.x.x:8443/mcp`. Đặt reverse proxy (Caddy/nginx — xem mẫu
      ở `../vserver/Caddyfile`) trước container, bind `BIND_ADDR`.
- [ ] Cert do **CA mà gateway tin cậy** cấp. Dùng CA nội bộ/self-signed → xác nhận với GreenNode cách đưa CA vào connector.

### 6. DNS

- [ ] Dùng IP trực tiếp (`xx.xx.x.x`) là đơn giản nhất. Nếu dùng hostname: VPC phải bật DNS resolution và
      resolve được hostname (zone/forwarder về DNS nội bộ DC); cert phải khớp hostname.

## Kiểm tra từ VPC khách hàng

Chạy trên một host (vd vServer) trong VPC khách hàng, **trước** khi tạo connector:

```bash
MCP_API_KEY=<key> ./check_connectivity.sh xx.xx.x.x 8080 http
MCP_API_KEY=<key> INSECURE=1 ./check_connectivity.sh xx.xx.x.x 8443 https   # cert nội bộ
```

Script in `PASS`/`FAIL` cho 3 bước: ① TCP tới host:port, ② `GET /health`, ③ `tools/list` JSON-RPC có
`X-Api-Key`. Chỉ cần `bash` + `curl`; thoát `0` nếu cả 3 PASS.

> Lưu ý: script chạy từ host trong VPC chỉ chứng minh đường **VPC → on-prem** thông. Đường
> **gateway (172.30.0.0/16) → on-prem** còn phụ thuộc route chiều về và firewall (mục 3–4).

## Nối vào MCP Gateway

1. Gateway **Private** (VPC + Subnet + Route CIDRs có dải on-prem).
2. Access Control: API Key provider (vd `stock-mcp-key`) = `MCP_API_KEYS`.
3. Connector `stock`: Endpoint `https://xx.xx.x.x:8443/mcp`, Outbound Auth = **API Key**, header `X-Api-Key`.
4. Policy Group: action `stock__<tool>` như README gốc.
