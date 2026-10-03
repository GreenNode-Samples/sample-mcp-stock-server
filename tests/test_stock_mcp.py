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
    assert names == set(m.TOOL_NAMES)
    assert len(names) == 13


def test_app_routes_health_and_mcp(m):
    paths = {getattr(r, "path", None) for r in m.asgi_app.router.routes}
    assert "/health" in paths and "/mcp" in paths


# ─────────────── Tools mới: doanh nghiệp / lịch sử ───────────────

from conftest import ok, routes  # noqa: E402

COMPANIES = [
    {"symbol": "HPG", "company_name": "Công ty Cổ phần Tập đoàn Hòa Phát", "floor": "HOSE",
     "short_name": "Hòa Phát", "company_name_eng": "Hoa Phat Group", "extra_name": "HPG, hoa phat",
     "description": "Tập đoàn sản xuất thép hàng đầu Việt Nam."},
    {"symbol": "HPX", "company_name": "Công ty Cổ phần Đầu tư Hải Phát", "floor": "HOSE"},
    {"symbol": "FPT", "company_name": "Công ty Cổ phần FPT", "floor": "HOSE",
     "company_name_eng": "FPT Corporation", "description": "x" * 2000},
    {"symbol": "PHP", "company_name": "Cảng Hải Phòng", "floor": "HNX"},
]


@pytest.mark.asyncio
async def test_search_company_accent_insensitive(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(COMPANIES))
        data = parse(await m.search_company("hoa phat"))
    assert data["results"][0]["symbol"] == "HPG"


@pytest.mark.asyncio
async def test_search_company_symbol_ranked_first(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(COMPANIES))
        data = parse(await m.search_company("hp"))
    # mã bắt đầu bằng 'hp' xếp trước mã chỉ khớp theo tên
    assert [r["symbol"] for r in data["results"]][:2] == ["HPG", "HPX"]


@pytest.mark.asyncio
async def test_search_company_priority_breaks_ties(m):
    """Cùng điểm khớp + cùng sàn: doanh nghiệp priority=1 (large-cap) xếp trước mã nhỏ."""
    companies = [
        {"symbol": "HPA", "company_name": "Công ty Cổ phần Nông nghiệp Quốc tế Hoàng Phát Hòa Phát",
         "floor": "HOSE", "priority": 0},
        {"symbol": "HPG", "company_name": "Công ty Cổ phần Tập đoàn Hòa Phát",
         "floor": "HOSE", "priority": 1},
        {"symbol": "HPX", "company_name": "Công ty Cổ phần Hòa Phát Xanh", "floor": "UPCOM", "priority": 0},
    ]
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(companies))
        data = parse(await m.search_company("hoa phat"))
    assert [r["symbol"] for r in data["results"]] == ["HPG", "HPA", "HPX"]
    # điểm khớp vẫn ưu tiên hơn priority: mã khớp chính xác luôn đứng đầu
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(companies))
        data = parse(await m.search_company("hpa"))
    assert data["results"][0]["symbol"] == "HPA"


@pytest.mark.asyncio
async def test_search_company_short_query(m):
    data = parse(await m.search_company("h"))
    assert "error" in data


@pytest.mark.asyncio
async def test_company_profile_truncates_description(m):
    with pytest.MonkeyPatch.context() as mp:
        fake = patch_api(m, mp, ok(COMPANIES))
        data = parse(await m.company_profile("fpt"))
        again = parse(await m.company_profile("HPG"))
    assert data["exchange"] == "HOSE" and data["description"].endswith("…")
    assert again["company_name_eng"] == "Hoa Phat Group"
    assert fake.calls == 1  # danh bạ công ty được cache


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "F", "FPT;DROP", "../x", None])
async def test_symbol_validation(m, bad):
    for tool in (m.company_profile, m.price_history, m.foreign_trading, m.valuation,
                 m.dividend_history, m.business_plan, m.company_announcements):
        data = parse(await tool(bad))
        assert "không hợp lệ" in data["error"]


