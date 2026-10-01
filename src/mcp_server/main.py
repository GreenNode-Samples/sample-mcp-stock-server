"""VN Stock MCP Server — MCP server THUẦN (không agent, không LLM, không memory).

Mô hình mẫu: **1 runtime chỉ để expose tools** — deploy như Custom Agent runtime
trên AgentBase (port 8080, GET /health), rồi đăng ký làm **MCP Connector** vào
**MCP Gateway** → mọi agent trên gateway (được Policy cho phép) gọi tools cổ phiếu
qua gateway mà không cần biết server này ở đâu.

Dữ liệu: API công khai của 24hMoney (app 24hmoney.vn) — endpoint
`/v1/ios/stock-recommend/top-stock-all` trả về ~90 mã top thị trường Việt Nam
(giá, +/- %, trần/sàn, khối lượng, giá trị giao dịch, ngoại mua/bán).
Lưu ý: đây là API không chính thức của app — chỉ dùng cho mục đích demo/sample.

Tools (5) — tất cả cắt từ 1 nguồn dữ liệu trên:
  - market_top_stocks(limit, sort)  → bảng top cổ phiếu (tuỳ chọn sort)
  - top_gainers(limit)              → tăng mạnh nhất
  - top_losers(limit)               → giảm sâu nhất
  - most_active(limit)              → thanh khoản lớn nhất (theo giá trị)
  - stock_quote(symbol)             → báo giá 1 mã (VIC, TCB, …)

Tuỳ biến qua env (tất cả optional): STOCK_API_BASE_URL, HTTP_TIMEOUT_SECONDS,
CACHE_TTL_SECONDS.

Auth: đặt STOCK_API_KEY để /mcp yêu cầu header X-Stock-Api-Key (hMAC-so-sánh
constant-time). /health và / vẫn mở (runtime health-probe). Để trống = demo mở.
Khi nối qua MCP Gateway: đặt connector outboundAuth = APIKEY (provider trong
AgentBase Identity) — gateway tự gắn key khi forward, người gọi không bao giờ
nhìn thấy key này.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
import string
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.fastmcp import FastMCP

# ────────────────────────── Cấu hình (env-overridable) ──────────────────────────

API_BASE = os.environ.get(
    "STOCK_API_BASE_URL", "https://api-finance-t19.24hmoney.vn"
).rstrip("/")
TOP_STOCK_PATH = "/v1/ios/stock-recommend/top-stock-all"
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "60"))  # dữ liệu phiên thay đổi nhanh

# API key bảo vệ /mcp (set = bật; trống = demo mở không cần key)
STOCK_API_KEY = os.environ.get("STOCK_API_KEY", "").strip()

TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")
STALE_AFTER = 15 * 60  # quá 15 phút không cập nhật → có thể đã hết phiên

MAX_LIMIT = 30


def _browser_id() -> str:
    """device_id/browser_id kiểu app 24hmoney web sinh: web + epoch_ms + random."""
    rand = "".join(random.choices(string.hexdigits.lower(), k=24))
    return f"web{int(time.time() * 1000)}{rand}"


DEVICE_ID = _browser_id()

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi",
    "Origin": "https://24hmoney.vn",
    "Referer": "https://24hmoney.vn/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.4 Safari/605.1.15"
    ),
}

API_PARAMS = {
    "device_id": DEVICE_ID,
    "device_name": "INVALID",
    "device_model": "INVALID",
    "network_carrier": "INVALID",
    "connection_type": "INVALID",
    "os": "Safari",
    "os_version": "26.4",
    "access_token": "INVALID",
    "push_token": "INVALID",
    "locale": "vi",
    "browser_id": DEVICE_ID,
}

# ────────────────────────── HTTP + TTL cache (async) ──────────────────────────

_cache: dict[str, tuple[float, object]] = {}
_stats = {"upstream_calls": 0, "cache_hits": 0}


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        _stats["cache_hits"] += 1
        return hit[1]
    return None


def _cache_put(key: str, ttl: int, value) -> None:
    _cache[key] = (time.monotonic() + ttl, value)


async def fetch_top_stocks() -> dict:
    """GET top-stock-all từ 24hMoney với TTL cache + retry (GET idempotent)."""
    cached = _cache_get("top-stocks")
    if cached is not None:
        return cached

    url = API_BASE + TOP_STOCK_PATH
    last_err: Exception | None = None
    for attempt in range(3):  # 3 lần, backoff 0.5 → 1.5s
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=BROWSER_HEADERS) as client:
                res = await client.get(url, params=API_PARAMS)
                res.raise_for_status()
                body = res.json()
            if body.get("status") != 200 or not (body.get("data") or {}).get("stocks"):
                raise ValueError(f"payload lạ từ 24hMoney: status={body.get('status')}")
            _stats["upstream_calls"] += 1
            data = {"stocks": body["data"]["stocks"],
                    "last_update": body["data"].get("last_update")}
            _cache_put("top-stocks", CACHE_TTL, data)
            return data
        except (httpx.HTTPError, ValueError, KeyError) as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(0.5 * (3 ** attempt))
    raise RuntimeError(f"24hMoney API không phản hồi sau 3 lần thử: {last_err}")


# ────────────────────── Chuẩn hoá & xếp hạng ──────────────────────


def price_state(s: dict) -> str:
    """Trạng thái giá so với sàn/trần/tham chiếu."""
    price, basic = s.get("price"), s.get("basic_price")
    ceil, floor = s.get("ceiling_price"), s.get("floor_price")
    try:
        if price is not None and ceil is not None and price >= ceil:
            return "chạm trần"
        if price is not None and floor is not None and price <= floor:
            return "chạm sàn"
        if price is not None and basic is not None:
            if price > basic:
                return "tăng"
            if price < basic:
                return "giảm"
            return "đứng giá"
    except (TypeError, ValueError):
        pass
    return "—"


def _fmt_quote(s: dict, rank: int | None = None) -> dict:
    out = {
        "symbol": s.get("symbol"),
        "price": s.get("price"),
        "change": s.get("change"),
        "change_percent": s.get("change_percent"),
        "state": price_state(s),
        "reference_price": s.get("basic_price"),
        "ceiling_price": s.get("ceiling_price"),
        "floor_price": s.get("floor_price"),
        # field API ghi nhầm "accumylated_vol" — giữ nguyên khi đọc
        "matched_volume": s.get("accumylated_vol"),
        "matched_value_bn_vnd": s.get("accumulated_val"),  # tỷ VND
        "foreign_buy_volume": s.get("buy_foreign_qtty"),
        "foreign_sell_volume": s.get("sell_foreign_qtty"),
    }
    if rank is not None:
        out = {"rank": rank, **out}
    return out


def _market_meta(data: dict) -> dict:
    last_update = data.get("last_update")
    meta: dict = {"source": "24hmoney.vn", "stocks_tracked": len(data.get("stocks") or [])}
    if isinstance(last_update, (int, float)) and last_update > 0:
        # API trả epoch GIÂY (vd 1790845020 ≈ 2026-10-01); phòng hờ định dạng ms
        ts_sec = last_update / 1000 if last_update > 1e12 else float(last_update)
        ts = datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone(TZ_VN)
        meta["last_update"] = ts.strftime("%Y-%m-%d %H:%M:%S (GMT+7)")
        age = time.time() - ts_sec
        if age > STALE_AFTER:
            meta["note"] = (
                f"dữ liệu cập nhật lần cuối {int(age // 60)} phút trước — thị trường "
                "có thể đã đóng cửa hoặc API ngừng cập nhật"
            )
    return meta


SORTS = {
    "default": None,                                   # thứ tự API (top recommend)
    "change_percent": lambda s: s.get("change_percent") or 0.0,
    "value": lambda s: s.get("accumulated_val") or 0.0,
    "volume": lambda s: s.get("accumylated_vol") or 0.0,
    "foreign_net_buy": lambda s: (s.get("buy_foreign_qtty") or 0) - (s.get("sell_foreign_qtty") or 0),
}


def _clamp_limit(limit) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 10
    return max(1, min(MAX_LIMIT, limit))


# ────────────────────────────── MCP tools ──────────────────────────────

mcp = FastMCP("vn-stock", stateless_http=True, json_response=True, host="0.0.0.0")


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


@mcp.tool()
async def market_top_stocks(limit: int = 20, sort: str = "default") -> str:
    """Bảng top cổ phiếu Việt Nam (nguồn 24hMoney) — ~90 mã thanh khoản cao nhất.

    sort: 'default' (thứ tự khuyến nghị của 24hMoney) | 'change_percent' (+/-%)
          | 'value' (giá trị GD, tỷ VND) | 'volume' (khối lượng GD)
          | 'foreign_net_buy' (ngoại mua ròng).
    limit: 1–30 (mặc định 20). Ví dụ: market_top_stocks(limit=15, sort='value').
    """
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    stocks = list(data.get("stocks") or [])
    if sort not in SORTS:
        return _err(f"sort không hợp lệ ({sort}). Chọn: " + ", ".join(SORTS))
    key = SORTS[sort]
    if key:
        stocks.sort(key=key, reverse=True)
    n = _clamp_limit(limit)
    top = stocks[:n]
    return _ok({
        "meta": _market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    })


@mcp.tool()
async def top_gainers(limit: int = 10) -> str:
    """Top cổ phiếu TĂNG mạnh nhất hôm nay (theo % tăng, chỉ mã đang tăng).

    limit: 1–30 (mặc định 10). Ví dụ: top_gainers(limit=5).
    """
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    gainers = [s for s in data.get("stocks") or []
               if (s.get("change_percent") or 0) > 0]
    gainers.sort(key=lambda s: s.get("change_percent") or 0.0, reverse=True)
    n = _clamp_limit(limit)
    top = gainers[:n]
    return _ok({
        "meta": _market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    })


@mcp.tool()
async def top_losers(limit: int = 10) -> str:
    """Top cổ phiếu GIẢM sâu nhất hôm nay (theo % giảm, chỉ mã đang giảm).

    limit: 1–30 (mặc định 10). Ví dụ: top_losers(limit=5).
    """
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    losers = [s for s in data.get("stocks") or []
              if (s.get("change_percent") or 0) < 0]
    losers.sort(key=lambda s: s.get("change_percent") or 0.0)
    n = _clamp_limit(limit)
    top = losers[:n]
    return _ok({
        "meta": _market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    })


@mcp.tool()
async def most_active(limit: int = 10) -> str:
    """Top cổ phiếu THANH KHOẢN lớn nhất (theo giá trị giao dịch, tỷ VND).

    limit: 1–30 (mặc định 10). Ví dụ: most_active(limit=15).
    """
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    actives = sorted(data.get("stocks") or [],
                     key=lambda s: s.get("accumulated_val") or 0.0, reverse=True)
    n = _clamp_limit(limit)
    top = actives[:n]
    return _ok({
        "meta": _market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    })


@mcp.tool()
async def stock_quote(symbol: str) -> str:
    """Báo giá 1 mã trong nhóm top (~90 mã thanh khoản cao): giá, +/-%, trần/sàn,
    khối lượng & giá trị khớp lệnh, khối lượng ngoại mua/bán.

    Ví dụ: stock_quote(symbol='VIC') hoặc stock_quote(symbol='vic').
    """
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    sym = (symbol or "").strip().upper()
    for s in data.get("stocks") or []:
        if (s.get("symbol") or "").upper() == sym:
            return _ok({"meta": _market_meta(data), "quote": _fmt_quote(s)})
    return _err(
        f"Không tìm thấy mã '{symbol}' trong nhóm top (~{len(data.get('stocks') or [])} mã "
        "thanh khoản cao nhất). Dùng market_top_stocks để xem danh sách."
    )


# ────────────────────────── HTTP app (runtime contract) ──────────────────────────


async def health(request):
    return JSONResponse({
        "status": "ok",
        "server": "vn-stock-mcp",
        "tools": 5,
        "mcp_endpoint": "/mcp",
        "mcp_auth": "X-Stock-Api-Key" if STOCK_API_KEY else "open (demo mode)",
        "cache": _stats,
    })


async def root(request):
    return JSONResponse({
        "server": "vn-stock-mcp",
        "what": "pure MCP server — no LLM, no memory; stock tools served to agents via MCP Gateway",
        "mcp_endpoint": "/mcp",
        "tools": ["market_top_stocks", "top_gainers", "top_losers",
                  "most_active", "stock_quote"],
        "data_source": "24hmoney.vn (unofficial public API)",
    })


# streamable_http_app() trả Starlette app (lifespan chạy session manager).
# MCP streamable HTTP mặc định tại /mcp; append routes phụ trợ vào CHÍNH app này
# (Mount vào app khác sẽ không chạy lifespan của sub-app).
asgi_app = mcp.streamable_http_app()
asgi_app.router.routes.append(Route("/health", health, methods=["GET"]))
asgi_app.router.routes.append(Route("/", root, methods=["GET"]))


class _RequireApiKeyMiddleware:
    """ASGI middleware: /mcp yêu cầu X-Stock-Api-Key khớp STOCK_API_KEY.

    - /health và / không cần key (health-probe của runtime phải luôn 200).
    - STOCK_API_KEY trống → middleware tắt hoàn toàn (chế độ demo mở).
    - So sánh constant-time (secrets.compare_digest) chống timing attack.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("path", "").rstrip("/") == "/mcp"
            and STOCK_API_KEY
        ):
            supplied = ""
            for k, v in scope.get("headers") or []:
                if k.decode("latin-1").lower() == "x-stock-api-key":
                    supplied = v.decode("latin-1")
                    break
            if not secrets.compare_digest(supplied, STOCK_API_KEY):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error":"missing or invalid X-Stock-Api-Key"}',
                })
                return
        await self.app(scope, receive, send)


app = _RequireApiKeyMiddleware(asgi_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
