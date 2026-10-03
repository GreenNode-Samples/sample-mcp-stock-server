"""VN Stock MCP Server — MCP server THUẦN (không agent, không LLM, không memory).

Mô hình mẫu: **1 Agent Runtime chỉ để expose tools** — deploy như Custom Agent runtime
trên AgentBase (port 8080, GET /health), rồi đăng ký làm **MCP Connector** trong
**MCP Gateway** → mọi agent đi qua gateway (được Policy Group cho phép) gọi tools
chứng khoán Việt Nam mà không cần biết server này ở đâu.

Dữ liệu: các API công khai mà web/app 24hmoney.vn dùng (`api-finance-t19.24hmoney.vn`).
Đây là API không chính thức — chỉ dùng cho demo/sample, không dùng cho giao dịch thật.

Tools (13):
  Thị trường (1 nguồn top-stock-all, cache 60s)
    - market_top_stocks(limit, sort) · top_gainers · top_losers · most_active · stock_quote
  Doanh nghiệp / mã cổ phiếu
    - search_company(query)       → tìm mã theo tên / mã (1.6k công ty HOSE · HNX · UPCOM)
    - company_profile(symbol)     → tên, sàn, mô tả doanh nghiệp
    - price_history(symbol, days) → giá đóng cửa, KL, GT theo ngày (≤ 30 phiên)
    - foreign_trading(symbol, days) → khối ngoại mua/bán theo ngày (≤ 25 phiên)
    - valuation(symbol)           → P/E, P/B, ROE, ROA, EPS… so với trung bình ngành
    - dividend_history(symbol)    → lịch sử cổ tức (tiền mặt / cổ phiếu)
    - business_plan(symbol)       → kế hoạch năm & % hoàn thành
    - company_announcements(symbol) → tin công bố thông tin (kèm link PDF)

Auth (fail-closed): /mcp bắt buộc API key.
  - MCP_API_KEYS="key1,key2"  (nhiều key để xoay vòng; STOCK_API_KEY vẫn được nhận)
  - Header: `X-Api-Key: <key>` hoặc `Authorization: Bearer <key>` (`X-Stock-Api-Key` cũ vẫn nhận)
  - Không cấu hình key → /mcp trả 503 (không bao giờ tự mở). Chỉ khi chạy local mới
    đặt ALLOW_ANONYMOUS=true.
  - /health và / luôn mở (runtime health-probe).
  Khi đi qua MCP Gateway: connector `stock` đặt Outbound Auth = API Key, secret lưu ở
  Access Control → gateway gắn header khi forward, agent không bao giờ thấy key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import string
import time
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vn-stock-mcp")

# ────────────────────────── Cấu hình (env-overridable) ──────────────────────────

API_BASE = os.environ.get(
    "STOCK_API_BASE_URL", "https://api-finance-t19.24hmoney.vn"
).rstrip("/")
TOP_STOCK_PATH = "/v1/ios/stock-recommend/top-stock-all"
COMPANY_ALL_PATH = "/v1/ios/company/all"
TRADING_HISTORY_PATH = "/v2/ios/stock/trading-history"
FOREIGN_HISTORY_PATH = "/v1/ios/stock/foreign-trading-history"
VALUATION_PATH = "/v1/web/company/financial-report-summary"
DIVIDEND_PATH = "/v1/ios/company/dividend-schedule"
PLAN_PATH = "/v1/ios/company/plan"
ANNOUNCEMENT_PATH = "/v1/web/announcement"

HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "60"))        # dữ liệu phiên
SLOW_TTL = int(os.environ.get("SLOW_CACHE_TTL_SECONDS", "900"))    # cổ tức, kế hoạch, định giá, tin
COMPANY_TTL = int(os.environ.get("COMPANY_CACHE_TTL_SECONDS", "21600"))  # danh bạ công ty (6h)


def _load_api_keys() -> list[str]:
    raw = os.environ.get("MCP_API_KEYS") or os.environ.get("STOCK_API_KEY") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


API_KEYS = _load_api_keys()
ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "").strip().lower() in ("1", "true", "yes")
for _k in API_KEYS:
    if len(_k) < 24:
        log.warning("MCP_API_KEYS có key ngắn hơn 24 ký tự — nên dùng `openssl rand -hex 32`")

TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")
STALE_AFTER = 15 * 60  # quá 15 phút không cập nhật → có thể đã hết phiên

MAX_LIMIT = 30
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")


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


async def fetch_api(path: str, params: dict | None = None, *, cache_key: str, ttl: int):
    """GET 1 endpoint 24hMoney → trả `data` (dict/list). TTL cache + retry 3 lần (GET idempotent)."""
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = API_BASE + path
    query = {**API_PARAMS, **(params or {})}
    last_err: Exception | None = None
    for attempt in range(3):  # backoff 0.5 → 1.5s
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=BROWSER_HEADERS) as client:
                res = await client.get(url, params=query)
                res.raise_for_status()
                body = res.json()
            if body.get("status") != 200 or "data" not in body:
                raise ValueError(f"payload lạ từ 24hMoney: status={body.get('status')}")
            _stats["upstream_calls"] += 1
            data = body["data"]
            _cache_put(cache_key, ttl, data)
            return data
        except (httpx.HTTPError, ValueError, KeyError) as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(0.5 * (3 ** attempt))
    raise RuntimeError(f"24hMoney API không phản hồi sau 3 lần thử: {last_err}")


async def fetch_top_stocks() -> dict:
    """~90 mã top thị trường (giá, +/-%, KL, GT, ngoại)."""
    data = await fetch_api(TOP_STOCK_PATH, cache_key="top-stocks", ttl=CACHE_TTL)
    if not isinstance(data, dict) or not data.get("stocks"):
        _cache.pop("top-stocks", None)
        raise RuntimeError("24hMoney trả về danh sách top cổ phiếu rỗng")
    return {"stocks": data["stocks"], "last_update": data.get("last_update")}


async def fetch_companies() -> list[dict]:
    """Danh bạ ~1.6k công ty niêm yết (HOSE · HNX · UPCOM) — cache 6h."""
    data = await fetch_api(COMPANY_ALL_PATH, {"time_updated": 0},
                           cache_key="companies", ttl=COMPANY_TTL)
    return data if isinstance(data, list) else []


# ────────────────────── Chuẩn hoá & tiện ích ──────────────────────


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


def _epoch_to_vn(ts) -> datetime | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    ts_sec = ts / 1000 if ts > 1e12 else float(ts)  # API trả epoch giây; phòng hờ ms
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).astimezone(TZ_VN)


def _vn_date(ts) -> str | None:
    d = _epoch_to_vn(ts)
    return d.strftime("%Y-%m-%d") if d else None


def _market_meta(data: dict) -> dict:
    meta: dict = {"source": "24hmoney.vn", "stocks_tracked": len(data.get("stocks") or []),
                  "units": "giá: nghìn VND · giá trị: tỷ VND"}
    ts = _epoch_to_vn(data.get("last_update"))
    if ts:
        meta["last_update"] = ts.strftime("%Y-%m-%d %H:%M:%S (GMT+7)")
        age = time.time() - ts.timestamp()
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


def _clamp(value, default: int, hi: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(hi, value))


def _clamp_limit(limit) -> int:
    return _clamp(limit, 10, MAX_LIMIT)


def _norm_symbol(symbol) -> str | None:
    sym = (symbol or "").strip().upper()
    return sym if SYMBOL_RE.match(sym) else None


def _fold(text: str) -> str:
    """Bỏ dấu tiếng Việt + lowercase để tìm kiếm ('Hòa Phát' ~ 'hoa phat')."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


