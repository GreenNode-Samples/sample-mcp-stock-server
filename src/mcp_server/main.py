"""VN Stock MCP Server - a pure MCP server (no agent, no LLM, no memory).

Sample pattern: **one Agent Runtime that only exposes tools**. Deploy it as a Custom Agent runtime
on AgentBase (port 8080, GET /health), then register it as an **MCP Connector** in **MCP Gateway**.
Every agent that goes through the gateway (and is allowed by a Policy Group) can then call the
Vietnamese stock-market tools without knowing where this server runs.

Data: the public APIs used by the 24hmoney.vn web app (`api-finance-t19.24hmoney.vn`).
This is an unofficial API - for demo / sample use only, never for real trading.

Tools (13):
  Market (one shared source, `top-stock-all`, cached 60s)
    - market_top_stocks(limit, sort) / top_gainers / top_losers / most_active / stock_quote
  Companies / tickers
    - search_company(query, limit)    -> find a ticker by name or ticker (~1.6k HOSE / HNX / UPCOM companies)
    - company_profile(symbol)         -> name, exchange, business description
    - price_history(symbol, days)     -> daily close, volume, value (up to 30 sessions)
    - foreign_trading(symbol, days)   -> daily foreign buy/sell (up to 25 sessions)
    - valuation(symbol)               -> P/E, P/B, ROE, ROA, EPS... vs the industry average
    - dividend_history(symbol, limit) -> cash / stock dividend history
    - business_plan(symbol)           -> annual plan and % completed
    - company_announcements(symbol, limit) -> corporate disclosures (with document links)

Results: every tool returns a dict (MCP structured output). Failures raise ToolError, so the MCP result
has `isError: true` and a short message the agent can read. Every result carries `source`; results that
contain numbers also carry `units`. Dates are `YYYY-MM-DD`, timestamps are ISO 8601 with the +07:00 offset.

Auth (fail-closed): /mcp requires an API key.
  - MCP_API_KEYS="key1,key2" (several keys allow rotation; read once at startup, so a restart is needed).
  - Header: `X-Api-Key: <key>` or `Authorization: Bearer <key>`.
  - Every key must be at least 32 characters and must not be a placeholder (contain `<`, `>` or `change-me`).
    A bad key makes /mcp answer 503 and logs the reason at startup; the server never opens itself up.
  - No key configured -> /mcp answers 503. Only for local development set ALLOW_ANONYMOUS=true.
  - /health and / are always open (runtime health probe).
  Behind MCP Gateway the connector `stock` uses Outbound Auth = API Key, the secret lives in Access
  Control and the gateway adds the header when it forwards the call, so agents never see the key.
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import string
import time
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vn-stock-mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs every request URL, which carries the device_id

# ────────────────────────── Configuration (env-overridable) ──────────────────────────

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

SOURCE = "24hmoney.vn"

# Total time budget (seconds) for the upstream work of one tool call: all attempts and back-offs included.
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "60"))  # session data
SLOW_TTL = int(os.environ.get("SLOW_CACHE_TTL_SECONDS", "900"))  # dividends, plan, valuation, announcements
COMPANY_TTL = int(os.environ.get("COMPANY_CACHE_TTL_SECONDS", "21600"))  # company directory (6h)

# Seconds to wait before attempt 2 and attempt 3; so a call makes at most len(RETRY_DELAYS) + 1 attempts.
RETRY_DELAYS = (0.5, 1.5)
MAX_CACHE_ENTRIES = 512

TZ_VN = ZoneInfo("Asia/Ho_Chi_Minh")
STALE_AFTER = 15 * 60  # no update for 15 minutes -> the market may be closed

MAX_LIMIT = 30  # list tools
MAX_HISTORY_DAYS = 30  # price_history
MAX_FOREIGN_DAYS = 25  # foreign_trading
MAX_ANNOUNCEMENTS = 20  # company_announcements
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")


# ────────────────────────────── Authentication config ──────────────────────────────

MIN_KEY_LENGTH = 32
PLACEHOLDER_MARKER = "change-me"  # the example env files ship `change-me-run-openssl-rand-hex-32`


@dataclass(frozen=True)
class AuthConfig:
    keys: tuple[str, ...]
    problems: tuple[str, ...]  # reasons the configured keys are rejected; non-empty -> /mcp is locked
    allow_anonymous: bool


def _load_auth(raw_keys: str, allow_anonymous: bool) -> AuthConfig:
    """Parse MCP_API_KEYS and reject keys that are placeholders or too weak to be real secrets."""
    keys = tuple(k.strip() for k in raw_keys.split(",") if k.strip())
    problems = []
    for i, key in enumerate(keys, start=1):
        if "<" in key or ">" in key or PLACEHOLDER_MARKER in key.lower():
            problems.append(f"MCP_API_KEYS entry #{i} is a placeholder - generate a key with `openssl rand -hex 32`")
        elif len(key) < MIN_KEY_LENGTH:
            problems.append(f"MCP_API_KEYS entry #{i} is shorter than {MIN_KEY_LENGTH} characters")
    return AuthConfig(keys, tuple(problems), allow_anonymous)


AUTH = _load_auth(
    os.environ.get("MCP_API_KEYS", ""),
    os.environ.get("ALLOW_ANONYMOUS", "").strip().lower() in ("1", "true", "yes"),
)
for _problem in AUTH.problems:
    log.error("%s - /mcp will answer 503 until it is fixed", _problem)


def _describe_auth() -> str:
    if AUTH.problems:
        return "locked (invalid MCP_API_KEYS)"
    if AUTH.keys:
        return f"api-key ({len(AUTH.keys)} key)"
    return "anonymous (ALLOW_ANONYMOUS, local use only)" if AUTH.allow_anonymous else "locked (MCP_API_KEYS not set)"


# ────────────────────────── Upstream client + cache ──────────────────────────


def _browser_id() -> str:
    """device_id / browser_id the way the 24hmoney web app generates it: web + epoch_ms + random."""
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

_client: httpx.AsyncClient | None = None  # created lazily, closed by the app lifespan
_cache: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, payload); insertion-ordered
_inflight: dict[str, asyncio.Task] = {}  # key -> the upstream request currently running for it


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=BROWSER_HEADERS)
    return _client


async def _close_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aclose()


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _cache_put(key: str, ttl: int, value: Any) -> None:
    """Store a payload; drop expired entries first and keep at most MAX_CACHE_ENTRIES (oldest out)."""
    now = time.monotonic()
    for stale in [k for k, (expires, _) in _cache.items() if expires <= now]:
        del _cache[stale]
    _cache.pop(key, None)
    _cache[key] = (now + ttl, value)
    while len(_cache) > MAX_CACHE_ENTRIES:
        del _cache[next(iter(_cache))]


def _unexpected(detail: str) -> ToolError:
    return ToolError(f"24hMoney API returned an unexpected data format ({detail}).")


def _status_message(code: int) -> str:
    hint = {400: "bad request", 401: "unauthorized", 403: "access denied", 404: "not found"}.get(code)
    detail = f"HTTP {code}, {hint}" if hint else f"HTTP {code}"
    return f"24hMoney API rejected the request ({detail})."


async def _get_json(path: str, query: dict) -> Any:
    """GET one endpoint. Retries only transport errors, HTTP 5xx and 429; any other failure ends the call."""
    client = _get_client()
    problem = ""
    for delay in (*RETRY_DELAYS, None):
        try:
            res = await client.get(API_BASE + path, params=query)
        except httpx.TransportError as e:
            problem = f"network error: {type(e).__name__}"
        except httpx.HTTPError as e:
            log.warning("upstream %s failed: %s", path, type(e).__name__)
            raise ToolError("24hMoney API request failed.") from None
        else:
            if res.is_success:
                try:
                    return res.json()
                except ValueError:
                    raise _unexpected("not JSON") from None
            if res.status_code != 429 and res.status_code < 500:
                raise ToolError(_status_message(res.status_code))
            problem = f"HTTP {res.status_code}"
        if delay is None:
            raise ToolError(
                f"24hMoney API is unavailable ({problem}) after {len(RETRY_DELAYS) + 1} attempts. Try again later."
            )
        log.warning("upstream %s: %s, retrying in %.1fs", path, problem, delay)
        await asyncio.sleep(delay)


async def _load(key: str, path: str, query: dict, ttl: int, parse) -> Any:
    try:
        async with asyncio.timeout(HTTP_TIMEOUT):
            body = await _get_json(path, query)
    except TimeoutError:
        raise ToolError(f"24hMoney API did not answer within {HTTP_TIMEOUT:g}s. Try again later.") from None
    if not isinstance(body, dict) or body.get("status") != 200 or "data" not in body:
        raise _unexpected("unknown response envelope")
    data = parse(body["data"])  # validate the shape BEFORE caching
    if data:  # never cache an empty payload
        _cache_put(key, ttl, data)
    return data


def _release(key: str, task: asyncio.Task) -> None:
    if _inflight.get(key) is task:
        del _inflight[key]
    if not task.cancelled():
        task.exception()  # mark as retrieved: every waiter may have gone away already


async def fetch_api(path: str, params: dict | None = None, *, cache_key: str, ttl: int, parse) -> Any:
    """Return the validated `data` of one 24hMoney endpoint.

    `parse(data)` checks the shape (raising ToolError when it is wrong) and returns the cleaned payload.
    Only valid, non-empty payloads are cached. Concurrent cold calls with the same `cache_key` share one
    upstream request, which keeps running even if one of the callers is cancelled.
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    task = _inflight.get(cache_key)
    if task is None:
        task = asyncio.create_task(_load(cache_key, path, {**API_PARAMS, **(params or {})}, ttl, parse))
        _inflight[cache_key] = task
        task.add_done_callback(partial(_release, cache_key))
    return await asyncio.shield(task)


