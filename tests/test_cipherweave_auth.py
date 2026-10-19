"""CipherWeave access tokens on /mcp, and the static token behind ACCEPT_LEGACY_TOKEN.

Tokens come from cipherweave_verify.testing.LocalIssuer: the issuer's exact
format (CipherWeave's tests hold the two together), a key that exists only in
this process, and its JWKS served in memory instead of over HTTPS.

The health tool is called through the real lambda_handler, Mangum and FastMCP
app — only the DynamoDB catalog load is replaced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cipherweave_verify import jwks as cw_jwks
from cipherweave_verify import reset_default_verifier
from cipherweave_verify.testing import LocalIssuer

from toolweave import mcp_auth

LEGACY = "legacy-static-token-value"
REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeSecrets:
    def __init__(self, secret=LEGACY):
        self.secret = secret
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        return {"SecretString": self.secret}


@pytest.fixture
def issuer(monkeypatch):
    issuer = LocalIssuer()
    monkeypatch.setenv("CIPHERWEAVE_JWKS_URL", "https://cipherweave.test/prod/.well-known/jwks.json")
    monkeypatch.setenv("CIPHERWEAVE_ISSUER", "urn:cipherweave:prod")
    monkeypatch.delenv("ACCEPT_LEGACY_TOKEN", raising=False)
    monkeypatch.setattr(cw_jwks, "http_fetcher", issuer.fetcher)
    reset_default_verifier()
    mcp_auth.clear_cache()
    yield issuer
    reset_default_verifier()
    mcp_auth.clear_cache()


@pytest.fixture
def secrets(monkeypatch):
    fake = _FakeSecrets()
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, "toolweave/mcp-auth-token")
    monkeypatch.setattr(mcp_auth, "_secrets_client", lambda: fake)
    return fake


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server

    monkeypatch.setattr(server.dynamodb_client, "load_full_catalog", list)
    monkeypatch.setattr(server, "_catalog", [])
    return server


def _health(server, token):
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "host": "toolweave.test",
    }
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    event = {
        "version": "2.0",
        "routeKey": "POST /mcp",
        "rawPath": "/mcp",
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "http": {"method": "POST", "path": "/mcp", "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1"},
            "stage": "$default",
            "domainName": "toolweave.test",
        },
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "health", "arguments": {}}}
        ),
        "isBase64Encoded": False,
    }
    return server.lambda_handler(event, None)


def _healthy(response) -> bool:
    if response["statusCode"] != 200:
        return False
    text = response["body"]
    if "data:" in text:
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    result = json.loads(text)["result"]
    payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    return result.get("isError") is not True and payload["status"] == "ok"


def test_cipherweave_token_calls_the_health_tool(issuer, secrets, server):
    assert _healthy(_health(server, issuer.mint(audience="toolweave")))
    assert secrets.calls == 0  # a valid CipherWeave token never reads the static secret


@pytest.mark.parametrize("audience", ["deviceweave", "contextweave", "modelweave"])
def test_token_for_another_weave_is_401(issuer, secrets, server, audience):
    response = _health(server, issuer.mint(audience=audience))
    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == mcp_auth.UNAUTHORIZED_CODE


def test_expired_forged_and_unsigned_tokens_are_401(issuer, secrets, server):
    forged = LocalIssuer().mint(audience="toolweave", header_overrides={"kid": issuer.kid})
    for token in (
        issuer.mint(audience="toolweave", now=1_000_000_000),
        forged,
        issuer.mint(audience="toolweave", header_overrides={"alg": "none"}),
    ):
        assert _health(server, token)["statusCode"] == 401


def test_static_token_still_works_by_default(issuer, secrets, server):
    assert _healthy(_health(server, LEGACY))


def test_static_token_is_refused_once_the_flag_is_off(issuer, secrets, server, monkeypatch):
    monkeypatch.setenv("ACCEPT_LEGACY_TOKEN", "false")
    assert _health(server, LEGACY)["statusCode"] == 401
    assert _healthy(_health(server, issuer.mint(audience="toolweave")))
    assert secrets.calls == 0  # with the flag off the static secret is never read


def test_without_a_jwks_url_only_the_static_token_works(issuer, secrets, server, monkeypatch):
    monkeypatch.delenv("CIPHERWEAVE_JWKS_URL")
    reset_default_verifier()
    assert _health(server, issuer.mint(audience="toolweave"))["statusCode"] == 401
    assert _healthy(_health(server, LEGACY))


def test_rejection_never_echoes_the_token(issuer, secrets, server):
    token = issuer.mint(audience="modelweave")
    response = _health(server, token)
    for part in token.split("."):
        assert part not in response["body"]


def test_template_and_workflow_wire_the_verifier():
    template = (REPO_ROOT / "template.yaml").read_text()
    for needle in ("CipherWeaveJwksUrl", "CIPHERWEAVE_JWKS_URL", "AcceptLegacyToken", "ACCEPT_LEGACY_TOKEN"):
        assert needle in template
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "pip install --no-deps --target src/ -r requirements-cipherweave.txt" in workflow
    assert "IssuerJwksUrl" in workflow