# ────────────────────────────── MCP tools ──────────────────────────────

mcp = FastMCP("vn-stock", stateless_http=True, json_response=True, host="0.0.0.0")


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def _bad_symbol(symbol) -> str:
    return _err(f"Mã cổ phiếu không hợp lệ: '{symbol}'. Ví dụ hợp lệ: FPT, VCB, HPG. "
                "Dùng search_company nếu chưa biết mã.")


async def _ranked(limit, key, keep=lambda s: True, reverse=True) -> str:
    try:
        data = await fetch_top_stocks()
    except RuntimeError as e:
        return _err(str(e))
    stocks = [s for s in data.get("stocks") or [] if keep(s)]
    if key:
        stocks.sort(key=key, reverse=reverse)
    top = stocks[:_clamp_limit(limit)]
    return _ok({
        "meta": _market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    })


@mcp.tool()
async def market_top_stocks(limit: int = 20, sort: str = "default") -> str:
    """Bảng top cổ phiếu Việt Nam (nguồn 24hMoney) — ~90 mã thanh khoản cao nhất.

    sort: 'default' (thứ tự khuyến nghị của 24hMoney) | 'change_percent' (+/-%)
          | 'value' (giá trị GD, tỷ VND) | 'volume' (khối lượng GD)
          | 'foreign_net_buy' (ngoại mua ròng).
    limit: 1–30 (mặc định 20). Ví dụ: market_top_stocks(limit=15, sort='value').
    """
    if sort not in SORTS:
        return _err(f"sort không hợp lệ ({sort}). Chọn: " + ", ".join(SORTS))
    return await _ranked(limit, SORTS[sort])