def _parse_rows(data: Any) -> list[dict]:
    if not isinstance(data, list):
        raise _unexpected("expected a list")
    return [row for row in data if isinstance(row, dict)]


def _parse_object(data: Any) -> dict:
    if not isinstance(data, dict):
        raise _unexpected("expected an object")
    return data


def _parse_top_stocks(data: Any) -> dict:
    stocks = _parse_object(data).get("stocks")
    if not isinstance(stocks, list):
        raise _unexpected("missing stock list")
    rows = [s for s in stocks if isinstance(s, dict)]
    if not rows:
        raise ToolError("24hMoney API returned an empty list of top stocks. Try again later.")
    return {"stocks": rows, "last_update": data.get("last_update")}


def _parse_companies(data: Any) -> list[dict]:
    rows = _parse_rows(data)
    if not rows:
        raise ToolError("24hMoney API returned an empty company directory. Try again later.")
    return rows


async def fetch_top_stocks() -> dict:
    """~90 top market stocks (price, +/-%, volume, value, foreign flow): {"stocks": [...], "last_update": ts}."""
    return await fetch_api(TOP_STOCK_PATH, cache_key="top-stocks", ttl=CACHE_TTL, parse=_parse_top_stocks)


async def fetch_companies() -> list[dict]:
    """Directory of ~1.6k listed companies (HOSE / HNX / UPCOM), cached 6h."""
    return await fetch_api(COMPANY_ALL_PATH, {"time_updated": 0}, cache_key="companies",
                           ttl=COMPANY_TTL, parse=_parse_companies)


