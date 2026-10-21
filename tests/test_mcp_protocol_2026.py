"""MCP 2026-07-28 on ToolWeave's /mcp, and in its DataDictionary client.

The server is FastMCP 4 on the MCP SDK 2, which serves the stateless revision
and the initialize handshake from one endpoint. These tests drive the real
lambda_handler, Mangum and FastMCP app so a dependency bump that drops either
era fails here rather than in a caller.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from toolweave import data_dictionary_client as dd
from toolweave import mcp_auth

TOKEN = "legacy-static-token-value"
MODERN = "2026-07-28"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, "toolweave/mcp-auth-token")
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)
    monkeypatch.delenv("CIPHERWEAVE_JWKS_URL", raising=False)
    secrets = type("S", (), {"get_secret_value": lambda self, SecretId: {"SecretString": TOKEN}})()
    monkeypatch.setattr(mcp_auth, "_secrets_client", lambda: secrets)
    mcp_auth.clear_cache()
    from toolweave import server

    monkeypatch.setattr(server.dynamodb_client, "load_full_catalog", list)
    monkeypatch.setattr(server, "_catalog", [])
    yield server
    mcp_auth.clear_cache()


def _post(server, body, headers):
    hdrs = {"content-type": "application/json", "accept": "application/json, text/event-stream",
            "host": "toolweave.test", "authorization": f"Bearer {TOKEN}"}
    hdrs.update({k.lower(): v for k, v in headers.items() if v is not None})
    response = server.lambda_handler({
        "version": "2.0", "routeKey": "POST /mcp", "rawPath": "/mcp", "rawQueryString": "",
        "headers": hdrs,
        "requestContext": {"http": {"method": "POST", "path": "/mcp", "protocol": "HTTP/1.1",
                                    "sourceIp": "127.0.0.1"}, "stage": "$default",
                           "domainName": "toolweave.test"},
        "body": json.dumps(body), "isBase64Encoded": False,
    }, None)
    text = response["body"]
    if "data:" in text:
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return response["statusCode"], json.loads(text)


def _modern(server, method, params=None, headers=None):
    params = dict(params or {})
    params["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN,
                       "io.modelcontextprotocol/clientCapabilities": {}}
    sent = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        sent["Mcp-Name"] = params["name"]
    sent.update(headers or {})
    return _post(server, {"jsonrpc": "2.0", "id": 3, "method": method, "params": params}, sent)


def test_discover_names_toolweave_not_the_sdk(server):
    status, body = _modern(server, "server/discover")
    assert status == 200
    assert MODERN in body["result"]["supportedVersions"]
    assert body["result"]["_meta"]["io.modelcontextprotocol/serverInfo"] == {"name": "ToolWeave",
                                                                            "version": "0.1.0"}


def test_modern_tools_call_and_list(server):
    status, body = _modern(server, "tools/call", {"name": "health", "arguments": {}})
    assert status == 200 and body["result"]["resultType"] == "complete"
    assert body["result"]["structuredContent"]["status"] == "ok"
    status, body = _modern(server, "tools/list")
    assert status == 200 and body["result"]["cacheScope"] == "private"
    assert "health" in [tool["name"] for tool in body["result"]["tools"]]


def test_mismatched_name_header_is_refused(server):
    status, body = _modern(server, "tools/call", {"name": "health", "arguments": {}},
                           headers={"Mcp-Name": "pre_tool"})
    assert status == 400 and body["error"]["code"] == -32020


def test_unknown_version_lists_the_supported_ones(server):
    status, body = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "1900-01-01",
        "io.modelcontextprotocol/clientCapabilities": {}}}},
        {"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"})
    assert status == 400 and body["error"]["code"] == -32022
    assert MODERN in body["error"]["data"]["supported"]


def test_initialize_still_negotiates_a_handshake_version(server):
    status, body = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"MCP-Protocol-Version": "2025-06-18"})
    assert status == 200 and body["result"]["protocolVersion"] == "2025-06-18"


# ── the DataDictionary client ────────────────────────────────────────────────

@pytest.fixture
def dd_transport(monkeypatch):
    """Route the client's httpx calls to a handler; record what each request sent."""
    seen = []
    handler = {}
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: handler["fn"](request, seen))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(dd, "_legacy_server", None)
    return handler, seen


def _ok(payload):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(payload)}]}})


def test_dd_client_speaks_2026_when_the_server_does(dd_transport):
    handler, seen = dd_transport

    def modern(request, seen):
        body = json.loads(request.content)
        seen.append(request.headers.get("mcp-protocol-version"))
        assert body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN
        assert request.headers["mcp-method"] == "tools/call"
        assert request.headers["mcp-name"] == body["params"]["name"]
        return _ok({"dataElement": "x"})

    handler["fn"] = modern
    assert asyncio.run(dd.get_data_element("x")) == {"dataElement": "x"}
    assert seen == [MODERN] and dd._legacy_server is False


def test_dd_client_falls_back_once_to_a_handshake_server(dd_transport):
    handler, seen = dd_transport

    def previous(request, seen):
        seen.append(request.headers.get("mcp-protocol-version"))
        if request.headers.get("mcp-protocol-version") == MODERN:
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": None,
                                             "error": {"code": -32600, "message": "Bad Request"}})
        return _ok({"dataElement": "x"})

    handler["fn"] = previous
    assert asyncio.run(dd.get_data_element("x")) == {"dataElement": "x"}
    assert asyncio.run(dd.get_data_element("x")) == {"dataElement": "x"}
    assert seen == [MODERN, None, None]


def test_dd_client_does_not_fall_back_on_a_modern_refusal(dd_transport):
    handler, seen = dd_transport

    def strict(request, seen):
        seen.append(request.headers.get("mcp-protocol-version"))
        return httpx.Response(400, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32022, "message": "no"}})

    handler["fn"] = strict
    assert asyncio.run(dd.get_data_element("x")) is None
    assert seen == [MODERN] and dd._legacy_server is None