@mcp.tool()
async def top_gainers(limit: int = 10) -> str:
    """Top cổ phiếu TĂNG mạnh nhất hôm nay (theo % tăng, chỉ mã đang tăng).

    limit: 1–30 (mặc định 10). Ví dụ: top_gainers(limit=5).
    """
    return await _ranked(limit, lambda s: s.get("change_percent") or 0.0,
                         keep=lambda s: (s.get("change_percent") or 0) > 0)


@mcp.tool()
async def top_losers(limit: int = 10) -> str:
    """Top cổ phiếu GIẢM sâu nhất hôm nay (theo % giảm, chỉ mã đang giảm).

    limit: 1–30 (mặc định 10). Ví dụ: top_losers(limit=5).
    """
    return await _ranked(limit, lambda s: s.get("change_percent") or 0.0,
                         keep=lambda s: (s.get("change_percent") or 0) < 0, reverse=False)


@mcp.tool()
async def most_active(limit: int = 10) -> str:
    """Top cổ phiếu THANH KHOẢN lớn nhất (theo giá trị giao dịch, tỷ VND).

    limit: 1–30 (mặc định 10). Ví dụ: most_active(limit=15).
    """
    return await _ranked(limit, lambda s: s.get("accumulated_val") or 0.0)


@mcp.tool()
async def stock_quote(symbol: str) -> str:
    """Báo giá 1 mã trong nhóm top (~90 mã thanh khoản cao): giá, +/-%, trần/sàn,
    khối lượng & giá trị khớp lệnh, khối lượng ngoại mua/bán.

    Mã ngoài nhóm top: dùng price_history(symbol, days=1).
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
        "thanh khoản cao nhất). Dùng market_top_stocks để xem danh sách, hoặc "
        "price_history(symbol) cho mã bất kỳ."
    )


@mcp.tool()
async def search_company(query: str, limit: int = 10) -> str:
    """Tìm mã cổ phiếu theo tên công ty hoặc mã (không phân biệt dấu, hoa/thường).

    Phủ ~1.6k doanh nghiệp niêm yết trên HOSE, HNX, UPCOM.
    Ví dụ: search_company('hoa phat'), search_company('ngân hàng ngoại thương'), search_company('FPT').
    """
    q = _fold(query).strip()
    if len(q) < 2:
        return _err("query cần ít nhất 2 ký tự")
    try:
        companies = await fetch_companies()
    except RuntimeError as e:
        return _err(str(e))
    scored = []
    for c in companies:
        sym = (c.get("symbol") or "").lower()
        names = _fold(" ".join(filter(None, [c.get("company_name"), c.get("short_name"),
                                             c.get("company_name_eng"), c.get("extra_name")])))
        if sym == q:
            score = 0
        elif sym.startswith(q):
            score = 1
        elif q in names:
            score = 2
        else:
            continue
        exch = {"HOSE": 0, "HNX": 1, "UPCOM": 2}.get(c.get("floor"), 3)
        # priority=1: ~72 doanh nghiệp lớn / quen thuộc → xếp trước (HPG trước HPA khi tìm 'hoa phat')
        prio = 1 if c.get("priority") in (1, "1", True) else 0
        scored.append((score, -prio, exch, sym, c))
    scored.sort(key=lambda t: t[:4])
    top = scored[:_clamp_limit(limit)]
    return _ok({
        "query": query,
        "count": len(top),
        "results": [{"symbol": c.get("symbol"), "company_name": c.get("company_name"),
                     "exchange": c.get("floor")} for *_, c in top],
    })


@mcp.tool()
async def company_profile(symbol: str) -> str:
    """Hồ sơ doanh nghiệp: tên đầy đủ (VN/EN), sàn niêm yết, mô tả hoạt động.

    Ví dụ: company_profile(symbol='HPG').
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        companies = await fetch_companies()
    except RuntimeError as e:
        return _err(str(e))
    for c in companies:
        if (c.get("symbol") or "").upper() == sym:
            desc = (c.get("description") or "").strip()
            return _ok({
                "symbol": sym,
                "company_name": c.get("company_name"),
                "company_name_eng": c.get("company_name_eng"),
                "short_name": c.get("short_name"),
                "exchange": c.get("floor"),
                "description": desc[:1200] + ("…" if len(desc) > 1200 else ""),
                "source": "24hmoney.vn",
            })
    return _err(f"Không tìm thấy doanh nghiệp có mã '{sym}'. Thử search_company.")