HISTORY = [  # API trả không theo thứ tự → server phải sort mới nhất trước
    {"trading_date": 1790755803, "match_price": 63.0, "basic_price": 62.0,
     "accumulated_vol": 2_000_000, "accumulated_val": 126.0},
    {"trading_date": 1790928601, "match_price": 62.1, "basic_price": 62.7,
     "accumulated_vol": 3_199_600, "accumulated_val": 200.3},
    {"trading_date": 1790842201, "match_price": 62.7, "basic_price": 63.0,
     "accumulated_vol": 1_500_000, "accumulated_val": 94.0},
]


@pytest.mark.asyncio
async def test_price_history_sorted_and_summary(m):
    with pytest.MonkeyPatch.context() as mp:
        fake = patch_api(m, mp, ok(HISTORY))
        data = parse(await m.price_history("fpt", days=3))
    s = data["sessions"]
    assert [r["close"] for r in s] == [62.1, 62.7, 63.0]
    assert s[0]["change"] == -0.6 and s[0]["change_percent"] == -0.96
    assert data["summary"]["high"] == 63.0 and data["summary"]["low"] == 62.1
    # kỳ: từ tham chiếu phiên cũ nhất (62.0) tới đóng cửa mới nhất (62.1)
    assert data["summary"]["period_change_percent"] == 0.16
    assert fake.urls[0][1]["symbol"] == "FPT"


@pytest.mark.asyncio
async def test_price_history_days_clamped(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(HISTORY))
        data = parse(await m.price_history("FPT", days=1))
    assert data["summary"]["sessions"] == 1


@pytest.mark.asyncio
async def test_foreign_trading_net(m):
    rows = [{"trading_date": 1790928601, "match_price": 62.1, "buy_foreign_qtty": 474146,
             "sell_foreign_qtty": 522297, "buy_foreign_val": 29.6825, "sell_foreign_val": 32.69685},
            {"trading_date": 1790842201, "match_price": 62.7, "buy_foreign_qtty": 900000,
             "sell_foreign_qtty": 100000, "buy_foreign_val": 56.4, "sell_foreign_val": 6.3}]
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(rows))
        data = parse(await m.foreign_trading("FPT"))
    assert data["sessions"][0]["net_value_bn_vnd"] == -3.014
    assert data["summary"]["trend"] == "mua ròng"


@pytest.mark.asyncio
async def test_valuation_metrics(m):
    payload = {"pe": {"value": 11.6611, "message": "Rẻ hơn TB ngành", "group_value": 11.98},
               "roe": {"value": 26.4747, "message": "Giảm 2 quý liên tiếp"},
               "roa": {"value": None}, "group_name": "Công nghệ Thông tin"}
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(payload))
        data = parse(await m.valuation("FPT"))
    assert data["industry"] == "Công nghệ Thông tin"
    assert data["metrics"]["pe"] == {"label": "P/E", "value": 11.66, "industry_avg": 11.98,
                                     "note": "Rẻ hơn TB ngành"}
    assert "roa" not in data["metrics"]


@pytest.mark.asyncio
async def test_valuation_empty(m):
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok({}))
        data = parse(await m.valuation("XYZQ"))
    assert "error" in data


@pytest.mark.asyncio
async def test_dividend_history_types(m):
    rows = [{"type": 2, "end_date": "2023-07-05", "ratio": 0.15},
            {"type": 1, "end_date": "2026-05-28", "ratio": 0.1},
            {"type": 3, "end_date": "2026-09-22", "ratio": 0.1}]
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(rows))
        data = parse(await m.dividend_history("FPT"))
    d = data["dividends"]
    assert [x["date"] for x in d] == ["2026-09-22", "2026-05-28", "2023-07-05"]
    assert d[1]["type"] == "tiền mặt" and d[1]["cash_vnd_per_share"] == 1000
    assert d[2]["type"] == "cổ phiếu" and d[2]["ratio_percent"] == 15.0


