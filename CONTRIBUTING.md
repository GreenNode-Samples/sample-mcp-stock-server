# Contributing

Cảm ơn bạn quan tâm! Đây là sample repo minh hoạ mô hình **MCP server thuần** trên
GreenNode AgentBase (runtime → MCP Connector → MCP Gateway).

## Cách đóng góp

1. Fork → tạo branch: `git checkout -b feat/ten-tinh-nang`
2. Chạy test trước khi commit:
   ```bash
   pip install -r requirements.txt pytest pytest-asyncio
   python -m pytest tests/ -v
   ```
   Test là **hermetic** (không gọi network) — vui lòng giữ nguyên tính chất này khi
   thêm test mới (fake qua `monkeypatch`, xem `tests/conftest.py`).
3. Commit theo convention: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`
4. Pull Request + mô tả ngắn gọn

## Quy ước code

- Python 3.12, không cần formatter riêng — giữ style hiện tại (ruff-friendly)
- Tool mới: docstring tiếng Việt rõ ràng (đây là phần agent đọc để quyết định gọi tool)
- KHÔNG commit secret (`.env` đã bị gitignore)
- Dữ liệu chỉ từ API công khai 24hMoney — không thêm nguồn nào cần API key

## Lưu ý về dữ liệu

Nguồn: API công khai (không chính thức) của app 24hMoney. Chỉ dùng cho mục đích
demo/sample — không dùng cho giao dịch thật.