@mcp.tool()
async def price_history(symbol: str, days: int = 10) -> str:
    """Lịch sử giá theo phiên (mới nhất trước): giá đóng cửa, tham chiếu, +/-%,
    khối lượng & giá trị khớp lệnh. Có tóm tắt cả kỳ (tăng/giảm %, cao nhất, thấp nhất).

    days: 1–30 phiên (mặc định 10). Ví dụ: price_history(symbol='FPT', days=20).
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        rows = await fetch_api(TRADING_HISTORY_PATH, {"symbol": sym},
                               cache_key=f"history:{sym}", ttl=CACHE_TTL)
    except RuntimeError as e:
        return _err(str(e))
    rows = sorted([r for r in rows or [] if isinstance(r, dict)],
                  key=lambda r: r.get("trading_date") or 0, reverse=True)
    if not rows:
        return _err(f"Không có lịch sử giá cho mã '{sym}'.")
    rows = rows[:_clamp(days, 10, 30)]
    out = []
    for r in rows:
        close, ref = r.get("match_price"), r.get("basic_price")
        chg = round(close - ref, 2) if close is not None and ref else None
        out.append({
            "date": _vn_date(r.get("trading_date")),
            "close": close,
            "reference": ref,
            "change": chg,
            "change_percent": round(chg / ref * 100, 2) if chg is not None else None,
            "volume": r.get("accumulated_vol"),
            "value_bn_vnd": r.get("accumulated_val"),
        })
    closes = [r["close"] for r in out if r["close"] is not None]
    first_ref = rows[-1].get("basic_price")
    summary = {
        "sessions": len(out),
        "from": out[-1]["date"], "to": out[0]["date"],
        "high": max(closes) if closes else None,
        "low": min(closes) if closes else None,
        "period_change_percent": (round((closes[0] - first_ref) / first_ref * 100, 2)
                                  if closes and first_ref else None),
    }
    return _ok({"symbol": sym, "units": "giá: nghìn VND · giá trị: tỷ VND",
                "summary": summary, "sessions": out})


@mcp.tool()
async def foreign_trading(symbol: str, days: int = 10) -> str:
    """Giao dịch khối ngoại theo phiên: khối lượng & giá trị mua/bán, mua ròng.

    days: 1–25 phiên (mặc định 10). Ví dụ: foreign_trading(symbol='VNM', days=5).
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        rows = await fetch_api(FOREIGN_HISTORY_PATH, {"symbol": sym},
                               cache_key=f"foreign:{sym}", ttl=CACHE_TTL)
    except RuntimeError as e:
        return _err(str(e))
    rows = sorted([r for r in rows or [] if isinstance(r, dict)],
                  key=lambda r: r.get("trading_date") or 0, reverse=True)
    if not rows:
        return _err(f"Không có dữ liệu khối ngoại cho mã '{sym}'.")
    rows = rows[:_clamp(days, 10, 25)]
    out = []
    for r in rows:
        bv, sv = r.get("buy_foreign_val") or 0, r.get("sell_foreign_val") or 0
        out.append({
            "date": _vn_date(r.get("trading_date")),
            "close": r.get("match_price"),
            "buy_volume": r.get("buy_foreign_qtty"),
            "sell_volume": r.get("sell_foreign_qtty"),
            "buy_value_bn_vnd": r.get("buy_foreign_val"),
            "sell_value_bn_vnd": r.get("sell_foreign_val"),
            "net_value_bn_vnd": round(bv - sv, 3),
        })
    net = round(sum(r["net_value_bn_vnd"] for r in out), 3)
    return _ok({
        "symbol": sym,
        "units": "giá: nghìn VND · giá trị: tỷ VND",
        "summary": {"sessions": len(out), "net_value_bn_vnd": net,
                    "trend": "mua ròng" if net > 0 else "bán ròng" if net < 0 else "cân bằng"},
        "sessions": out,
    })


