# Run the MCP server on-prem (customer data center)

> For an end-to-end on-premises walkthrough (GreenNode VPN Site-to-Site, strongSwan IPsec config, nftables firewall, lab without a real data center), see [sample-onprem-mcp-vpn](https://github.com/GreenNode-Samples/sample-onprem-mcp-vpn).

Use this when the data or business logic must stay in the customer's **data center**. MCP Gateway (**Private**
mode) enters through the customer's VPC, then crosses a **Site-to-Site VPN** or **Interconnect** to the
on-prem MCP server. It uses the same image as the Agent Runtime / vServer / VKS variants.

```
Agent → MCP Gateway (Private, AgentBase VPC 172.30.0.0/16)
      → private connection → customer VPC (Route CIDRs include the on-prem range)
      → Site-to-Site VPN / Interconnect (customer side) → DC firewall
      → stock-mcp :8080 (or :8443 behind a TLS reverse proxy) → 24hMoney
```

## Run the container

```bash
cd deploy/onprem
cp .env.example .env && chmod 600 .env     # MCP_API_KEYS, BIND_ADDR=<internal IP>
docker compose up -d --build
docker compose ps                          # healthy
curl -s http://<BIND_ADDR>:8080/health
```

`BIND_ADDR` is the IP of the internal interface — the container does **not** listen on all interfaces.

## Network checklist

### 1. CIDR planning

- [ ] The on-prem CIDR (`xx.xx.x.x/xx`) **does not overlap** the customer VPC CIDR (`xx.xx.x.x/xx`) and **does not overlap**
      `172.30.0.0/16` (the AgentBase VPC range).
- [ ] Record the IP of the on-prem MCP host (`xx.xx.x.x`) and the port (`8080`/`8443`).

### 2. VPC ↔ data center connectivity (customer side)

- [ ] A **Site-to-Site VPN** or **Interconnect** between the customer VPC and the data center is set up and in `UP` state.
- [ ] The customer VPC is **privately connected to AgentBase** (only such VPCs appear when creating a Private gateway;
      contact GreenNode support to enable it) and has **DNS resolution enabled**.

### 3. Routes — both directions

- [ ] **Gateway → on-prem**: when creating the Private gateway (Network & Compute step), select the VPC + Subnet and declare
      **Route CIDRs** that include the on-prem CIDR `xx.xx.x.x/xx`. The Network mode **cannot be changed after creation**
      (Route CIDRs can be edited; see the `agentbase-gateway` skill — change VPC routes).
- [ ] **Customer VPC**: the route table has `xx.xx.x.x/xx (on-prem)` → VPN/Interconnect.
- [ ] **On-prem → gateway (return path)**: the DC router/firewall has a route `172.30.0.0/16` → the VPN/Interconnect tunnel;
      otherwise the MCP server's reply packets will not return to the gateway.

### 4. Firewall

- [ ] Allow source **`172.30.0.0/16`** → `xx.xx.x.x:8080` (or `:8443`) TCP.
- [ ] The source seen on the data center side may be `172.30.0.0/16` or may have been NATed in the VPC — **confirm with GreenNode**
      and then open exactly that range.
- [ ] Block all other sources to the MCP port.
- [ ] Outbound from the MCP host to `api-finance-t19.24hmoney.vn:443` (or a proxy/mirror via `STOCK_API_BASE_URL`).

### 5. TLS

- [ ] The connector URL should be HTTPS: `https://xx.xx.x.x:8443/mcp`. Place a reverse proxy (Caddy/nginx — see the sample
      in `../vserver/Caddyfile`) in front of the container, bound to `BIND_ADDR`.
- [ ] The certificate must be issued by a **CA that the gateway trusts**. If you use an internal/self-signed CA, confirm with GreenNode how to provide the CA to the connector.

### 6. DNS

- [ ] Using a direct IP (`xx.xx.x.x`) is the simplest option. If you use a hostname: the VPC must have DNS resolution enabled and
      be able to resolve the hostname (zone/forwarder to the DC's internal DNS); the certificate must match the hostname.

## Verify from the customer VPC

Run on a host (for example, a vServer) in the customer VPC, **before** creating the connector:

```bash
MCP_API_KEY=<key> ./check_connectivity.sh xx.xx.x.x 8080 http
MCP_API_KEY=<key> INSECURE=1 ./check_connectivity.sh xx.xx.x.x 8443 https   # internal cert
```

The script prints `PASS`/`FAIL` for three steps: ① TCP to host:port, ② `GET /health`, ③ JSON-RPC `tools/list` with
`X-Api-Key`. It needs only `bash` + `curl`, and exits `0` if all three PASS.

> Note: running the script from a host in the VPC only proves that the **VPC → on-prem** path works. The
> **gateway (172.30.0.0/16) → on-prem** path also depends on the return route and the firewall (sections 3–4).

## Connect to MCP Gateway

1. A **Private** gateway (VPC + Subnet + Route CIDRs including the on-prem range).
2. Access Control: an API Key provider (e.g. `stock-mcp-key`) = `MCP_API_KEYS`.
3. Connector `stock`: Endpoint `https://xx.xx.x.x:8443/mcp`, Outbound Auth = **API Key**, header `X-Api-Key`.
4. Policy Group: `stock__<tool>` actions as in the main README.
