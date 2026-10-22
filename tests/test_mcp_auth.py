"""Bearer auth on the deployed /mcp route.

Missing header, wrong token, and a missing secret all refuse the call.
A correct token is the only case that reaches the MCP app. Other routes
are not checked, so a catalog load or a non-MCP path still proceeds.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from toolweave import mcp_auth

TOKEN = "expected-token-value"
SECRET_NAME = "toolweave/mcp-auth-token"
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "template.yaml"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
README_PATH = REPO_ROOT / "README.md"


class _FakeSecrets:
    def __init__(self, secret=None, error=None):
        self.secret = secret
        self.error = error
        self.calls = 0
        self.secret_ids = []

    def get_secret_value(self, SecretId):
        self.calls += 1
        self.secret_ids.append(SecretId)
        if self.error is not None:
            raise self.error
        return {"SecretString": self.secret}


class _RecordingMangum:
    def __init__(self, app, lifespan="auto"):
        self.app = app
        self.lifespan = lifespan
        self.calls = []

    def __call__(self, event, context):
        self.calls.append((event, context))
        return {"statusCode": 200, "body": "{\"ok\":true}"}


def _event(method="POST", path="/mcp", token=TOKEN, body=None, headers=None):
    hdrs = {}
    if token is not None:
        hdrs["authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    event = {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": hdrs,
    }
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    return event


def _body(response):
    return json.loads(response["body"])


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    mcp_auth.clear_cache()
    monkeypatch.delenv(mcp_auth.SECRET_NAME_ENV, raising=False)
    yield
    mcp_auth.clear_cache()


@pytest.fixture
def secrets(monkeypatch):
    fake = _FakeSecrets(TOKEN)
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, SECRET_NAME)
    monkeypatch.setattr(mcp_auth, "_secrets_client", lambda: fake)
    return fake


@pytest.fixture
def mcp_server(monkeypatch):
    """Import the Lambda entrypoint without a real observatory secret."""
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server

    return server


def _install_handler_doubles(monkeypatch, server, secrets_fake):
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, SECRET_NAME)
    monkeypatch.setattr(mcp_auth, "_secrets_client", lambda: secrets_fake)
    catalog_calls = {"n": 0}

    def _load():
        catalog_calls["n"] += 1
        return []

    monkeypatch.setattr(server.dynamodb_client, "load_full_catalog", _load)
    server._catalog = []
    # lambda_handler builds Mangum(app)(event, context). One instance records
    # whether the MCP app was actually entered.
    mangum = _RecordingMangum(None)

    def _factory(app, lifespan="auto"):
        mangum.app = app
        mangum.lifespan = lifespan
        return mangum

    monkeypatch.setattr(server, "Mangum", _factory)
    return {"mangum": mangum, "catalog": catalog_calls}


def test_token_compare_uses_fixed_length_digests(monkeypatch):
    seen = {}

    def _fake_compare(left, right):
        seen["left"] = left
        seen["right"] = right
        return True

    monkeypatch.setattr(mcp_auth.hmac, "compare_digest", _fake_compare)
    assert mcp_auth.tokens_equal("short", "much-longer-value") is True
    assert isinstance(seen["left"], (bytes, bytearray))
    assert len(seen["left"]) == len(seen["right"]) == 32
    assert seen["left"] == hashlib.sha256(b"short").digest()
    assert seen["right"] == hashlib.sha256(b"much-longer-value").digest()


def test_token_compare_rejects_mismatches():
    assert mcp_auth.tokens_equal("abc", "abc") is True
    assert mcp_auth.tokens_equal("abc", "abd") is False
    assert mcp_auth.tokens_equal("abc", "abcd") is False
    assert mcp_auth.tokens_equal("", "abc") is False


def test_credentials_ok_still_compares_when_a_side_is_missing(monkeypatch):
    seen = {"n": 0}

    def _fake_equal(presented, expected):
        seen["n"] += 1
        seen["presented"] = presented
        seen["expected"] = expected
        return True

    monkeypatch.setattr(mcp_auth, "tokens_equal", _fake_equal)
    assert mcp_auth.credentials_ok(None, None) is False
    assert seen == {"n": 1, "presented": "", "expected": ""}
    assert mcp_auth.credentials_ok("presented", None) is False
    assert mcp_auth.credentials_ok(None, "expected") is False


def test_missing_header_is_401(secrets, mcp_server, monkeypatch):
    doubles = _install_handler_doubles(monkeypatch, mcp_server, secrets)
    event = _event(
        token=None,
        body={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
    )
    response = mcp_server.lambda_handler(event, None)

    assert response["statusCode"] == 401
    assert response["headers"]["Content-Type"] == "application/json"
    assert "Bearer" in response["headers"]["WWW-Authenticate"]
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 4
    assert payload["error"] == {"code": -32001, "message": "Unauthorized"}
    assert "result" not in payload
    assert TOKEN not in response["body"]
    assert doubles["mangum"].calls == []
    assert doubles["catalog"]["n"] == 0


def test_wrong_token_is_the_same_401(secrets, mcp_server, monkeypatch):
    doubles = _install_handler_doubles(monkeypatch, mcp_server, secrets)
    event = _event(
        token="submitted-token-value",
        body={"jsonrpc": "2.0", "id": "call-1", "method": "initialize"},
    )
    response = mcp_server.lambda_handler(event, None)

    assert response["statusCode"] == 401
    payload = _body(response)
    assert payload["id"] == "call-1"
    assert payload["error"] == {"code": -32001, "message": "Unauthorized"}
    assert TOKEN not in response["body"]
    assert "submitted-token-value" not in response["body"]
    assert doubles["mangum"].calls == []


@pytest.mark.parametrize(
    "header",
    ["Token not-a-bearer", "Bearer", "Basic expected-token-value"],
)
def test_malformed_authorization_header_is_401(secrets, mcp_server, monkeypatch, header):
    doubles = _install_handler_doubles(monkeypatch, mcp_server, secrets)
    event = _event(token=None, headers={"Authorization": header})
    response = mcp_server.lambda_handler(event, None)
    assert response["statusCode"] == 401
    assert _body(response)["error"]["code"] == -32001
    assert doubles["mangum"].calls == []


def test_correct_token_reaches_the_mcp_app(secrets, mcp_server, monkeypatch):
    doubles = _install_handler_doubles(monkeypatch, mcp_server, secrets)
    event = _event(
        token=TOKEN,
        body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    response = mcp_server.lambda_handler(event, {"aws_request_id": "req"})

    assert response["statusCode"] == 200
    assert doubles["mangum"].calls
    seen_event, seen_context = doubles["mangum"].calls[0]
    assert seen_event is event
    assert seen_context == {"aws_request_id": "req"}
    assert secrets.secret_ids == [SECRET_NAME]


def test_correct_token_is_cached_for_the_execution_environment(secrets, monkeypatch):
    monkeypatch.setattr(mcp_auth, "_secrets_client", lambda: secrets)
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, SECRET_NAME)
    assert mcp_auth.read_configured_token() == TOKEN
    assert mcp_auth.read_configured_token() == TOKEN
    assert secrets.calls == 1


def test_missing_secret_fails_closed(mcp_server, monkeypatch):
    """A lookup error refuses the call even when a bearer token is presented."""
    fake = _FakeSecrets(error=RuntimeError("ResourceNotFoundException: secret name leaked"))
    doubles = _install_handler_doubles(monkeypatch, mcp_server, fake)
    event = _event(
        token=TOKEN,
        body={"jsonrpc": "2.0", "id": 9, "method": "tools/call"},
    )
    response = mcp_server.lambda_handler(event, None)

    assert response["statusCode"] == 401
    payload = _body(response)
    assert payload["jsonrpc"] == "2.0"
    assert payload["error"] == {"code": -32001, "message": "Unauthorized"}
    assert "leaked" not in response["body"]
    assert SECRET_NAME not in response["body"]
    assert doubles["mangum"].calls == []
    assert doubles["catalog"]["n"] == 0


def test_empty_secret_fails_closed(mcp_server, monkeypatch):
    fake = _FakeSecrets(secret="   ")
    doubles = _install_handler_doubles(monkeypatch, mcp_server, fake)
    response = mcp_server.lambda_handler(_event(token=TOKEN), None)
    assert response["statusCode"] == 401
    assert doubles["mangum"].calls == []
    # An empty read is not cached, so a later value can succeed without a cold start.
    fake.secret = TOKEN
    response = mcp_server.lambda_handler(_event(token=TOKEN), None)
    assert response["statusCode"] == 200
    assert fake.calls == 2


def test_unset_secret_name_fails_closed_without_calling_secrets_manager(
    mcp_server, monkeypatch
):
    fake = _FakeSecrets(TOKEN)
    doubles = _install_handler_doubles(monkeypatch, mcp_server, fake)
    monkeypatch.delenv(mcp_auth.SECRET_NAME_ENV, raising=False)
    response = mcp_server.lambda_handler(_event(token=TOKEN), None)
    assert response["statusCode"] == 401
    assert fake.calls == 0
    assert doubles["mangum"].calls == []


def test_get_mcp_without_a_token_is_401(secrets, mcp_server, monkeypatch):
    doubles = _install_handler_doubles(monkeypatch, mcp_server, secrets)
    response = mcp_server.lambda_handler(_event(method="GET", token=None), None)
    assert response["statusCode"] == 401
    assert _body(response)["id"] is None
    assert doubles["mangum"].calls == []


def test_other_routes_are_not_authenticated(mcp_server, monkeypatch):
    fake = _FakeSecrets(error=AssertionError("non-mcp route must not read the secret"))
    doubles = _install_handler_doubles(monkeypatch, mcp_server, fake)
    monkeypatch.delenv(mcp_auth.SECRET_NAME_ENV, raising=False)
    event = _event(method="GET", path="/", token=None)
    response = mcp_server.lambda_handler(event, None)
    assert response["statusCode"] == 200
    assert doubles["mangum"].calls
    assert fake.calls == 0


def test_whitespace_around_the_secret_is_stripped(monkeypatch):
    fake = _FakeSecrets("  from-secret \n")
    monkeypatch.setenv(mcp_auth.SECRET_NAME_ENV, SECRET_NAME)
    assert mcp_auth.read_configured_token(secrets_client=fake) == "from-secret"
    assert fake.secret_ids == [SECRET_NAME]


def test_template_names_the_secret_and_rejects_a_blank_name():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "McpAuthTokenSecretName:" in text
    assert "Default: toolweave/mcp-auth-token" in text
    assert "MinLength: 1" in text
    assert 'AllowedPattern: "[A-Za-z0-9/_+=.@-]+"' in text
    assert "MCP_AUTH_TOKEN_SECRET_NAME: !Ref McpAuthTokenSecretName" in text
    assert "secretsmanager:GetSecretValue" in text
    assert "secret:${McpAuthTokenSecretName}-*" in text
    # Each secrets grant names its secret (or prefix). A wildcard resource
    # would let the function read every secret in the account.
    resources = [
        part.split("Resource:", 1)[1].split("\n", 1)[0].strip()
        for part in text.split("secretsmanager:GetSecretValue")[1:]
    ]
    assert any("McpAuthTokenSecretName" in r for r in resources)
    assert all(r not in ('"*"', "'*'", "*") and ":secret:" in r for r in resources), resources
    assert "Bearer " not in text
    assert re.search(r"SecretString:\s*\S", text) is None


def test_deploy_workflow_passes_a_non_blank_secret_name():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert (
        "MCP_AUTH_TOKEN_SECRET_NAME: ${{ vars.MCP_AUTH_TOKEN_SECRET_NAME || 'toolweave/mcp-auth-token' }}"
        in text
    )
    assert "McpAuthTokenSecretName=${SECRET_NAME}" in text
    assert "McpAuthTokenSecretName is blank" in text
    assert 'McpAuthTokenSecretName=""' not in text
    # An override that expands an empty variable would wipe the template default.
    assert "McpAuthTokenSecretName=${{ vars.MCP_AUTH_TOKEN_SECRET_NAME }}" not in text
    # The workflow carries the name, never a token value.
    assert "OBSERVATORY_SECRET_KEY" in text
    assert "openssl" not in text
    assert "secret-string" not in text.lower()


def test_readme_documents_the_secret_and_the_header_without_a_real_endpoint():
    text = README_PATH.read_text(encoding="utf-8")
    assert "aws secretsmanager create-secret" in text
    assert "--name toolweave/mcp-auth-token" in text
    assert "--secret-string" in text
    assert "Authorization: Bearer <token>" in text
    assert "mcp-remote" in text
    section = text.split("## MCP authentication", 1)[1].split("\n## ", 1)[0]
    assert "<api-id>" in section
    assert not re.search(r"\b\d{12}\b", section)
    assert "lambda-url" not in section
    assert "amazonaws.com/" not in section.replace(
        "https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp", ""
    )


def test_compare_digest_is_the_primitive():
    """Guard against a refactor that compares the raw strings with ==."""
    source = (REPO_ROOT / "src" / "toolweave" / "mcp_auth.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest" in source
    assert "hashlib.sha256" in source
    # The equality helper must not short-circuit on the secret itself.
    helper = source.split("def tokens_equal", 1)[1].split("def ", 1)[0]
    assert "==" not in helper