@pytest.mark.asyncio
async def test_business_plan(m):
    payload = {"year": 2026, "quarter": 2, "plan": [
        {"expect": 58580.0, "current": 26268.5, "percent": 44.84, "label": "Doanh thu"}]}
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(payload))
        data = parse(await m.business_plan("FPT"))
    assert data["plan"][0] == {"item": "Doanh thu", "target": 58580.0,
                               "actual": 26268.5, "completed_percent": 44.84}


@pytest.mark.asyncio
async def test_company_announcements_limit(m):
    rows = [{"title": f"Tin {i}", "published_date": 1790580541, "link": [f"https://x/{i}.pdf"]}
            for i in range(10)]
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, ok(rows))
        data = parse(await m.company_announcements("FPT", limit=3))
    assert data["count"] == 3
    assert data["announcements"][0] == {"date": "2026-09-28", "title": "Tin 0", "url": "https://x/0.pdf"}


@pytest.mark.asyncio
async def test_routes_fake_dispatch(m):
    """Nhiều endpoint trong 1 test: mỗi tool gọi đúng path của nó."""
    with pytest.MonkeyPatch.context() as mp:
        patch_api(m, mp, routes(**{m.PLAN_PATH: ok({"year": 2026, "quarter": 1, "plan": [
            {"label": "LNST", "expect": 10, "current": 5, "percent": 50}]}),
            m.DIVIDEND_PATH: ok([{"type": 1, "end_date": "2026-01-01", "ratio": 0.2}])}))
        plan = parse(await m.business_plan("VNM"))
        div = parse(await m.dividend_history("VNM"))
    assert plan["plan"][0]["completed_percent"] == 50
    assert div["dividends"][0]["cash_vnd_per_share"] == 2000


# ─────────────── Auth fail-closed (MCP_API_KEYS) ───────────────
# Gộp 1 test duy nhất (1 lifespan): session manager MCP không restart được
# trong cùng process → nhiều TestClient lifespan sẽ xung đột.

def test_mcp_auth_fail_closed(m, monkeypatch):
    from starlette.testclient import TestClient

    ACCEPT = {"Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    KEY_A, KEY_B = "a" * 32, "b" * 32

    with TestClient(m.app) as c:  # context manager → chạy lifespan đúng 1 lần
        # ① không cấu hình key → 503, KHÔNG tự mở
        monkeypatch.setattr(m, "API_KEYS", [])
        monkeypatch.setattr(m, "ALLOW_ANONYMOUS", False)
        r = c.post("/mcp", json=body, headers=ACCEPT)
        assert r.status_code == 503
        assert c.get("/health").json()["mcp_auth"].startswith("locked")

        # ② ALLOW_ANONYMOUS (local dev) → mở
        monkeypatch.setattr(m, "ALLOW_ANONYMOUS", True)
        r = c.post("/mcp", json=body, headers=ACCEPT)
        assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 13

        # ③ có key → key luôn bắt buộc, kể cả khi ALLOW_ANONYMOUS
        monkeypatch.setattr(m, "API_KEYS", [KEY_A, KEY_B])
        r = c.post("/mcp", json=body, headers=ACCEPT)
        assert r.status_code == 401 and "www-authenticate" in r.headers
        r = c.post("/mcp", json=body, headers={**ACCEPT, "X-Api-Key": "sai"})
        assert r.status_code == 401

        # ④ 3 kiểu header hợp lệ + xoay vòng 2 key
        for hdr in ({"X-Api-Key": KEY_A}, {"Authorization": f"Bearer {KEY_B}"},
                    {"X-Stock-Api-Key": KEY_A}):
            r = c.post("/mcp", json=body, headers={**ACCEPT, **hdr})
            assert r.status_code == 200, hdr

        # ⑤ /health và / không cần key
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/health").json()["mcp_auth"] == "api-key (2 key)"


def test_load_api_keys_env(m, monkeypatch):
    monkeypatch.setenv("MCP_API_KEYS", " k1 , ,k2 ")
    assert m._load_api_keys() == ["k1", "k2"]
    monkeypatch.delenv("MCP_API_KEYS")
    monkeypatch.setenv("STOCK_API_KEY", "legacy")
    assert m._load_api_keys() == ["legacy"]
