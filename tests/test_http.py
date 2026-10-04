"""HTTP surface: /mcp auth (fail-closed), /health, structured results and tool errors over real MCP calls."""

import json
import re
from pathlib import Path

import httpx
import pytest
from conftest import FRESH_S, SAMPLE_STOCKS, VALID_KEY, make_api_payload, ok, patch_api

ACCEPT = {"Accept": "application/json, text/event-stream"}
KEY_A, KEY_B = "a" * 32, "b" * 32


def rpc(client, method, params=None, headers=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers={**ACCEPT, **(headers or {})})


def call_tool(client, name, arguments):
    r = rpc(client, "tools/call", {"name": name, "arguments": arguments}, {"X-Api-Key": VALID_KEY})
    assert r.status_code == 200
    return r.json()["result"]


@pytest.fixture()
def authed(m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth(VALID_KEY, False))


# ─────────────── Key validation (startup) ───────────────


@pytest.mark.parametrize("raw", [
    "<openssl rand -hex 32>",
    "change-me",
    "change-me-run-openssl-rand-hex-32",
    "CHANGE-ME-run-openssl-rand-hex-32",
    "short",
    "a" * 31,
    "a" * 40 + ">",
    KEY_A + ",change-me",    # one bad entry rejects the whole configuration
    KEY_A + ",short",
])
def test_bad_keys_are_rejected(m, raw):
    assert m._load_auth(raw, False).problems


@pytest.mark.parametrize("raw", [KEY_A, "a" * 32, KEY_A + "," + KEY_B, f" {KEY_A} , ,{KEY_B} "])
def test_good_keys_are_accepted(m, raw):
    auth = m._load_auth(raw, False)
    assert not auth.problems and auth.keys == (tuple(k.strip() for k in raw.split(",") if k.strip()))


def test_empty_config_is_not_a_problem_but_has_no_keys(m):
    auth = m._load_auth("", False)
    assert auth.keys == () and auth.problems == ()


def test_example_files_ship_rejected_placeholders(m):
    """The example secrets must be placeholders the server refuses, never values it would accept."""
    root = Path(__file__).resolve().parents[1]
    files = [root / ".env.example", root / "deploy/vserver/.env.example", root / "deploy/onprem/.env.example",
             root / "deploy/vks/secret.example.yaml"]
    for f in files:
        match = re.search(r"^\s*MCP_API_KEYS\s*[=:]\s*\"?([^\"\n]+)\"?\s*$", f.read_text(), re.MULTILINE)
        assert match, f"{f} has no MCP_API_KEYS"
        assert m._load_auth(match.group(1), False).problems, f"{f} ships a key the server would accept"


# ─────────────── /mcp authentication ───────────────


def test_no_key_configured_is_locked(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth("", False))
    assert rpc(mcp_client, "tools/list").status_code == 503


def test_anonymous_mode_opens_mcp_for_local_dev(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth("", True))
    r = rpc(mcp_client, "tools/list")
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 13


@pytest.mark.parametrize("raw", ["<openssl rand -hex 32>", "change-me-run-openssl-rand-hex-32", "short"])
@pytest.mark.parametrize("anonymous", [False, True])
def test_invalid_keys_lock_mcp_even_for_the_placeholder_itself(mcp_client, m, monkeypatch, raw, anonymous):
    monkeypatch.setattr(m, "AUTH", m._load_auth(raw, anonymous))
    assert rpc(mcp_client, "tools/list").status_code == 503
    assert rpc(mcp_client, "tools/list", headers={"X-Api-Key": raw}).status_code == 503
    assert rpc(mcp_client, "tools/list", headers={"Authorization": f"Bearer {raw}"}).status_code == 503


def test_keys_are_mandatory_even_with_allow_anonymous(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth(f"{KEY_A},{KEY_B}", True))
    r = rpc(mcp_client, "tools/list")
    assert r.status_code == 401 and "www-authenticate" in r.headers
    assert rpc(mcp_client, "tools/list", headers={"X-Api-Key": "wrong"}).status_code == 401


def test_accepted_headers_and_rotation(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth(f"{KEY_A},{KEY_B}", False))
    for headers in ({"X-Api-Key": KEY_A}, {"X-Api-Key": KEY_B}, {"Authorization": f"Bearer {KEY_B}"}):
        assert rpc(mcp_client, "tools/list", headers=headers).status_code == 200, headers


def test_legacy_stock_key_header_is_gone(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth(KEY_A, False))
    assert rpc(mcp_client, "tools/list", headers={"X-Stock-Api-Key": KEY_A}).status_code == 401


