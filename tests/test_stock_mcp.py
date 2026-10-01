"""Tests — VN Stock MCP Server (hermetic: không gọi network thật).

API 24hMoney được fake qua monkeypatch httpx.AsyncClient (xem conftest.py).
"""

import json
import time

import pytest

from conftest import make_api_payload, patch_api, SAMPLE_STOCKS, FRESH_S, STALE_S


def parse(raw: str) -> dict:
    return json.loads(raw)


def fresh_payload(stocks=None):
    return make_api_payload(stocks if stocks is not None else SAMPLE_STOCKS, FRESH_S)


# ─────────────── price_state ───────────────


def test_price_state_basic(m):
    vic = next(s for s in SAMPLE_STOCKS if s["symbol"] == "VIC")    # 220 < basic 227.9
    tcb = next(s for s in SAMPLE_STOCKS if s["symbol"] == "TCB")    # 35.5 > basic 34.2
    ssi = next(s for s in SAMPLE_STOCKS if s["symbol"] == "SSI")    # bằng basic
    assert m.price_state(vic) == "giảm"
    assert m.price_state(tcb) == "tăng"
    assert m.price_state(ssi) == "đứng giá"


def test_price_state_ceiling_floor(m):
    hnx = next(s for s in SAMPLE_STOCKS if s["symbol"] == "HNX-A")  # price == ceiling
    assert m.price_state(hnx) == "chạm trần"
    floor_hit = {"price": 9.9, "basic_price": 11.0, "ceiling_price": 12.1, "floor_price": 9.9}
    assert m.price_state(floor_hit) == "chạm sàn"
    assert m.price_state({}) == "—"


# ─────────────── market_top_stocks ───────────────


@pytest.mark.asyncio
async def test_top_stocks_default_order(m):
    with pytest.MonkeyPatch.context() as mp:
        fake = patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=3))
    assert data["count"] == 3
    assert [s["symbol"] for s in data["stocks"]] == ["VIC", "TCB", "SSI"]
    assert data["stocks"][0]["rank"] == 1
    assert data["stocks"][0]["state"] == "giảm"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_top_stocks_sort_change_percent(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=3, sort="change_percent"))
    assert [s["symbol"] for s in data["stocks"]] == ["HNX-A", "VHM", "TCB"]


@pytest.mark.asyncio
async def test_top_stocks_sort_value(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=2, sort="value"))
    assert [s["symbol"] for s in data["stocks"]] == ["VHM", "TCB"]


@pytest.mark.asyncio
async def test_top_stocks_sort_foreign_net_buy(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=1, sort="foreign_net_buy"))
    assert data["stocks"][0]["symbol"] == "TCB"  # 1.2M - 300k = +900k ngoại mua ròng lớn nhất


@pytest.mark.asyncio
async def test_top_stocks_bad_sort(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=3, sort="xyz"))
    assert "error" in data


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,expect", [(0, 1), (99, 6), ("x", 6), (None, 6), (3, 3)])
async def test_top_stocks_limit_clamped(m, bad, expect):
    """limit 0→1, 99→30 nhưng chỉ có 6 mã sample, 'x'/None→mặc định 10→6."""
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.market_top_stocks(limit=bad))
    assert data["count"] == expect
    assert len(data["stocks"]) == expect


@pytest.mark.asyncio
async def test_top_stocks_stale_warning(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, make_api_payload(SAMPLE_STOCKS, STALE_S))
        data = parse(await m.market_top_stocks(limit=1))
    assert "đóng cửa" in data["meta"].get("note", "")
    assert data["meta"]["last_update"].endswith("(GMT+7)")


@pytest.mark.asyncio
async def test_top_stocks_api_down(m):
    """API sập 3 lần → trả error JSON gọn gàng (không raise ra ngoài)."""
    import httpx

    class DeadClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            raise httpx.ConnectError("boom")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(m.httpx, "AsyncClient", lambda *a, **k: DeadClient())
        # test chạy nhanh: bỏ backoff sleep
        mp.setattr(m.asyncio, "sleep", _noop_sleep())
        data = parse(await m.market_top_stocks())
    assert "error" in data and "3 lần" in data["error"]


def _noop_sleep():
    async def _sleep(_s):
        return None
    return _sleep


# ─────────────── top_gainers / top_losers / most_active ───────────────


@pytest.mark.asyncio
async def test_top_gainers_only_positive(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.top_gainers(limit=10))
    syms = [s["symbol"] for s in data["stocks"]]
    assert "VIC" not in syms and "SHB" not in syms  # 2 mã giảm
    assert syms[0] == "HNX-A"  # +25% cao nhất
    assert all(s["change_percent"] > 0 for s in data["stocks"])


@pytest.mark.asyncio
async def test_top_losers_only_negative(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.top_losers(limit=10))
    syms = [s["symbol"] for s in data["stocks"]]
    assert syms[0] == "SHB"  # -9.09% sâu nhất
    assert all(s["change_percent"] < 0 for s in data["stocks"])


@pytest.mark.asyncio
async def test_most_active_by_value(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.most_active(limit=2))
    assert [s["symbol"] for s in data["stocks"]] == ["VHM", "TCB"]
    assert data["stocks"][0]["matched_value_bn_vnd"] == 1080.2


# ─────────────── stock_quote ───────────────


@pytest.mark.asyncio
async def test_stock_quote_found_case_insensitive(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.stock_quote("vic"))
    q = data["quote"]
    assert q["symbol"] == "VIC"
    assert q["price"] == 220.0
    assert q["reference_price"] == 227.9
    assert q["matched_volume"] == 3716400  # field API ghi nhầm accumylated_vol


@pytest.mark.asyncio
async def test_stock_quote_not_found(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, fresh_payload())
        data = parse(await m.stock_quote("NOPE"))
    assert "error" in data and "market_top_stocks" in data["error"]


# ─────────────── Cache TTL ───────────────


@pytest.mark.asyncio
async def test_cache_dedupes_upstream_calls(m):
    with pytest.MonkeyPatch.context() as mp:
        fake = patch_api(m, mp, fresh_payload())
        a = parse(await m.market_top_stocks(limit=5))
        b = parse(await m.top_gainers())
        c = parse(await m.stock_quote("TCB"))
    assert fake.calls == 1  # 3 tool call, 1 upstream call duy nhất
    assert m._stats["cache_hits"] == 2
    assert a["meta"]["stocks_tracked"] == b["meta"]["stocks_tracked"]


# ─────────────── MCP app contract ───────────────


def test_fastmcp_registered_tools(m):
    import asyncio

    tools = asyncio.run(m.mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {"market_top_stocks", "top_gainers", "top_losers",
                     "most_active", "stock_quote"}


def test_app_routes_health_and_mcp(m):
    paths = {getattr(r, "path", None) for r in m.app.router.routes}
    assert "/health" in paths and "/mcp" in paths


def test_browser_id_format(m):
    assert m.DEVICE_ID.startswith("web") and len(m.DEVICE_ID) > 20
