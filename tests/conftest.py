"""Pytest fixtures — import module MCP server từ src/mcp_server (không cần install)."""

import sys
import time as _time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mcp_server"
sys.path.insert(0, str(SRC))

import main as stock_main  # noqa: E402


@pytest.fixture()
def m():
    return stock_main


@pytest.fixture(autouse=True)
def clean_cache(m, monkeypatch):
    """Cache là state toàn cục — xoá + reset đếm sau mỗi test."""
    m._cache.clear()
    m._stats["upstream_calls"] = 0
    m._stats["cache_hits"] = 0
    # test không đợi backoff
    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    yield
    m._cache.clear()


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeClient:
    """httpx.AsyncClient giả — trả payload 24hMoney, đếm số call thật."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls = 0
        self.urls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls += 1
        self.urls.append((url, dict(params or {})))
        payload = self._payload
        if isinstance(payload, dict) and "__routes__" in payload:
            # route theo path: {"__routes__": {"/v1/...": payload}}
            for path, body in payload["__routes__"].items():
                if url.endswith(path):
                    return FakeResponse(body)
            return FakeResponse({"status": 404}, status=404)
        return FakeResponse(payload)


def make_api_payload(stocks: list[dict], last_update_ms: int) -> dict:
    return {"message": "success", "status": 200,
            "data": {"stocks": stocks, "last_update": last_update_ms}}


def patch_api(m, monkeypatch, payload: dict) -> FakeClient:
    """Monkeypatch httpx.AsyncClient trong fetch_top_stocks."""
    fake = FakeClient(payload)

    # QUAN TRỌNG: phải là hàm THƯỜNG trả FakeClient (context manager),
    # không được async def (sẽ trả coroutine → `async with` fail)
    def fake_client(*args, **kwargs):
        return fake

    monkeypatch.setattr(m.httpx, "AsyncClient", fake_client)
    return fake


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
    {"symbol": "HNX-A", "price": 20.0, "basic_price": 16.0, "ceiling_price": 20.0,
     "floor_price": 14.4, "change": 4.0, "change_percent": 25.0,
     "accumylated_vol": 1000, "accumulated_val": 0.02,
     "buy_foreign_qtty": 0, "sell_foreign_qtty": 0},
]

import time as _time
FRESH_S = int(_time.time())            # epoch giây "vừa cập nhật"
STALE_S = int(_time.time() - 3600)     # 1 giờ trước → phải có cảnh báo hết phiên


def ok(data) -> dict:
    return {"message": "success", "status": 200, "data": data}


def routes(**by_path) -> dict:
    """Payload nhiều endpoint: routes(**{"/v1/x": ok([...])})."""
    return {"__routes__": by_path}
