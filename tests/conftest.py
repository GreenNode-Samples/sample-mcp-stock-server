"""Pytest fixtures: import the server from src/mcp_server (no install needed) and fake 24hMoney.

The upstream API is faked with `httpx.MockTransport`, so the tests never touch the network.
"""

import inspect
import sys
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "mcp_server"))

import main as stock_main  # noqa: E402

VALID_KEY = "k" * 32


@pytest.fixture(scope="session")
def m():
    return stock_main


@pytest.fixture(scope="session")
def mcp_client(m):
    """One TestClient for the whole run: the MCP session manager can only be started once per process."""
    with TestClient(m.app) as client:
        yield client


@pytest.fixture(autouse=True)
def isolate_state(m, monkeypatch):
    """Cache, in-flight requests and the HTTP client are module state: reset them for every test."""
    m._cache.clear()
    m._inflight.clear()
    monkeypatch.setattr(m, "RETRY_DELAYS", (0, 0))  # same number of attempts, no waiting

    def no_network(request):
        raise AssertionError(f"test made an unfaked upstream call: {request.url.path}")

    # Safety net: a test that forgets to install a fake can never reach the real API.
    monkeypatch.setattr(m, "_client", httpx.AsyncClient(transport=httpx.MockTransport(no_network)))
    yield
    m._cache.clear()
    m._inflight.clear()


class Upstream:
    """Fake 24hMoney. Records every request and answers from `responder`:

    - a dict            -> 200 with that JSON body
    - routes(**by_path) -> 200 with the body of the path the request ends with, 404 otherwise
    - a callable        -> called with the request; returns an httpx.Response (may be async)
    """

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[httpx.Request] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self.responder
        if callable(responder):
            out = responder(request)
            return await out if inspect.isawaitable(out) else out
        if isinstance(responder, dict) and "__routes__" in responder:
            for path, body in responder["__routes__"].items():
                if request.url.path.endswith(path):
                    return httpx.Response(200, json=body)
            return httpx.Response(404, json={"status": 404})
        return httpx.Response(200, json=responder)


def patch_api(m, monkeypatch, responder) -> Upstream:
    """Point the server's HTTP client at a fake upstream."""
    upstream = Upstream(responder)
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), headers=m.BROWSER_HEADERS)
    monkeypatch.setattr(m, "_client", client)
    return upstream


def ok(data) -> dict:
    """A successful 24hMoney response envelope."""
    return {"message": "success", "status": 200, "data": data}


def routes(**by_path) -> dict:
    """Responses for several endpoints: routes(**{"/v1/x": ok([...])})."""
    return {"__routes__": by_path}


def make_api_payload(stocks: list[dict], last_update: int) -> dict:
    return ok({"stocks": stocks, "last_update": last_update})


SAMPLE_STOCKS = [
    # symbol, price, basic, ceiling, floor, change, chg%, vol, val, fbuy, fsell
    {"symbol": "VIC", "price": 220.0, "basic_price": 227.9, "ceiling_price": 243.8,
     "floor_price": 212.0, "change": -7.9, "change_percent": -3.47,
     "accumylated_vol": 3716400, "accumulated_val": 824.01,
     "buy_foreign_qtty": 256670, "sell_foreign_qtty": 382800},
    {"symbol": "TCB", "price": 35.5, "basic_price": 34.2, "ceiling_price": 37.6,
     "floor_price": 30.8, "change": 1.3, "change_percent": 3.80,
     "accumylated_vol": 25100200, "accumulated_val": 877.15,
     "buy_foreign_qtty": 1200000, "sell_foreign_qtty": 300000},
    {"symbol": "SSI", "price": 40.0, "basic_price": 40.0, "ceiling_price": 44.0,
     "floor_price": 36.0, "change": 0.0, "change_percent": 0.0,
     "accumylated_vol": 15000000, "accumulated_val": 600.0,
     "buy_foreign_qtty": 500000, "sell_foreign_qtty": 500000},
    {"symbol": "VHM", "price": 90.5, "basic_price": 85.0, "ceiling_price": 93.5,
     "floor_price": 76.5, "change": 5.5, "change_percent": 6.47,
     "accumylated_vol": 12000000, "accumulated_val": 1080.2,
     "buy_foreign_qtty": 900000, "sell_foreign_qtty": 100000},
    {"symbol": "SHB", "price": 10.0, "basic_price": 11.0, "ceiling_price": 12.1,
     "floor_price": 9.9, "change": -1.0, "change_percent": -9.09,
     "accumylated_vol": 40000000, "accumulated_val": 400.0,
     "buy_foreign_qtty": 0, "sell_foreign_qtty": 2000000},
    {"symbol": "HNXA", "price": 20.0, "basic_price": 16.0, "ceiling_price": 20.0,
     "floor_price": 14.4, "change": 4.0, "change_percent": 25.0,
     "accumylated_vol": 1000, "accumulated_val": 0.02,
     "buy_foreign_qtty": 0, "sell_foreign_qtty": 0},
]

FRESH_S = int(time.time())          # epoch seconds, "just updated"
STALE_S = int(time.time() - 3600)   # one hour ago: the market-closed note must appear
