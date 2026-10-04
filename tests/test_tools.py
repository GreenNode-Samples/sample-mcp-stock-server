"""Tool behaviour: ranking, validation, output shapes, error handling (hermetic, upstream faked)."""

from datetime import datetime

import pytest
from conftest import (
    FRESH_S,
    SAMPLE_STOCKS,
    STALE_S,
    make_api_payload,
    ok,
    patch_api,
    routes,
)
from mcp.server.fastmcp.exceptions import ToolError


def fresh_payload(stocks=None):
    return make_api_payload(stocks if stocks is not None else SAMPLE_STOCKS, FRESH_S)


def by_symbol(symbol):
    return next(s for s in SAMPLE_STOCKS if s["symbol"] == symbol)


# ─────────────── price_state ───────────────


def test_price_state_basic(m):
    assert m.price_state(by_symbol("VIC")) == "down"        # 220 < basic 227.9
    assert m.price_state(by_symbol("TCB")) == "up"          # 35.5 > basic 34.2
    assert m.price_state(by_symbol("SSI")) == "unchanged"   # equals basic


def test_price_state_ceiling_floor(m):
    assert m.price_state(by_symbol("HNXA")) == "at_ceiling"  # price == ceiling
    floor_hit = {"price": 9.9, "basic_price": 11.0, "ceiling_price": 12.1, "floor_price": 9.9}
    assert m.price_state(floor_hit) == "at_floor"
    assert m.price_state({}) == "unknown"
    assert m.price_state({"price": "abc", "basic_price": [1]}) == "unknown"


# ─────────────── market tools ───────────────


async def test_top_stocks_default_order_and_envelope(m, monkeypatch):
    fake = patch_api(m, monkeypatch, fresh_payload())
    data = await m.market_top_stocks(limit=3)
    assert data["count"] == 3
    assert [s["symbol"] for s in data["stocks"]] == ["VIC", "TCB", "SSI"]
    assert data["stocks"][0]["rank"] == 1
    assert data["stocks"][0]["state"] == "down"
    assert data["source"] == "24hmoney.vn"
    assert data["units"] == m.UNITS_MARKET
    assert data["stocks_tracked"] == len(SAMPLE_STOCKS)
    assert fake.calls == 1


