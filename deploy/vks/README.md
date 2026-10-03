# Chạy MCP server trên GreenNode VKS (Kubernetes)

Cùng image với bản Agent Runtime, chạy thành Deployment 2 replica trong cluster **VKS** của
khách hàng, phơi ra qua **internal load balancer** để **MCP Gateway chế độ Private** gọi vào.

```
Agent → MCP Gateway (Private, 172.30.0.0/16) → kết nối private → internal LB (VPC khách hàng)
      → Service stock-mcp-server → 2 Pod :8080 → 24hMoney
```

| File | Vai trò |
|---|---|
| `namespace.yaml` | Namespace `stock-mcp` |
| `secret.example.yaml` | Mẫu Secret `MCP_API_KEYS` (khuyên tạo bằng `kubectl create secret`) |
| `deployment.yaml` | 2 replica, resources, probe `/health`, non-root, `envFrom` secret |
| `service.yaml` | `LoadBalancer` (nội bộ) + phương án `NodePort` |
| `networkpolicy.yaml` | Tuỳ chọn: chỉ cho `172.30.0.0/16` vào cổng 8080 |

## Yêu cầu

- Cluster VKS trong VPC đã **kết nối private với AgentBase** (liên hệ GreenNode support để kích hoạt),
  `kubectl` đã trỏ đúng cluster.
- Image đã có trên vCR.

## Các bước

```bash
cd deploy/vks

# 1. Build & push image (từ gốc repo)
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/stock-mcp-server:v1 ../..
docker push vcr.vngcloud.vn/<repo>/stock-mcp-server:v1
#    -> sửa `image:` trong deployment.yaml cho khớp (và thêm imagePullSecrets nếu repo vCR private)

# 2. Namespace
kubectl apply -f namespace.yaml

# 3. Secret — tạo bằng lệnh, không commit giá trị thật
export MCP_KEY=$(openssl rand -hex 32)      # GHI LẠI để lưu vào Access Control
kubectl -n stock-mcp create secret generic stock-mcp-secret \
  --from-literal=MCP_API_KEYS="$MCP_KEY"

# 4. Deployment + Service
kubectl apply -f deployment.yaml -f service.yaml
kubectl -n stock-mcp rollout status deploy/stock-mcp-server

# 5. (Tuỳ chọn) NetworkPolicy
kubectl apply -f networkpolicy.yaml

# 6. Lấy địa chỉ LB nội bộ
kubectl -n stock-mcp get svc stock-mcp-server      # cột EXTERNAL-IP = IP private xx.xx.x.x
```

## TODO trước khi dùng thật

- [ ] **Annotation internal LB** trong `service.yaml` — *thêm annotation internal LB theo docs vLB/VKS
      của GreenNode*. Chưa có annotation thì LB có thể được tạo ở dạng public, **đừng để lộ ra Internet**.
- [ ] Cổng LB: mẫu dùng `8080` (HTTP). Nếu connector dùng `https://xx.xx.x.x:8443/mcp`, terminate TLS
      ở LB/ingress rồi đổi `port` thành `8443` (xác nhận với GreenNode về TLS/CA mà gateway tin cậy).
- [ ] `networkpolicy.yaml`: xác nhận nguồn IP thấy được ở Pod (có SNAT hay không) rồi chỉnh CIDR.

## Nối vào MCP Gateway

1. Gateway: Network mode **Private** (chọn VPC + Subnet của cluster/LB, DNS resolution bật; không đổi được sau khi tạo).
2. Access Control: API Key provider (vd `stock-mcp-key`) = giá trị `$MCP_KEY`.
3. Connector `stock`: Endpoint `https://xx.xx.x.x:8443/mcp` (IP của LB nội bộ), Outbound Auth = **API Key**,
   header `X-Api-Key`, provider `stock-mcp-key`.
4. Policy Group: action `stock__<tool>` như README gốc.

Kiểm tra từ host trong VPC: `deploy/onprem/check_connectivity.sh`.

## Xoay key / cập nhật

```bash
kubectl -n stock-mcp create secret generic stock-mcp-secret \
  --from-literal=MCP_API_KEYS="key_cu,key_moi" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n stock-mcp rollout restart deploy/stock-mcp-server   # env chỉ đọc lúc khởi động
```

Dọn dẹp: `kubectl delete namespace stock-mcp`.