# ────────────────────────── Normalisation helpers ──────────────────────────
# Upstream values are untrusted: a field may be missing, null or have drifted to another type.
# These helpers turn anything unexpected into None / "" so the tools never raise on bad data.


def _num(value: Any) -> int | float | None:
    """A finite number, or None. Numeric strings are accepted; booleans and everything else are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _zero(value: Any) -> int | float:
    """Like _num but a missing value counts as 0 - only for sorting and filtering."""
    return _num(value) or 0


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def price_state(s: dict) -> str:
    """Price relative to the ceiling / floor / reference price."""
    price, basic = _num(s.get("price")), _num(s.get("basic_price"))
    ceiling, floor = _num(s.get("ceiling_price")), _num(s.get("floor_price"))
    if price is None:
        return "unknown"
    if ceiling is not None and price >= ceiling:
        return "at_ceiling"
    if floor is not None and price <= floor:
        return "at_floor"
    if basic is None:
        return "unknown"
    return "up" if price > basic else "down" if price < basic else "unchanged"


def _fmt_quote(s: dict, rank: int | None = None) -> dict:
    out = {
        "symbol": _text(s.get("symbol")) or None,
        "price": _num(s.get("price")),
        "change": _num(s.get("change")),
        "change_percent": _num(s.get("change_percent")),
        "state": price_state(s),
        "reference_price": _num(s.get("basic_price")),
        "ceiling_price": _num(s.get("ceiling_price")),
        "floor_price": _num(s.get("floor_price")),
        # the upstream field name is misspelled "accumylated_vol" - keep it when reading
        "matched_volume": _num(s.get("accumylated_vol")),
        "matched_value_bn_vnd": _num(s.get("accumulated_val")),
        "foreign_buy_volume": _num(s.get("buy_foreign_qtty")),
        "foreign_sell_volume": _num(s.get("sell_foreign_qtty")),
    }
    return {"rank": rank, **out} if rank is not None else out


def _epoch_to_vn(ts: Any) -> datetime | None:
    ts = _num(ts)
    if ts is None or ts <= 0:
        return None
    ts_sec = ts / 1000 if ts > 1e12 else float(ts)  # the API sends epoch seconds; tolerate milliseconds
    return datetime.fromtimestamp(ts_sec, tz=UTC).astimezone(TZ_VN)


def _vn_date(ts: Any) -> str | None:
    d = _epoch_to_vn(ts)
    return d.strftime("%Y-%m-%d") if d else None


def _market_meta(data: dict) -> dict:
    meta: dict = {"stocks_tracked": len(data["stocks"])}
    ts = _epoch_to_vn(data.get("last_update"))
    if ts:
        meta["last_update"] = ts.isoformat(timespec="seconds")
        age = time.time() - ts.timestamp()
        if age > STALE_AFTER:
            meta["note"] = (
                f"Data was last updated {int(age // 60)} minutes ago - the market may be closed "
                "or the upstream feed may have stopped updating."
            )
    return meta


SORTS = {
    "default": None,  # upstream order (24hMoney recommendation)
    "change_percent": lambda s: _zero(s.get("change_percent")),
    "value": lambda s: _zero(s.get("accumulated_val")),
    "volume": lambda s: _zero(s.get("accumylated_vol")),
    "foreign_net_buy": lambda s: _zero(s.get("buy_foreign_qtty")) - _zero(s.get("sell_foreign_qtty")),
}


SortKey = Literal["default", "change_percent", "value", "volume", "foreign_net_buy"]  # keys of SORTS


def _clamp(value: int, hi: int) -> int:
    """Limit a count to 1..hi. Tool arguments are validated as integers before they reach the tools."""
    return max(1, min(hi, value))


def _norm_symbol(symbol: str) -> str | None:
    sym = symbol.strip().upper()
    return sym if SYMBOL_RE.match(sym) else None


def _require_symbol(symbol: str) -> str:
    sym = _norm_symbol(symbol)
    if not sym:
        raise ToolError(f"Invalid stock symbol: '{symbol}'. Valid examples: FPT, VCB, HPG. "
                        "Use search_company if you do not know the ticker.")
    return sym


def _fold(text: str) -> str:
    """Strip Vietnamese diacritics and lowercase, so 'Hòa Phát' matches 'hoa phat'."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def _newest_first(rows: list[dict], field: str = "trading_date") -> list[dict]:
    return sorted(rows, key=lambda r: _zero(r.get(field)), reverse=True)