@mcp.tool()
async def valuation(symbol: str) -> str:
    """Chỉ số định giá & hiệu quả: P/E, P/B (so với trung bình ngành), ROE, ROA, EPS,
    biên lợi nhuận ròng, EV/EBITDA — kèm nhận xét ngắn của 24hMoney.

    Ví dụ: valuation(symbol='FPT').
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        data = await fetch_api(VALUATION_PATH, {"symbol": sym},
                               cache_key=f"valuation:{sym}", ttl=SLOW_TTL)
    except RuntimeError as e:
        return _err(str(e))
    if not isinstance(data, dict) or not data:
        return _err(f"Không có dữ liệu định giá cho mã '{sym}'.")
    labels = {"pe": "P/E", "pb": "P/B", "roe": "ROE (%)", "roa": "ROA (%)", "eps": "EPS (VND)",
              "net_profit_margin": "Biên LN ròng (%)", "ev_per_ebitda": "EV/EBITDA"}
    metrics = {}
    for k, label in labels.items():
        v = data.get(k)
        if isinstance(v, dict) and v.get("value") is not None:
            item = {"label": label, "value": round(v["value"], 2)}
            if v.get("group_value") is not None:
                item["industry_avg"] = v["group_value"]
            if v.get("message"):
                item["note"] = v["message"]
            metrics[k] = item
    return _ok({"symbol": sym, "industry": data.get("group_name"),
                "metrics": metrics, "source": "24hmoney.vn"})


DIVIDEND_TYPES = {1: "tiền mặt", 2: "cổ phiếu", 3: "cổ phiếu thưởng / phát hành tăng vốn"}


@mcp.tool()
async def dividend_history(symbol: str, limit: int = 10) -> str:
    """Lịch sử chia cổ tức (mới nhất trước): ngày, hình thức, tỷ lệ.

    Tỷ lệ tính trên mệnh giá 10.000 VND (vd 10% tiền mặt = 1.000 VND/cp).
    limit: 1–30 (mặc định 10). Ví dụ: dividend_history(symbol='FPT').
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        rows = await fetch_api(DIVIDEND_PATH, {"symbol": sym},
                               cache_key=f"dividend:{sym}", ttl=SLOW_TTL)
    except RuntimeError as e:
        return _err(str(e))
    rows = sorted([r for r in rows or [] if isinstance(r, dict)],
                  key=lambda r: r.get("end_date") or "", reverse=True)
    if not rows:
        return _err(f"Không có lịch sử cổ tức cho mã '{sym}'.")
    out = []
    for r in rows[:_clamp_limit(limit)]:
        ratio = r.get("ratio")
        item = {"date": r.get("end_date"),
                "type": DIVIDEND_TYPES.get(r.get("type"), f"khác ({r.get('type')})"),
                "ratio_percent": round(ratio * 100, 2) if isinstance(ratio, (int, float)) else None}
        if r.get("type") == 1 and isinstance(ratio, (int, float)):
            item["cash_vnd_per_share"] = round(ratio * 10_000)
        out.append(item)
    return _ok({"symbol": sym, "count": len(out), "dividends": out, "source": "24hmoney.vn"})


@mcp.tool()
async def business_plan(symbol: str) -> str:
    """Kế hoạch kinh doanh năm (doanh thu, lợi nhuận) và % hoàn thành tới quý gần nhất.

    Đơn vị: tỷ VND. Ví dụ: business_plan(symbol='MWG').
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    try:
        data = await fetch_api(PLAN_PATH, {"symbol": sym},
                               cache_key=f"plan:{sym}", ttl=SLOW_TTL)
    except RuntimeError as e:
        return _err(str(e))
    if not isinstance(data, dict) or not data.get("plan"):
        return _err(f"Không có kế hoạch kinh doanh cho mã '{sym}'.")
    return _ok({
        "symbol": sym,
        "year": data.get("year"),
        "through_quarter": data.get("quarter"),
        "units": "tỷ VND",
        "plan": [{"item": p.get("label"), "target": p.get("expect"),
                  "actual": p.get("current"), "completed_percent": p.get("percent")}
                 for p in data["plan"] if isinstance(p, dict)],
        "source": "24hmoney.vn",
    })


@mcp.tool()
async def company_announcements(symbol: str, limit: int = 5) -> str:
    """Tin công bố thông tin mới nhất của doanh nghiệp (tiêu đề, ngày, link tài liệu).

    limit: 1–20 (mặc định 5). Ví dụ: company_announcements(symbol='VCB', limit=10).
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return _bad_symbol(symbol)
    n = _clamp(limit, 5, 20)
    try:
        rows = await fetch_api(ANNOUNCEMENT_PATH, {"symbol": sym, "per_page": 20, "timestamp": 0},
                               cache_key=f"announce:{sym}", ttl=SLOW_TTL)
    except RuntimeError as e:
        return _err(str(e))
    rows = [r for r in rows or [] if isinstance(r, dict)]
    if not rows:
        return _err(f"Không có tin công bố cho mã '{sym}'.")
    out = []
    for r in rows[:n]:
        links = r.get("link") or []
        out.append({"date": _vn_date(r.get("published_date")),
                    "title": r.get("title"),
                    "url": links[0] if isinstance(links, list) and links else None})
    return _ok({"symbol": sym, "count": len(out), "announcements": out, "source": "24hmoney.vn"})


