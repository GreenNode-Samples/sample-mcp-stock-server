# Run the MCP server on GreenNode vServer (inside the customer VPC)

Use this when the MCP server must sit **inside the customer's VPC** (not exposed to the Internet) and be
called by a **Private-mode MCP Gateway** over a private connection to AgentBase.
It uses the same image as the Agent Runtime variant — only the place it runs differs.

```
Agent → MCP Gateway (Private, AgentBase VPC 172.30.0.0/16)
      → private connection → customer VPC → vServer (private subnet)
      → [Caddy :8443 TLS] → stock-mcp :8080 → 24hMoney
```

## 1. Prepare the network

1. Create a **vServer** (Ubuntu/Debian with Docker + the Docker Compose plugin) in a **private subnet**
   of the customer VPC. No Floating/Public IP is needed for inbound traffic.
2. The VPC must be **already privately connected to AgentBase** (only such VPCs appear when creating a
   Private gateway). If it is not, contact GreenNode support to enable it.
3. The vServer's **security group** — inbound:

   | Protocol | Port | Source | Notes |
   |---|---|---|---|
   | TCP | `8080` (or `8443` if using TLS) | `172.30.0.0/16` | AgentBase VPC range (MCP Gateway) / private connection |
   | TCP | `22` | `xx.xx.x.x/xx` | SSH from your administration range |

   Do not open `8080` to `0.0.0.0/0`. Outbound must be able to reach `api-finance-t19.24hmoney.vn:443`
   (via the VPC's NAT Gateway) — or set `STOCK_API_BASE_URL` to point to an internal proxy/mirror.
4. The VPC must have **DNS resolution** enabled (a Private gateway requirement).

> Confirm with GreenNode whether the request source reaching the vServer is NATed (it may not be
> `172.30.0.0/16`), then adjust the security group accordingly.

## 2. Run the container

```bash
git clone <repo> && cd sample-mcp-stock-server/deploy/vserver
cp .env.example .env && chmod 600 .env
# edit .env: MCP_API_KEYS=$(openssl rand -hex 32)  — RECORD IT, reuse it in Access Control

docker compose up -d --build        # build from the repo's Dockerfile
# or use the image on vCR: set MCP_IMAGE=vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 in .env
#   docker login vcr.vngcloud.vn && docker compose pull && docker compose up -d

docker compose ps                   # STATUS must be "healthy"
curl -s http://localhost:8080/health
```

Compose already sets `restart: unless-stopped` and a `GET /health` healthcheck
(the slim image has no `curl`, so the healthcheck uses `python`).

## 3. TLS (recommended)

The sample connector URL in the documentation is `https://xx.xx.x.x:8443/mcp`. If the connector's endpoint
requires HTTPS, run Caddy in front:

```bash
# .env
BIND_ADDR=127.0.0.1          # only Caddy is exposed externally
CADDY_SITE=xx.xx.x.x         # the vServer's private IP (or an internal hostname)
TLS_PORT=8443

docker compose --profile tls up -d --build
curl -ks https://xx.xx.x.x:8443/health
```

The default `Caddyfile` uses `tls internal` (Caddy's internal CA). The gateway must **trust** that CA —
confirm with GreenNode how to use a custom CA/certificate for the connector. A more reliable approach: use a certificate issued by
an enterprise CA (one the gateway trusts), mount it into `./certs`, and enable the line `tls /certs/server.crt /certs/server.key`.
If you prefer nginx: use `proxy_pass http://127.0.0.1:8080;` with `proxy_buffering off;` and `listen 8443 ssl;`.

Remember to open the security group for port `8443` instead of `8080`.

## 4. Connect to MCP Gateway

1. **Gateway**: create a gateway with Network mode **Private** (select the VPC + Subnet of the vServer; **cannot be
   changed after creation**).
2. **Access Control**: create an API Key provider (e.g. `stock-mcp-key`) with exactly the value of `MCP_API_KEYS`.
3. **Connector**: Endpoint `https://xx.xx.x.x:8443/mcp` (or `http://xx.xx.x.x:8080/mcp` without TLS),
   Outbound Auth = **API Key**, header `X-Api-Key`, provider `stock-mcp-key`.
4. **Policy Group**: keep the `stock__<tool>` actions exactly as in the main README.

Quick check from another host in the VPC: `../onprem/check_connectivity.sh` (it also works for vServer).

## Updates / key rotation

```bash
docker compose up -d --build            # after pulling new code
# rotate key: MCP_API_KEYS="old_key,new_key" → docker compose up -d → change the key in Access Control → remove old_key
```