# Units, keyed by the kind of number a field holds; every result carries the ones it uses.
UNITS_MARKET = {"price": "thousand VND", "value": "billion VND", "volume": "shares"}


def _result(payload: dict, units: dict | None = None) -> dict:
    """Common envelope: `units` (when the result has numbers) and `source` sit at the top level of every tool result."""
    return {**payload, **({"units": units} if units else {}), "source": SOURCE}


# ────────────────────────────── MCP tools ──────────────────────────────

# host="0.0.0.0" is required, do not remove it: with the default host (127.0.0.1) FastMCP enables DNS-rebinding
# protection that only accepts Host headers of localhost, so every request forwarded by MCP Gateway (Host =
# the runtime / VPC address) would be rejected. Access is controlled by the API key middleware below.
mcp = FastMCP("vn-stock", stateless_http=True, json_response=True, host="0.0.0.0")

# Every tool only reads data (readOnlyHint) from an external service (openWorldHint).
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

Symbol = Annotated[str, Field(description="Stock ticker, 2-10 letters or digits, case-insensitive (e.g. FPT, VCB, HPG).")]
Limit = Annotated[int, Field(description=f"Maximum number of results, 1-{MAX_LIMIT}; larger values are clamped.")]


async def _ranked(limit: int, key=None, keep=None, reverse: bool = True) -> dict:
    data = await fetch_top_stocks()
    stocks = [s for s in data["stocks"] if keep is None or keep(s)]
    if key:
        stocks.sort(key=key, reverse=reverse)
    top = stocks[:_clamp(limit, MAX_LIMIT)]
    return _result({
        **_market_meta(data),
        "count": len(top),
        "stocks": [_fmt_quote(s, rank=i + 1) for i, s in enumerate(top)],
    }, UNITS_MARKET)


@mcp.tool(annotations=READ_ONLY)
async def market_top_stocks(
    limit: Limit = 20,
    sort: Annotated[SortKey, Field(description=(
        "'default' (24hMoney recommendation order), 'change_percent' (+/- %), 'value' (traded value), "
        "'volume' (traded volume) or 'foreign_net_buy' (foreign net buy volume)."))] = "default",
) -> dict[str, Any]:
    """Vietnamese market table: the ~90 most liquid stocks (source: 24hMoney), sorted as requested.

    Example: market_top_stocks(limit=15, sort='value').
    """
    return await _ranked(limit, SORTS[sort])


