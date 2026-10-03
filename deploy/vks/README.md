# Run the MCP server on GreenNode VKS (Kubernetes)

Uses the same image as the Agent Runtime variant, running as a 2-replica Deployment in the customer's **VKS** cluster,
exposed through an **internal load balancer** so that a **Private-mode MCP Gateway** can call it.

```
Agent → MCP Gateway (Private, 172.30.0.0/16) → private connection → internal LB (customer VPC)
      → Service stock-mcp-server → 2 Pods :8080 → 24hMoney
```

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace `stock-mcp` |
| `secret.example.yaml` | Sample `MCP_API_KEYS` Secret (creating it with `kubectl create secret` is recommended) |
| `deployment.yaml` | 2 replicas, resources, `/health` probe, non-root, `envFrom` secret |
| `service.yaml` | `LoadBalancer` (internal) + a `NodePort` alternative |
| `networkpolicy.yaml` | Optional: only allows `172.30.0.0/16` to port 8080 |

## Prerequisites

- A VKS cluster in a VPC that is **privately connected to AgentBase** (contact GreenNode support to enable it),
  with `kubectl` pointed at the right cluster.
- The image is available in vCR.

## Steps

```bash
cd deploy/vks

# 1. Build & push the image (from the repo root)
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 ../..
docker push vcr.vngcloud.vn/<repo>/stock-mcp-server:v1
#    -> update `image:` in deployment.yaml to match (and add imagePullSecrets if the vCR repo is private)

# 2. Namespace
kubectl apply -f namespace.yaml

# 3. Secret — create it from the command line; do not commit real values
export MCP_KEY=$(openssl rand -hex 32)      # RECORD IT to store in Access Control
kubectl -n stock-mcp create secret generic stock-mcp-secret \
  --from-literal=MCP_API_KEYS="$MCP_KEY"

# 4. Deployment + Service
kubectl apply -f deployment.yaml -f service.yaml
kubectl -n stock-mcp rollout status deploy/stock-mcp-server

# 5. (Optional) NetworkPolicy
kubectl apply -f networkpolicy.yaml

# 6. Get the internal LB address
kubectl -n stock-mcp get svc stock-mcp-server      # EXTERNAL-IP column = private IP xx.xx.x.x
```

## TODO before real use

- [ ] **Internal LB annotation** in `service.yaml` — *add the internal LB annotation per the GreenNode vLB/VKS
      documentation*. Without the annotation, the LB may be created as public; **do not expose it to the Internet**.
- [ ] LB port: the sample uses `8080` (HTTP). If the connector uses `https://xx.xx.x.x:8443/mcp`, terminate TLS
      at the LB/ingress and change `port` to `8443` (confirm with GreenNode which TLS/CA the gateway trusts).
- [ ] `networkpolicy.yaml`: confirm the source IP visible at the Pod (whether SNAT is applied) and then adjust the CIDR.

## Connect to MCP Gateway

1. Gateway: Network mode **Private** (select the VPC + Subnet of the cluster/LB, DNS resolution enabled; cannot be changed after creation).
2. Access Control: an API Key provider (e.g. `stock-mcp-key`) = the value of `$MCP_KEY`.
3. Connector `stock`: Endpoint `https://xx.xx.x.x:8443/mcp` (the internal LB's IP), Outbound Auth = **API Key**,
   header `X-Api-Key`, provider `stock-mcp-key`.
4. Policy Group: `stock__<tool>` actions as in the main README.

Verify from a host in the VPC: `deploy/onprem/check_connectivity.sh`.

## Key rotation / updates

```bash
kubectl -n stock-mcp create secret generic stock-mcp-secret \
  --from-literal=MCP_API_KEYS="old_key,new_key" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n stock-mcp rollout restart deploy/stock-mcp-server   # env is only read at startup
```

Cleanup: `kubectl delete namespace stock-mcp`.