TOOL_NAMES = ["market_top_stocks", "top_gainers", "top_losers", "most_active", "stock_quote",
              "search_company", "company_profile", "price_history", "foreign_trading",
              "valuation", "dividend_history", "business_plan", "company_announcements"]

# ────────────────────────── HTTP app (runtime contract) ──────────────────────────


def _auth_mode() -> str:
    if API_KEYS:
        return f"api-key ({len(API_KEYS)} key)"
    return "anonymous (ALLOW_ANONYMOUS — chỉ dùng local)" if ALLOW_ANONYMOUS else "locked (chưa cấu hình MCP_API_KEYS)"


async def health(request):
    return JSONResponse({
        "status": "ok",
        "server": "vn-stock-mcp",
        "tools": len(TOOL_NAMES),
        "mcp_endpoint": "/mcp",
        "mcp_auth": _auth_mode(),
        "cache": _stats,
    })


async def root(request):
    return JSONResponse({
        "server": "vn-stock-mcp",
        "what": "pure MCP server — no LLM, no memory; stock tools served to agents via MCP Gateway",
        "mcp_endpoint": "/mcp",
        "auth": "X-Api-Key: <key>  hoặc  Authorization: Bearer <key>",
        "tools": TOOL_NAMES,
        "data_source": "24hmoney.vn (unofficial public API)",
    })


# streamable_http_app() trả Starlette app (lifespan chạy session manager).
# MCP streamable HTTP mặc định tại /mcp; append routes phụ trợ vào CHÍNH app này
# (Mount vào app khác sẽ không chạy lifespan của sub-app).
asgi_app = mcp.streamable_http_app()
asgi_app.router.routes.append(Route("/health", health, methods=["GET"]))
asgi_app.router.routes.append(Route("/", root, methods=["GET"]))


def _extract_key(headers) -> str:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers or []}
    for name in ("x-api-key", "x-stock-api-key"):
        if h.get(name):
            return h[name].strip()
    auth = h.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _key_valid(supplied: str) -> bool:
    if not supplied:
        return False
    ok = False
    for good in API_KEYS:  # duyệt hết key → thời gian không lộ key nào khớp
        ok |= secrets.compare_digest(supplied.encode(), good.encode())
    return ok


class RequireApiKeyMiddleware:
    """ASGI middleware fail-closed cho /mcp.

    - Có MCP_API_KEYS → bắt buộc key hợp lệ (401 nếu thiếu/sai).
    - Không có key + ALLOW_ANONYMOUS → mở (chỉ cho local dev).
    - Không có key, không ALLOW_ANONYMOUS → 503, không bao giờ tự mở.
    - /health và / luôn mở (health-probe của runtime phải 200).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reply(send, status: int, message: str, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"error": message}, ensure_ascii=False).encode()})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path.rstrip("/") == "/mcp" or path.startswith("/mcp/")):
            if not API_KEYS:
                if not ALLOW_ANONYMOUS:
                    return await self._reply(send, 503, "MCP server chưa cấu hình MCP_API_KEYS (fail-closed)")
            elif not _key_valid(_extract_key(scope.get("headers"))):
                client = (scope.get("client") or ("?",))[0]
                log.warning("401 /mcp từ %s — thiếu hoặc sai API key", client)
                return await self._reply(send, 401, "missing or invalid API key (X-Api-Key / Bearer)",
                                         [(b"www-authenticate", b'Bearer realm="vn-stock-mcp"')])
        await self.app(scope, receive, send)


app = RequireApiKeyMiddleware(asgi_app)


if __name__ == "__main__":
    import uvicorn

    log.info("vn-stock-mcp · %d tools · auth: %s", len(TOOL_NAMES), _auth_mode())
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