@mcp.tool(annotations=READ_ONLY)
async def top_gainers(limit: Limit = 10) -> dict[str, Any]:
    """Stocks with the largest gains today (by % change; only stocks that are up).

    Example: top_gainers(limit=5).
    """
    return await _ranked(limit, lambda s: _zero(s.get("change_percent")),
                         keep=lambda s: _zero(s.get("change_percent")) > 0)


@mcp.tool(annotations=READ_ONLY)
async def top_losers(limit: Limit = 10) -> dict[str, Any]:
    """Stocks with the deepest losses today (by % change; only stocks that are down).

    Example: top_losers(limit=5).
    """
    return await _ranked(limit, lambda s: _zero(s.get("change_percent")),
                         keep=lambda s: _zero(s.get("change_percent")) < 0, reverse=False)


@mcp.tool(annotations=READ_ONLY)
async def most_active(limit: Limit = 10) -> dict[str, Any]:
    """Most liquid stocks today (by traded value, billion VND).

    Example: most_active(limit=15).
    """
    return await _ranked(limit, lambda s: _zero(s.get("accumulated_val")))


@mcp.tool(annotations=READ_ONLY)
async def stock_quote(symbol: Symbol) -> dict[str, Any]:
    """Quote for one stock in the top group (~90 most liquid): price, +/- %, ceiling / floor,
    matched volume and value, foreign buy / sell volume.

    For a stock outside the top group use price_history(symbol, days=1).
    Example: stock_quote(symbol='VIC') or stock_quote(symbol='vic').
    """
    sym = _require_symbol(symbol)
    data = await fetch_top_stocks()
    for s in data["stocks"]:
        if _text(s.get("symbol")).upper() == sym:
            return _result({**_market_meta(data), "quote": _fmt_quote(s)}, UNITS_MARKET)
    raise ToolError(
        f"Symbol '{sym}' is not in the top group (~{len(data['stocks'])} most liquid stocks). "
        "Use market_top_stocks to list it, or price_history(symbol) for any listed stock."
    )


@mcp.tool(annotations=READ_ONLY)
async def search_company(
    query: Annotated[str, Field(description="Company name or ticker, at least 2 characters; "
                                            "case and Vietnamese diacritics are ignored.")],
    limit: Limit = 10,
) -> dict[str, Any]:
    """Find a stock ticker by company name or ticker.

    Covers ~1.6k companies listed on HOSE, HNX and UPCOM.
    Examples: search_company('hoa phat'), search_company('FPT').
    """
    q = _fold(query).strip()
    if len(q) < 2:
        raise ToolError("query needs at least 2 characters")
    scored = []
    for c in await fetch_companies():
        sym = _text(c.get("symbol")).lower()
        names = _fold(" ".join(_text(c.get(f)) for f in
                               ("company_name", "short_name", "company_name_eng", "extra_name")))
        if sym == q:
            score = 0
        elif sym.startswith(q):
            score = 1
        elif q in names:
            score = 2
        else:
            continue
        exch = {"HOSE": 0, "HNX": 1, "UPCOM": 2}.get(_text(c.get("floor")), 3)
        # priority=1 marks ~72 large / well-known companies: rank them first (HPG before HPA for 'hoa phat')
        prio = 1 if c.get("priority") in (1, "1", True) else 0
        scored.append((score, -prio, exch, sym, c))
    scored.sort(key=lambda t: t[:4])
    top = scored[:_clamp(limit, MAX_LIMIT)]
    return _result({
        "query": query,
        "count": len(top),
        "results": [{"symbol": _text(c.get("symbol")) or None,
                     "company_name": _text(c.get("company_name")) or None,
                     "exchange": _text(c.get("floor")) or None} for *_, c in top],
    })


@mcp.tool(annotations=READ_ONLY)
async def company_profile(symbol: Symbol) -> dict[str, Any]:
    """Company profile: full name (Vietnamese / English), listing exchange, business description.

    Example: company_profile(symbol='HPG').
    """
    sym = _require_symbol(symbol)
    for c in await fetch_companies():
        if _text(c.get("symbol")).upper() == sym:
            desc = _text(c.get("description"))
            return _result({
                "symbol": sym,
                "company_name": _text(c.get("company_name")) or None,
                "company_name_eng": _text(c.get("company_name_eng")) or None,
                "short_name": _text(c.get("short_name")) or None,
                "exchange": _text(c.get("floor")) or None,
                "description": desc[:1200] + ("…" if len(desc) > 1200 else ""),
            })
    raise ToolError(f"No company found with symbol '{sym}'. Try search_company.")


