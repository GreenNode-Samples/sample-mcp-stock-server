"""Upstream layer: cache, in-flight de-duplication, retry policy, time budget, error text."""

import asyncio
import time

import httpx
import pytest
from conftest import FRESH_S, SAMPLE_STOCKS, make_api_payload, ok, patch_api
from mcp.server.fastmcp.exceptions import ToolError

TOP = make_api_payload(SAMPLE_STOCKS, FRESH_S)


def sequence(*steps):
    """Responder that answers with the given steps in order: an int is a status code, a dict a 200 body,
    an exception instance is raised (transport error)."""
    steps = list(steps)

    def respond(request):
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return httpx.Response(step, json={"status": step})
        return httpx.Response(200, json=step)

    return respond


# ─────────────── Cache ───────────────


@pytest.mark.parametrize("body", [
    ok(None), ok([]), ok({}), ok("oops"), ok({"stocks": []}), ok({"stocks": "x"}),
    {"status": 500, "data": {"stocks": [1]}}, {"data": {}}, [], None, "maintenance",
])
async def test_bad_payloads_are_never_cached(m, monkeypatch, body):
    fake = patch_api(m, monkeypatch, body)
    for _ in range(2):
        with pytest.raises(ToolError):
            await m.market_top_stocks()
    assert fake.calls == 2  # nothing was cached, so every call went upstream again
    assert m._cache == {}


async def test_empty_results_are_not_cached(m, monkeypatch):
    fake = patch_api(m, monkeypatch, ok([]))
    for _ in range(2):
        with pytest.raises(ToolError, match="No dividend history"):
            await m.dividend_history("FPT")
    assert fake.calls == 2 and m._cache == {}


async def test_null_data_is_an_error_everywhere(m, monkeypatch):
    patch_api(m, monkeypatch, ok(None))
    for tool in (m.valuation, m.price_history, m.business_plan, m.company_announcements):
        with pytest.raises(ToolError, match="unexpected data format"):
            await tool("FPT")
    with pytest.raises(ToolError, match="unexpected data format"):
        await m.search_company("fpt")
    assert m._cache == {}


async def test_errors_are_not_cached_and_recovery_works(m, monkeypatch):
    fake = patch_api(m, monkeypatch, sequence(500, 500, 500, TOP))
    with pytest.raises(ToolError, match="unavailable"):
        await m.market_top_stocks()
    assert (await m.market_top_stocks())["count"] == len(SAMPLE_STOCKS)
    assert fake.calls == 4


async def test_valid_payload_is_cached_with_ttl(m, monkeypatch):
    fake = patch_api(m, monkeypatch, TOP)
    await m.market_top_stocks()
    await m.market_top_stocks()
    assert fake.calls == 1
    key = "top-stocks"
    expires, _ = m._cache[key]
    m._cache[key] = (time.monotonic() - 1, m._cache[key][1])  # force expiry
    await m.market_top_stocks()
    assert fake.calls == 2 and m._cache[key][0] > expires


def test_cache_put_purges_expired_entries(m):
    m._cache["old"] = (time.monotonic() - 1, {"x": 1})
    m._cache_put("new", 60, {"y": 2})
    assert list(m._cache) == ["new"]


def test_cache_is_bounded(m, monkeypatch):
    monkeypatch.setattr(m, "MAX_CACHE_ENTRIES", 3)
    for i in range(10):
        m._cache_put(f"k{i}", 60, {"i": i})
    assert list(m._cache) == ["k7", "k8", "k9"]  # the oldest entries are evicted first
    m._cache_put("k7", 60, {"i": 70})              # re-putting a key makes it the newest
    assert list(m._cache) == ["k8", "k9", "k7"]


async def test_cache_stays_bounded_across_many_symbols(m, monkeypatch):
    monkeypatch.setattr(m, "MAX_CACHE_ENTRIES", 5)
    patch_api(m, monkeypatch, ok([{"trading_date": 1790928601, "match_price": 1.0}]))
    for i in range(20):
        await m.price_history(f"AB{i}")
    assert len(m._cache) == 5


# ─────────────── In-flight de-duplication ───────────────


async def test_concurrent_cold_calls_share_one_upstream_request(m, monkeypatch):
    async def slow(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=TOP)

    fake = patch_api(m, monkeypatch, slow)
    results = await asyncio.gather(*[m.top_gainers() for _ in range(20)])
    assert fake.calls == 1
    assert all(r["count"] == results[0]["count"] for r in results)
    assert m._inflight == {}


