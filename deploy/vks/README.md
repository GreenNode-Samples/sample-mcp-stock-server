# Run the MCP server on GreenNode VKS (Kubernetes)

Uses the same image as the Agent Runtime variant, running as a 2-replica Deployment in the customer's **VKS** cluster,
exposed on the nodes' **private** addresses through a `NodePort` Service so that a **Private-mode MCP Gateway** can
call it. These manifests create no public endpoint.

```
Agent → MCP Gateway (Private, 172.30.0.0/16) → private connection → worker node <private IP>:30080
      → Service stock-mcp-server → 2 Pods :8080 → 24hMoney
```

The connection is plain HTTP (`http://<node-private-ip>:30080/mcp`). It stays inside the private network
(AgentBase VPC ↔ your VPC), but no TLS component ships with this sample: see [TLS](#tls) if you need HTTPS.

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace `stock-mcp` |
| `secret.example.yaml` | Sample `MCP_API_KEYS` Secret with a placeholder the server rejects (creating the Secret with `kubectl create secret` is recommended) |
| `deployment.yaml` | 2 replicas, resources, `/health` probe, non-root, `envFrom` secret |
| `service.yaml` | `NodePort` Service on port `30080` (internal only) |
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

# 6. Get the private IP of a worker node (INTERNAL-IP column)
kubectl get nodes -o wide
```

## Before real use

- [ ] **Security group of the worker nodes**: allow TCP `30080` only from `172.30.0.0/16` (the AgentBase VPC range)
      and make sure the nodes have no public IP. Confirm with GreenNode whether the gateway's source address is
      NATed before opening the rule.
- [ ] `networkpolicy.yaml`: confirm the source IP the pods see (whether SNAT is applied) and adjust the CIDR.
- [ ] High availability: the connector points to ONE node IP. If that node is replaced, update the connector;
      for an address that survives node changes use an internal load balancer instead (next section).

### Internal load balancer (not included)

Do **not** change the Service to `type: LoadBalancer` as is: without an internal-LB annotation the cloud may create
a public load balancer. The annotation to use comes from the GreenNode vLB / VKS documentation and is not part of this
sample because it has not been verified. If you add one, keep the Service type `LoadBalancer`, point the connector
to the LB's private address and port `8080`, and remove the `nodePort`.

### TLS

This sample serves HTTP only. For HTTPS terminate TLS in front of the Service (an ingress controller or an internal
load balancer with a certificate that the gateway trusts) and use `https://<address>/mcp` as the connector endpoint.
Confirm with GreenNode which CA the gateway trusts for custom certificates.

## Connect to MCP Gateway

1. Gateway: Network mode **Private** (select the VPC + Subnet of the cluster/LB, DNS resolution enabled; cannot be changed after creation).
2. Access Control: an API Key provider (e.g. `stock-mcp-key`) = the value of `$MCP_KEY`.
3. Connector `stock`: Endpoint `http://<node-private-ip>:30080/mcp` (exactly `/mcp`, no trailing slash),
   Outbound Auth = **API Key**, header `X-Api-Key`, provider `stock-mcp-key`.
4. Policy Group: `stock__<tool>` actions as in the main README.

Verify from a host in the VPC: `MCP_API_KEY=<key> ../onprem/check_connectivity.sh <node-private-ip> 30080 http`.

## Key rotation / updates

```bash
kubectl -n stock-mcp create secret generic stock-mcp-secret \
  --from-literal=MCP_API_KEYS="old_key,new_key" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n stock-mcp rollout restart deploy/stock-mcp-server   # keys are read once at startup, so a restart is required
```

Cleanup: `kubectl delete namespace stock-mcp`.