@mcp.tool(annotations=READ_ONLY)
async def price_history(
    symbol: Symbol,
    days: Annotated[int, Field(description=f"Number of trading sessions, 1-{MAX_HISTORY_DAYS}; "
                                           "larger values are clamped.")] = 10,
) -> dict[str, Any]:
    """Price history per trading session, newest first: close, reference price, +/- %, matched volume and value.

    The summary covers the returned sessions: first and last date, highest and lowest CLOSE, and the
    % change from the oldest session's reference price to the latest close.
    Example: price_history(symbol='FPT', days=20).
    """
    sym = _require_symbol(symbol)
    rows = await fetch_api(TRADING_HISTORY_PATH, {"symbol": sym}, cache_key=f"history:{sym}",
                           ttl=CACHE_TTL, parse=_parse_rows)
    if not rows:
        raise ToolError(f"No price history for '{sym}'.")
    rows = _newest_first(rows)[:_clamp(days, MAX_HISTORY_DAYS)]
    sessions = []
    for r in rows:
        close, ref = _num(r.get("match_price")), _num(r.get("basic_price"))
        chg = round(close - ref, 2) if close is not None and ref else None
        sessions.append({
            "date": _vn_date(r.get("trading_date")),
            "close": close,
            "reference": ref,
            "change": chg,
            "change_percent": round(chg / ref * 100, 2) if chg is not None else None,
            "volume": _num(r.get("accumulated_vol")),
            "value_bn_vnd": _num(r.get("accumulated_val")),
        })
    closes = [s["close"] for s in sessions if s["close"] is not None]
    first_ref = _num(rows[-1].get("basic_price"))
    return _result({
        "symbol": sym,
        "count": len(sessions),
        "summary": {
            "from": sessions[-1]["date"],
            "to": sessions[0]["date"],
            "highest_close": max(closes) if closes else None,
            "lowest_close": min(closes) if closes else None,
            "period_change_percent": (round((closes[0] - first_ref) / first_ref * 100, 2)
                                      if closes and first_ref else None),
        },
        "sessions": sessions,
    }, UNITS_MARKET)


@mcp.tool(annotations=READ_ONLY)
async def foreign_trading(
    symbol: Symbol,
    days: Annotated[int, Field(description=f"Number of trading sessions, 1-{MAX_FOREIGN_DAYS}; "
                                           "larger values are clamped.")] = 10,
) -> dict[str, Any]:
    """Foreign investor trading per session, newest first: buy / sell volume and value, net value.

    Values the upstream does not report stay null; they are never counted as 0 in the net figures.
    Example: foreign_trading(symbol='VNM', days=5).
    """
    sym = _require_symbol(symbol)
    rows = await fetch_api(FOREIGN_HISTORY_PATH, {"symbol": sym}, cache_key=f"foreign:{sym}",
                           ttl=CACHE_TTL, parse=_parse_rows)
    if not rows:
        raise ToolError(f"No foreign trading data for '{sym}'.")
    rows = _newest_first(rows)[:_clamp(days, MAX_FOREIGN_DAYS)]
    sessions = []
    for r in rows:
        buy, sell = _num(r.get("buy_foreign_val")), _num(r.get("sell_foreign_val"))
        sessions.append({
            "date": _vn_date(r.get("trading_date")),
            "close": _num(r.get("match_price")),
            "buy_volume": _num(r.get("buy_foreign_qtty")),
            "sell_volume": _num(r.get("sell_foreign_qtty")),
            "buy_value_bn_vnd": buy,
            "sell_value_bn_vnd": sell,
            "net_value_bn_vnd": round(buy - sell, 3) if buy is not None and sell is not None else None,
        })
    nets = [s["net_value_bn_vnd"] for s in sessions if s["net_value_bn_vnd"] is not None]
    net = round(sum(nets), 3) if nets else None
    return _result({
        "symbol": sym,
        "count": len(sessions),
        "summary": {
            "sessions_with_net_value": len(nets),
            "net_value_bn_vnd": net,
            "trend": None if net is None else "net_buy" if net > 0 else "net_sell" if net < 0 else "balanced",
        },
        "sessions": sessions,
    }, UNITS_MARKET)


VALUATION_METRICS = {  # upstream key -> (label, unit)
    "pe": ("P/E", "x"),
    "pb": ("P/B", "x"),
    "roe": ("ROE", "%"),
    "roa": ("ROA", "%"),
    "eps": ("EPS", "VND"),
    "net_profit_margin": ("Net profit margin", "%"),
    "ev_per_ebitda": ("EV/EBITDA", "x"),
}