def test_trailing_slash_is_not_redirected(mcp_client, authed):
    r = mcp_client.post("/mcp/", json={}, headers={**ACCEPT, "X-Api-Key": VALID_KEY}, follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers


# ─────────────── /health and / ───────────────


@pytest.mark.parametrize("raw,anonymous", [("", False), ("", True), (KEY_A, False), ("change-me", False)])
def test_health_is_liveness_only_and_never_leaks_auth(mcp_client, m, monkeypatch, raw, anonymous):
    monkeypatch.setattr(m, "AUTH", m._load_auth(raw, anonymous))
    r = mcp_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "tools": 13}


def test_root_describes_the_server_without_auth_state(mcp_client, m, monkeypatch):
    monkeypatch.setattr(m, "AUTH", m._load_auth(KEY_A, False))
    r = mcp_client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert len(body["tools"]) == 13 and body["mcp_endpoint"] == "/mcp"
    assert KEY_A not in r.text


# ─────────────── MCP protocol: tool metadata ───────────────


def test_tool_metadata(mcp_client, authed):
    tools = rpc(mcp_client, "tools/list", headers={"X-Api-Key": VALID_KEY}).json()["result"]["tools"]
    assert len(tools) == 13
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["openWorldHint"] is True
        assert tool["description"], tool["name"]
        assert "result" not in tool["outputSchema"].get("properties", {})  # a plain dict, not a wrapped value
        for name, prop in tool["inputSchema"]["properties"].items():
            assert prop.get("description"), f"{tool['name']}.{name} has no description"


def test_sort_parameter_is_an_enum_in_the_schema(mcp_client, m, authed):
    tools = rpc(mcp_client, "tools/list", headers={"X-Api-Key": VALID_KEY}).json()["result"]["tools"]
    sort = next(t for t in tools if t["name"] == "market_top_stocks")["inputSchema"]["properties"]["sort"]
    assert set(sort["enum"]) == set(m.SORTS) and sort["description"]


def test_tool_names_come_from_the_registry(mcp_client, authed):
    names = {t["name"] for t in rpc(mcp_client, "tools/list", headers={"X-Api-Key": VALID_KEY}).json()["result"]["tools"]}
    assert names == {"market_top_stocks", "top_gainers", "top_losers", "most_active", "stock_quote",
                     "search_company", "company_profile", "price_history", "foreign_trading", "valuation",
                     "dividend_history", "business_plan", "company_announcements"}


# ─────────────── MCP protocol: results and errors ───────────────


def test_success_is_structured_dict_output(mcp_client, m, monkeypatch, authed):
    patch_api(m, monkeypatch, make_api_payload(SAMPLE_STOCKS, FRESH_S))
    result = call_tool(mcp_client, "top_gainers", {"limit": 2})
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert "result" not in structured                       # not wrapped in {"result": "<json string>"}
    assert structured["count"] == 2 and structured["stocks"][0]["symbol"] == "HNXA"
    assert structured["source"] == "24hmoney.vn"
    assert json.loads(result["content"][0]["text"]) == structured


@pytest.mark.parametrize("tool,args,message", [
    ("stock_quote", {"symbol": "no way"}, "Invalid stock symbol"),
    ("market_top_stocks", {"sort": "bogus"}, "Input should be 'default'"),   # Literal validation
    ("search_company", {"query": "x"}, "at least 2 characters"),
    ("price_history", {"symbol": "FPT", "days": "abc"}, "validation"),   # wrong argument type
])
def test_errors_are_marked_is_error(mcp_client, m, monkeypatch, authed, tool, args, message):
    patch_api(m, monkeypatch, make_api_payload(SAMPLE_STOCKS, FRESH_S))
    result = call_tool(mcp_client, tool, args)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert "structuredContent" not in result or not result["structuredContent"]


@pytest.mark.parametrize("body", [[], None, "maintenance", {"stocks": "x"}])
def test_unexpected_upstream_body_is_an_is_error_result(mcp_client, m, monkeypatch, authed, body):
    patch_api(m, monkeypatch, ok(body))
    for tool, args in (("market_top_stocks", {}), ("price_history", {"symbol": "FPT"}),
                       ("valuation", {"symbol": "FPT"}), ("search_company", {"query": "fpt"})):
        result = call_tool(mcp_client, tool, args)
        assert result["isError"] is True, tool
        assert "AttributeError" not in result["content"][0]["text"]
        assert "TypeError" not in result["content"][0]["text"]


def test_upstream_outage_is_an_is_error_result(mcp_client, m, monkeypatch, authed):
    patch_api(m, monkeypatch, lambda request: httpx.Response(503))
    result = call_tool(mcp_client, "most_active", {})
    assert result["isError"] is True and "unavailable" in result["content"][0]["text"]
    assert m.DEVICE_ID not in result["content"][0]["text"]