async def test_concurrent_calls_share_the_same_error(m, monkeypatch):
    async def slow_fail(request):
        await asyncio.sleep(0.05)
        return httpx.Response(404)

    fake = patch_api(m, monkeypatch, slow_fail)
    results = await asyncio.gather(*[m.top_gainers() for _ in range(5)], return_exceptions=True)
    assert fake.calls == 1
    assert all(isinstance(r, ToolError) for r in results)
    assert m._inflight == {}


async def test_cancelled_caller_does_not_cancel_the_shared_request(m, monkeypatch):
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=TOP)

    fake = patch_api(m, monkeypatch, slow)
    first = asyncio.create_task(m.top_gainers())
    second = asyncio.create_task(m.top_gainers())
    await asyncio.sleep(0.02)
    first.cancel()
    assert (await second)["count"] > 0
    assert fake.calls == 1


# ─────────────── Retry policy ───────────────


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_retries_5xx_and_429(m, monkeypatch, status):
    fake = patch_api(m, monkeypatch, sequence(status, status, TOP))
    assert (await m.market_top_stocks())["count"] == len(SAMPLE_STOCKS)
    assert fake.calls == 3


async def test_retries_transport_errors(m, monkeypatch):
    fake = patch_api(m, monkeypatch, sequence(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), TOP))
    assert (await m.market_top_stocks())["count"] == len(SAMPLE_STOCKS)
    assert fake.calls == 3


async def test_gives_up_after_three_attempts(m, monkeypatch):
    fake = patch_api(m, monkeypatch, sequence(httpx.ConnectError("boom"), 503, 502))
    with pytest.raises(ToolError, match=r"unavailable \(HTTP 502\) after 3 attempts"):
        await m.market_top_stocks()
    assert fake.calls == 3


@pytest.mark.parametrize("status,hint", [(400, "bad request"), (403, "access denied"), (404, "not found"),
                                         (410, "HTTP 410")])
async def test_4xx_is_not_retried(m, monkeypatch, status, hint):
    fake = patch_api(m, monkeypatch, sequence(status))
    with pytest.raises(ToolError, match=hint):
        await m.market_top_stocks()
    assert fake.calls == 1


async def test_non_json_response_is_not_retried(m, monkeypatch):
    fake = patch_api(m, monkeypatch, lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ToolError, match="unexpected data format"):
        await m.market_top_stocks()
    assert fake.calls == 1


@pytest.mark.parametrize("responder", [
    sequence(500, 500, 500),
    sequence(httpx.ConnectError("https://api.example/secret-path"), httpx.ConnectError("x"), httpx.ConnectError("y")),
    sequence(404),
    lambda request: httpx.Response(200, text="not json"),
])
async def test_error_text_does_not_leak_url_or_device_id(m, monkeypatch, responder):
    patch_api(m, monkeypatch, responder)
    with pytest.raises(ToolError) as exc:
        await m.market_top_stocks()
    message = str(exc.value)
    assert m.DEVICE_ID not in message
    assert "://" not in message and m.API_BASE not in message and "secret-path" not in message


async def test_logs_do_not_contain_the_device_id(m, monkeypatch, caplog):
    patch_api(m, monkeypatch, sequence(500, 500, 500))
    with pytest.raises(ToolError), caplog.at_level("DEBUG", logger="vn-stock-mcp"):
        await m.price_history("FPT")
    assert m.DEVICE_ID not in caplog.text


# ─────────────── Total time budget ───────────────


async def test_total_timeout_covers_the_whole_call(m, monkeypatch):
    async def hang(request):
        await asyncio.sleep(30)

    monkeypatch.setattr(m, "HTTP_TIMEOUT", 0.2)
    monkeypatch.setattr(m, "RETRY_DELAYS", (0.5, 1.5))  # real back-offs must not extend the budget
    fake = patch_api(m, monkeypatch, hang)
    started = time.monotonic()
    with pytest.raises(ToolError, match="did not answer within 0.2s"):
        await m.market_top_stocks()
    assert time.monotonic() - started < 1.0
    assert fake.calls == 1
    assert m._inflight == {} and m._cache == {}


async def test_budget_is_shared_by_retries(m, monkeypatch):
    monkeypatch.setattr(m, "HTTP_TIMEOUT", 0.3)
    monkeypatch.setattr(m, "RETRY_DELAYS", (0.2, 5))  # the second back-off would overrun the budget
    fake = patch_api(m, monkeypatch, lambda request: httpx.Response(503))
    started = time.monotonic()
    with pytest.raises(ToolError, match="did not answer within 0.3s"):
        await m.market_top_stocks()
    assert time.monotonic() - started < 1.0
    assert fake.calls == 2