@mcp.tool(annotations=READ_ONLY)
async def valuation(symbol: Symbol) -> dict[str, Any]:
    """Valuation and efficiency metrics: P/E, P/B (vs the industry average), ROE, ROA, EPS,
    net profit margin, EV/EBITDA - with a short comment from 24hMoney.

    Example: valuation(symbol='FPT').
    """
    sym = _require_symbol(symbol)
    data = await fetch_api(VALUATION_PATH, {"symbol": sym}, cache_key=f"valuation:{sym}",
                           ttl=SLOW_TTL, parse=_parse_object)
    metrics = {}
    for key, (label, _unit) in VALUATION_METRICS.items():
        v = data.get(key)
        value = _num(v.get("value")) if isinstance(v, dict) else None
        if value is None:
            continue
        item = {"label": label, "value": round(value, 2)}
        industry_avg = _num(v.get("group_value"))
        if industry_avg is not None:
            item["industry_avg"] = round(industry_avg, 2)
        if _text(v.get("message")):
            item["note"] = _text(v["message"])
        metrics[key] = item
    if not metrics:
        raise ToolError(f"No valuation data for '{sym}'.")
    return _result({"symbol": sym, "industry": _text(data.get("group_name")) or None, "metrics": metrics},
                   {key: unit for key, (_label, unit) in VALUATION_METRICS.items()})


DIVIDEND_TYPES = {1: "cash", 2: "stock", 3: "bonus_shares"}  # 3 = bonus shares / capital-raising issue
PAR_VALUE_VND = 10_000


@mcp.tool(annotations=READ_ONLY)
async def dividend_history(symbol: Symbol, limit: Limit = 10) -> dict[str, Any]:
    """Dividend history, newest first: date, type (cash / stock / bonus_shares) and ratio.

    The ratio is a percentage of the 10,000 VND par value (10% cash = 1,000 VND per share).
    Example: dividend_history(symbol='FPT').
    """
    sym = _require_symbol(symbol)
    rows = await fetch_api(DIVIDEND_PATH, {"symbol": sym}, cache_key=f"dividend:{sym}",
                           ttl=SLOW_TTL, parse=_parse_rows)
    if not rows:
        raise ToolError(f"No dividend history for '{sym}'.")
    rows = sorted(rows, key=lambda r: _text(r.get("end_date")), reverse=True)
    dividends = []
    for r in rows[:_clamp(limit, MAX_LIMIT)]:
        code, ratio = _num(r.get("type")), _num(r.get("ratio"))
        item = {"date": _text(r.get("end_date")) or None,
                "type": DIVIDEND_TYPES.get(code, "other"),
                "ratio_percent": round(ratio * 100, 2) if ratio is not None else None}
        if code == 1 and ratio is not None:
            item["cash_vnd_per_share"] = round(ratio * PAR_VALUE_VND)
        dividends.append(item)
    return _result({"symbol": sym, "count": len(dividends), "dividends": dividends},
                   {"ratio_percent": f"% of the {PAR_VALUE_VND:,} VND par value", "cash_vnd_per_share": "VND"})


@mcp.tool(annotations=READ_ONLY)
async def business_plan(symbol: Symbol) -> dict[str, Any]:
    """Annual business plan (revenue, profit) and % completed up to the latest quarter.

    Example: business_plan(symbol='MWG').
    """
    sym = _require_symbol(symbol)
    data = await fetch_api(PLAN_PATH, {"symbol": sym}, cache_key=f"plan:{sym}",
                           ttl=SLOW_TTL, parse=_parse_object)
    plan = data.get("plan")
    items = [p for p in plan if isinstance(p, dict)] if isinstance(plan, list) else []
    if not items:
        raise ToolError(f"No business plan for '{sym}'.")
    return _result({
        "symbol": sym,
        "year": _num(data.get("year")),
        "through_quarter": _num(data.get("quarter")),
        "count": len(items),
        "plan": [{"item": _text(p.get("label")) or None, "target": _num(p.get("expect")),
                  "actual": _num(p.get("current")), "completed_percent": _num(p.get("percent"))}
                 for p in items],
    }, {"target": "billion VND", "actual": "billion VND", "completed_percent": "%"})