async def test_top_stocks_sort_change_percent(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.market_top_stocks(limit=3, sort="change_percent")
    assert [s["symbol"] for s in data["stocks"]] == ["HNXA", "VHM", "TCB"]


async def test_top_stocks_sort_value(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.market_top_stocks(limit=2, sort="value")
    assert [s["symbol"] for s in data["stocks"]] == ["VHM", "TCB"]


async def test_top_stocks_sort_foreign_net_buy(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.market_top_stocks(limit=1, sort="foreign_net_buy")
    assert data["stocks"][0]["symbol"] == "TCB"  # 1.2M - 300k = +900k, the largest net foreign buy


async def test_top_stocks_bad_sort(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    with pytest.raises(ToolError, match="Invalid sort 'xyz'"):
        await m.market_top_stocks(limit=3, sort="xyz")


@pytest.mark.parametrize("limit,expected", [(0, 1), (-5, 1), (99, 6), (3, 3)])
async def test_top_stocks_limit_clamped(m, monkeypatch, limit, expected):
    """Out-of-range limits are clamped to 1..30; the sample only has 6 stocks."""
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.market_top_stocks(limit=limit)
    assert data["count"] == expected == len(data["stocks"])


async def test_top_stocks_default_limits_match_docs(m, monkeypatch):
    """market_top_stocks documents a default of 20, the other rankings 10."""
    stocks = [{"symbol": f"S{i:02d}", "price": 10.0, "basic_price": 9.0, "change_percent": float(i + 1),
               "accumulated_val": float(i)} for i in range(40)]
    patch_api(m, monkeypatch, fresh_payload(stocks))
    assert (await m.market_top_stocks())["count"] == 20
    assert (await m.top_gainers())["count"] == 10
    assert (await m.most_active())["count"] == 10


async def test_top_stocks_stale_note_and_iso_timestamp(m, monkeypatch):
    patch_api(m, monkeypatch, make_api_payload(SAMPLE_STOCKS, STALE_S))
    data = await m.market_top_stocks(limit=1)
    assert "market may be closed" in data["note"]
    assert data["last_update"].endswith("+07:00")
    assert datetime.fromisoformat(data["last_update"]).utcoffset().total_seconds() == 7 * 3600


async def test_top_gainers_only_positive(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.top_gainers(limit=10)
    symbols = [s["symbol"] for s in data["stocks"]]
    assert "VIC" not in symbols and "SHB" not in symbols  # the two losers
    assert symbols[0] == "HNXA"                            # +25%, the largest gain
    assert all(s["change_percent"] > 0 for s in data["stocks"])


async def test_top_losers_only_negative(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.top_losers(limit=10)
    assert data["stocks"][0]["symbol"] == "SHB"  # -9.09%, the deepest loss
    assert all(s["change_percent"] < 0 for s in data["stocks"])


async def test_most_active_by_value(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    data = await m.most_active(limit=2)
    assert [s["symbol"] for s in data["stocks"]] == ["VHM", "TCB"]
    assert data["stocks"][0]["matched_value_bn_vnd"] == 1080.2


# ─────────────── stock_quote ───────────────


@pytest.mark.parametrize("raw", ["vic", "VIC", "  vic  "])
async def test_stock_quote_normalises_symbol(m, monkeypatch, raw):
    patch_api(m, monkeypatch, fresh_payload())
    q = (await m.stock_quote(raw))["quote"]
    assert q["symbol"] == "VIC"
    assert q["price"] == 220.0
    assert q["reference_price"] == 227.9
    assert q["matched_volume"] == 3716400  # the upstream field is misspelled "accumylated_vol"


@pytest.mark.parametrize("bad", ["", "V", "VIC;DROP", "../x"])
async def test_stock_quote_rejects_invalid_symbol(m, monkeypatch, bad):
    fake = patch_api(m, monkeypatch, fresh_payload())
    with pytest.raises(ToolError, match="Invalid stock symbol"):
        await m.stock_quote(bad)
    assert fake.calls == 0  # rejected before any upstream call


async def test_stock_quote_not_found(m, monkeypatch):
    patch_api(m, monkeypatch, fresh_payload())
    with pytest.raises(ToolError, match="market_top_stocks"):
        await m.stock_quote("NOPE")


async def test_cache_shares_top_stocks_between_tools(m, monkeypatch):
    fake = patch_api(m, monkeypatch, fresh_payload())
    a = await m.market_top_stocks(limit=5)
    b = await m.top_gainers()
    c = await m.stock_quote("TCB")
    assert fake.calls == 1  # three tool calls, one upstream call
    assert a["stocks_tracked"] == b["stocks_tracked"] == c["stocks_tracked"]


# ─────────────── companies ───────────────

COMPANIES = [
    {"symbol": "HPG", "company_name": "Công ty Cổ phần Tập đoàn Hòa Phát", "floor": "HOSE",
     "short_name": "Hòa Phát", "company_name_eng": "Hoa Phat Group", "extra_name": "HPG, hoa phat",
     "description": "Tập đoàn sản xuất thép hàng đầu Việt Nam."},
    {"symbol": "HPX", "company_name": "Công ty Cổ phần Đầu tư Hải Phát", "floor": "HOSE"},
    {"symbol": "FPT", "company_name": "Công ty Cổ phần FPT", "floor": "HOSE",
     "company_name_eng": "FPT Corporation", "description": "x" * 2000},
    {"symbol": "PHP", "company_name": "Cảng Hải Phòng", "floor": "HNX"},
]


async def test_search_company_ignores_diacritics_and_case(m, monkeypatch):
    patch_api(m, monkeypatch, ok(COMPANIES))
    data = await m.search_company("HOA phat")
    assert data["results"][0]["symbol"] == "HPG"
    assert data["count"] == len(data["results"])
    assert data["source"] == "24hmoney.vn"


async def test_search_company_symbol_ranked_first(m, monkeypatch):
    patch_api(m, monkeypatch, ok(COMPANIES))
    data = await m.search_company("hp")
    # tickers starting with 'hp' come before companies that only match by name
    assert [r["symbol"] for r in data["results"]][:2] == ["HPG", "HPX"]


async def test_search_company_priority_breaks_ties(m, monkeypatch):
    """Same match score and exchange: a priority=1 (large-cap) company ranks first."""
    companies = [
        {"symbol": "HPA", "company_name": "Công ty Cổ phần Nông nghiệp Quốc tế Hoàng Phát Hòa Phát",
         "floor": "HOSE", "priority": 0},
        {"symbol": "HPG", "company_name": "Công ty Cổ phần Tập đoàn Hòa Phát", "floor": "HOSE", "priority": 1},
        {"symbol": "HPX", "company_name": "Công ty Cổ phần Hòa Phát Xanh", "floor": "UPCOM", "priority": 0},
    ]
    patch_api(m, monkeypatch, ok(companies))
    assert [r["symbol"] for r in (await m.search_company("hoa phat"))["results"]] == ["HPG", "HPA", "HPX"]
    # the match score still beats priority: an exact ticker match always comes first
    assert (await m.search_company("hpa"))["results"][0]["symbol"] == "HPA"


async def test_search_company_limit(m, monkeypatch):
    patch_api(m, monkeypatch, ok(COMPANIES))
    data = await m.search_company("hai", limit=1)
    assert data["count"] == 1


async def test_search_company_short_query(m):
    with pytest.raises(ToolError, match="at least 2 characters"):
        await m.search_company("h")


async def test_company_profile_truncates_description(m, monkeypatch):
    fake = patch_api(m, monkeypatch, ok(COMPANIES))
    data = await m.company_profile("fpt")
    again = await m.company_profile("HPG")
    assert data["exchange"] == "HOSE" and data["description"].endswith("…")
    assert again["company_name_eng"] == "Hoa Phat Group"
    assert fake.calls == 1  # the company directory is cached


async def test_company_profile_unknown_symbol(m, monkeypatch):
    patch_api(m, monkeypatch, ok(COMPANIES))
    with pytest.raises(ToolError, match="No company found"):
        await m.company_profile("ZZZ")


@pytest.mark.parametrize("bad", ["", "F", "FPT;DROP", "../x"])
async def test_symbol_validation(m, bad):
    for tool in (m.company_profile, m.price_history, m.foreign_trading, m.valuation,
                 m.dividend_history, m.business_plan, m.company_announcements):
        with pytest.raises(ToolError, match="Invalid stock symbol"):
            await tool(bad)


# ─────────────── price_history / foreign_trading ───────────────

HISTORY = [  # the API does not sort: the server must return newest first
    {"trading_date": 1790755803, "match_price": 63.0, "basic_price": 62.0,
     "accumulated_vol": 2_000_000, "accumulated_val": 126.0},
    {"trading_date": 1790928601, "match_price": 62.1, "basic_price": 62.7,
     "accumulated_vol": 3_199_600, "accumulated_val": 200.3},
    {"trading_date": 1790842201, "match_price": 62.7, "basic_price": 63.0,
     "accumulated_vol": 1_500_000, "accumulated_val": 94.0},
]


async def test_price_history_sorted_and_summary(m, monkeypatch):
    fake = patch_api(m, monkeypatch, ok(HISTORY))
    data = await m.price_history("fpt", days=3)
    s = data["sessions"]
    assert [r["close"] for r in s] == [62.1, 62.7, 63.0]
    assert s[0]["change"] == -0.6 and s[0]["change_percent"] == -0.96
    assert data["count"] == 3
    assert data["summary"]["highest_close"] == 63.0 and data["summary"]["lowest_close"] == 62.1
    # period: from the reference price of the oldest session (62.0) to the latest close (62.1)
    assert data["summary"]["period_change_percent"] == 0.16
    assert data["summary"]["from"] < data["summary"]["to"]
    assert data["units"] == m.UNITS_MARKET and data["source"] == "24hmoney.vn"
    assert fake.requests[0].url.params["symbol"] == "FPT"


async def test_price_history_days_clamped(m, monkeypatch):
    patch_api(m, monkeypatch, ok(HISTORY))
    assert (await m.price_history("FPT", days=1))["count"] == 1
    assert (await m.price_history("FPT", days=0))["count"] == 1
    assert (await m.price_history("FPT", days=500))["count"] == 3


async def test_price_history_no_data(m, monkeypatch):
    patch_api(m, monkeypatch, ok([]))
    with pytest.raises(ToolError, match="No price history"):
        await m.price_history("FPT")


FOREIGN_ROWS = [
    {"trading_date": 1790928601, "match_price": 62.1, "buy_foreign_qtty": 474146,
     "sell_foreign_qtty": 522297, "buy_foreign_val": 29.6825, "sell_foreign_val": 32.69685},
    {"trading_date": 1790842201, "match_price": 62.7, "buy_foreign_qtty": 900000,
     "sell_foreign_qtty": 100000, "buy_foreign_val": 56.4, "sell_foreign_val": 6.3},
]


async def test_foreign_trading_net(m, monkeypatch):
    patch_api(m, monkeypatch, ok(FOREIGN_ROWS))
    data = await m.foreign_trading("FPT")
    assert data["sessions"][0]["net_value_bn_vnd"] == -3.014
    assert data["summary"]["net_value_bn_vnd"] == 47.086
    assert data["summary"]["trend"] == "net_buy"
    assert data["summary"]["sessions_with_net_value"] == 2
    assert data["count"] == 2


async def test_foreign_trading_missing_values_stay_null(m, monkeypatch):
    """A missing value is null, never 0: it must not leak into the net figures or the trend."""
    rows = [{"trading_date": 1790928601, "match_price": 62.1, "buy_foreign_val": 10.0},   # no sell value
            {"trading_date": 1790842201, "match_price": 62.7, "buy_foreign_val": 5.0, "sell_foreign_val": 2.0}]
    patch_api(m, monkeypatch, ok(rows))
    data = await m.foreign_trading("FPT")
    first, second = data["sessions"]
    assert first["net_value_bn_vnd"] is None and first["sell_value_bn_vnd"] is None
    assert first["buy_volume"] is None and first["sell_volume"] is None
    assert second["net_value_bn_vnd"] == 3.0
    assert data["summary"] == {"sessions_with_net_value": 1, "net_value_bn_vnd": 3.0, "trend": "net_buy"}


async def test_foreign_trading_nothing_known(m, monkeypatch):
    patch_api(m, monkeypatch, ok([{"trading_date": 1790928601, "match_price": 62.1}]))
    summary = (await m.foreign_trading("FPT"))["summary"]
    assert summary == {"sessions_with_net_value": 0, "net_value_bn_vnd": None, "trend": None}


# ─────────────── valuation / dividends / plan / announcements ───────────────


async def test_valuation_metrics(m, monkeypatch):
    payload = {"pe": {"value": 11.6611, "message": "Cheaper than the industry", "group_value": 11.98},
               "roe": {"value": 26.4747, "message": "Down 2 quarters in a row"},
               "roa": {"value": None}, "group_name": "Information Technology"}
    patch_api(m, monkeypatch, ok(payload))
    data = await m.valuation("FPT")
    assert data["industry"] == "Information Technology"
    assert data["metrics"]["pe"] == {"label": "P/E", "value": 11.66, "industry_avg": 11.98,
                                     "note": "Cheaper than the industry"}
    assert "roa" not in data["metrics"]
    assert data["units"]["pe"] == "x" and data["units"]["roe"] == "%" and data["units"]["eps"] == "VND"


async def test_valuation_empty(m, monkeypatch):
    patch_api(m, monkeypatch, ok({}))
    with pytest.raises(ToolError, match="No valuation data"):
        await m.valuation("XYZQ")


async def test_dividend_history_types(m, monkeypatch):
    rows = [{"type": 2, "end_date": "2023-07-05", "ratio": 0.15},
            {"type": 1, "end_date": "2026-05-28", "ratio": 0.1},
            {"type": 3, "end_date": "2026-09-22", "ratio": 0.1},
            {"type": 9, "end_date": "2020-01-01"}]
    patch_api(m, monkeypatch, ok(rows))
    data = await m.dividend_history("FPT")
    d = data["dividends"]
    assert [x["date"] for x in d] == ["2026-09-22", "2026-05-28", "2023-07-05", "2020-01-01"]
    assert d[1]["type"] == "cash" and d[1]["cash_vnd_per_share"] == 1000
    assert d[2]["type"] == "stock" and d[2]["ratio_percent"] == 15.0
    assert d[0]["type"] == "bonus_shares"
    assert d[3]["type"] == "other" and d[3]["ratio_percent"] is None
    assert data["count"] == 4 and data["source"] == "24hmoney.vn"


async def test_business_plan(m, monkeypatch):
    payload = {"year": 2026, "quarter": 2, "plan": [
        {"expect": 58580.0, "current": 26268.5, "percent": 44.84, "label": "Revenue"}]}
    patch_api(m, monkeypatch, ok(payload))
    data = await m.business_plan("FPT")
    assert data["plan"][0] == {"item": "Revenue", "target": 58580.0, "actual": 26268.5, "completed_percent": 44.84}
    assert data["count"] == 1 and data["year"] == 2026 and data["through_quarter"] == 2


async def test_business_plan_without_items(m, monkeypatch):
    patch_api(m, monkeypatch, ok({"year": 2026, "quarter": 1, "plan": []}))
    with pytest.raises(ToolError, match="No business plan"):
        await m.business_plan("FPT")


async def test_company_announcements_limit(m, monkeypatch):
    rows = [{"title": f"News {i}", "published_date": 1790580541, "link": [f"https://x/{i}.pdf"]}
            for i in range(10)]
    patch_api(m, monkeypatch, ok(rows))
    data = await m.company_announcements("FPT", limit=3)
    assert data["count"] == 3
    assert data["announcements"][0] == {"date": "2026-09-28", "title": "News 0", "url": "https://x/0.pdf"}


async def test_each_tool_calls_its_own_endpoint(m, monkeypatch):
    patch_api(m, monkeypatch, routes(**{
        m.PLAN_PATH: ok({"year": 2026, "quarter": 1, "plan": [
            {"label": "Net profit", "expect": 10, "current": 5, "percent": 50}]}),
        m.DIVIDEND_PATH: ok([{"type": 1, "end_date": "2026-01-01", "ratio": 0.2}])}))
    plan = await m.business_plan("VNM")
    div = await m.dividend_history("VNM")
    assert plan["plan"][0]["completed_percent"] == 50
    assert div["dividends"][0]["cash_vnd_per_share"] == 2000


# ─────────────── Upstream data is untrusted: never AttributeError / TypeError ───────────────

WEIRD = [None, True, "abc", "12.5", [], ["x"], {}, {"a": 1}, -1, 0]
ROW_FIELDS = [
    "symbol", "price", "basic_price", "ceiling_price", "floor_price", "change", "change_percent",
    "accumylated_vol", "accumulated_val", "buy_foreign_qtty", "sell_foreign_qtty", "trading_date",
    "match_price", "accumulated_vol", "buy_foreign_val", "sell_foreign_val", "company_name",
    "short_name", "company_name_eng", "extra_name", "floor", "priority", "description", "end_date",
    "type", "ratio", "label", "expect", "current", "percent", "title", "published_date", "link",
]

TOOL_CALLS = [
    ("market_top_stocks", {}), ("market_top_stocks", {"sort": "foreign_net_buy"}), ("top_gainers", {}),
    ("top_losers", {}), ("most_active", {}), ("stock_quote", {"symbol": "VIC"}),
    ("search_company", {"query": "fpt"}), ("company_profile", {"symbol": "FPT"}),
    ("price_history", {"symbol": "FPT"}), ("foreign_trading", {"symbol": "FPT"}),
    ("valuation", {"symbol": "FPT"}), ("dividend_history", {"symbol": "FPT"}),
    ("business_plan", {"symbol": "FPT"}), ("company_announcements", {"symbol": "FPT"}),
]


def weird_bodies(weird):
    """Upstream `data` values with every known field replaced by `weird`, in every container shape."""
    row = dict.fromkeys(ROW_FIELDS, weird)
    row["symbol"] = "FPT" if weird is None else weird
    metrics = {k: {"value": weird, "group_value": weird, "message": weird}
               for k in ("pe", "pb", "roe", "roa", "eps", "net_profit_margin", "ev_per_ebitda")}
    return [
        {"stocks": [row], "last_update": weird},
        [row],
        {"plan": [row], "year": weird, "quarter": weird},
        {**metrics, "group_name": weird},
    ]


@pytest.mark.parametrize("body", [[], None, "maintenance", 12, True, {"x": 1}, [1, "a", None], {"stocks": "x"},
                                  {"stocks": [1, 2]}, {"stocks": []}, {"plan": "x"}, {"plan": [1]}])
@pytest.mark.parametrize("tool,args", TOOL_CALLS)
async def test_unexpected_shapes_give_clean_errors(m, monkeypatch, tool, args, body):
    patch_api(m, monkeypatch, ok(body))
    try:
        result = await getattr(m, tool)(**args)
    except ToolError:
        return
    assert isinstance(result, dict)  # a tolerant tool may legitimately return an (empty) result


@pytest.mark.parametrize("weird", WEIRD, ids=repr)
@pytest.mark.parametrize("tool,args", TOOL_CALLS)
async def test_type_drift_never_raises_raw_exceptions(m, monkeypatch, tool, args, weird):
    for body in weird_bodies(weird):
        m._cache.clear()
        patch_api(m, monkeypatch, ok(body))
        try:
            result = await getattr(m, tool)(**args)
        except ToolError:
            continue
        assert isinstance(result, dict)


async def test_string_numbers_are_coerced(m, monkeypatch):
    patch_api(m, monkeypatch, ok([{"trading_date": "1790928601", "match_price": "62.1", "basic_price": "62.7"}]))
    session = (await m.price_history("FPT"))["sessions"][0]
    assert session["close"] == 62.1 and session["change"] == -0.6