@mcp.tool(annotations=READ_ONLY)
async def company_announcements(
    symbol: Symbol,
    limit: Annotated[int, Field(description=f"Maximum number of announcements, 1-{MAX_ANNOUNCEMENTS}; "
                                            "larger values are clamped.")] = 5,
) -> dict[str, Any]:
    """Latest corporate disclosures of a company, newest first: title, date, document link.

    Example: company_announcements(symbol='VCB', limit=10).
    """
    sym = _require_symbol(symbol)
    rows = await fetch_api(ANNOUNCEMENT_PATH, {"symbol": sym, "per_page": MAX_ANNOUNCEMENTS, "timestamp": 0},
                           cache_key=f"announce:{sym}", ttl=SLOW_TTL, parse=_parse_rows)
    if not rows:
        raise ToolError(f"No announcements for '{sym}'.")
    announcements = []
    for r in rows[:_clamp(limit, MAX_ANNOUNCEMENTS)]:
        links = r.get("link")
        announcements.append({
            "date": _vn_date(r.get("published_date")),
            "title": _text(r.get("title")) or None,
            "url": links[0] if isinstance(links, list) and links and isinstance(links[0], str) else None,
        })
    return _result({"symbol": sym, "count": len(announcements), "announcements": announcements})


# ────────────────────────── HTTP app (runtime contract) ──────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness only: nothing about configuration, keys or upstream state."""
    return JSONResponse({"status": "ok", "tools": len(await mcp.list_tools())})


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request) -> JSONResponse:
    return JSONResponse({
        "server": "vn-stock-mcp",
        "what": "pure MCP server - no LLM, no memory; stock tools served to agents via MCP Gateway",
        "mcp_endpoint": "/mcp",
        "auth": "X-Api-Key: <key>  or  Authorization: Bearer <key>",
        "tools": [t.name for t in await mcp.list_tools()],
        "data_source": "24hmoney.vn (unofficial public API)",
    })


# streamable_http_app() returns a Starlette app whose lifespan runs the MCP session manager and whose
# routes include the custom ones above. It is wrapped, not mounted: a mounted sub-app would not run its lifespan.
asgi_app = mcp.streamable_http_app()
# The connector URL is exactly /mcp. Without this, /mcp/ would answer a 307 redirect whose Location
# is built from the (proxy-hidden) scheme and host.
asgi_app.router.redirect_slashes = False

_mcp_lifespan = asgi_app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app):
    try:
        async with _mcp_lifespan(app):
            yield
    finally:
        await _close_client()


asgi_app.router.lifespan_context = _lifespan


def _extract_key(headers) -> str:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers or []}
    if h.get("x-api-key"):
        return h["x-api-key"].strip()
    auth = h.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return ""


def _key_valid(supplied: str) -> bool:
    if not supplied:
        return False
    ok = False
    for good in AUTH.keys:  # check every key so timing does not reveal which one matched
        ok |= secrets.compare_digest(supplied.encode(), good.encode())
    return ok


class RequireApiKeyMiddleware:
    """Fail-closed ASGI middleware for /mcp.

    - Invalid MCP_API_KEYS (placeholder / too short) -> 503, even with ALLOW_ANONYMOUS.
    - Valid keys configured -> a valid key is required (401 if missing or wrong).
    - No key and ALLOW_ANONYMOUS=true -> open (local development only).
    - No key and no ALLOW_ANONYMOUS -> 503: the server never opens itself up.
    - /health and / are always open (the runtime health probe must get 200).
    """

    def __init__(self, app):
        self.app = app

    @staticmethod
    async def _reply(send, status: int, message: str, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *extra]})
        await send({"type": "http.response.body", "body": json.dumps({"error": message}).encode()})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path == "/mcp" or path.startswith("/mcp/")):
            if AUTH.problems:
                return await self._reply(send, 503, "MCP server API key configuration is invalid (see the server log)")
            if not AUTH.keys:
                if not AUTH.allow_anonymous:
                    return await self._reply(send, 503, "MCP server has no MCP_API_KEYS configured (fail-closed)")
            elif not _key_valid(_extract_key(scope.get("headers"))):
                client = (scope.get("client") or ("?",))[0]
                log.warning("401 /mcp from %s - missing or invalid API key", client)
                return await self._reply(send, 401, "missing or invalid API key (X-Api-Key / Bearer)",
                                         [(b"www-authenticate", b'Bearer realm="vn-stock-mcp"')])
        await self.app(scope, receive, send)


app = RequireApiKeyMiddleware(asgi_app)


if __name__ == "__main__":
    import uvicorn

    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080"))
    log.info("vn-stock-mcp listening on %s:%d - auth: %s", host, port, _describe_auth())
    uvicorn.run(app, host=host, port=port)
