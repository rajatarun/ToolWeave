"""Per-API credentials replace the hardcoded `Bearer 123`, and egress connects to the checked address."""

from __future__ import annotations

import json

import httpx
import pytest

from toolweave import api_credentials, executor, plan_guard


class _Secrets:
    def __init__(self, values: dict[str, object] | None = None, error: str | None = None) -> None:
        self.values = values or {}
        self.error = error
        self.calls = 0

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.error:
            raise type("E", (Exception,), {"response": {"Error": {"Code": self.error}}})()
        if SecretId not in self.values:
            raise type("E", (Exception,), {"response": {"Error": {"Code": "ResourceNotFoundException"}}})()
        return {"SecretString": json.dumps(self.values[SecretId])}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    api_credentials.clear_cache()
    monkeypatch.delenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, raising=False)
    yield
    api_credentials.clear_cache()


def _secrets(monkeypatch, **kw) -> _Secrets:
    fake = _Secrets(**kw)
    monkeypatch.setattr(api_credentials, "_client", fake)
    return fake


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_no_secret_means_no_credential_and_no_placeholder(monkeypatch) -> None:
    fake = _secrets(monkeypatch)
    assert api_credentials.headers_for("orders", "https://api.example.com/v1/x") == {}
    assert api_credentials.headers_for("orders", "https://api.example.com/v1/y") == {}
    assert fake.calls == 1  # the miss is cached too
    from pathlib import Path

    assert "Bearer 123" not in Path(executor.__file__).read_text()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({"type": "bearer", "token": "tkn"}, {"Authorization": "Bearer tkn"}),
        ({"type": "header", "name": "X-Api-Key", "value": "k"}, {"X-Api-Key": "k"}),
        ({"type": "basic", "username": "u", "password": "p"}, {"Authorization": "Basic dTpw"}),
    ],
)
def test_stored_credential_is_sent_to_its_host(monkeypatch, stored, expected) -> None:
    _secrets(monkeypatch, values={"toolweave/api/orders": {"host": "api.example.com", **stored}})
    assert api_credentials.headers_for("orders", "https://API.example.com/v1/x") == expected


@pytest.mark.parametrize(
    "stored",
    [
        {"type": "bearer", "token": "t"},  # no host: would go anywhere
        {"host": "other.example.com", "type": "bearer", "token": "t"},  # catalog moved the API
        {"host": "api.example.com", "type": "magic"},
        {"host": "api.example.com", "type": "header", "name": "Host", "value": "x"},
        {"host": "api.example.com", "type": "header", "name": "X-K", "value": "a\r\nInjected: 1"},
    ],
)
def test_unusable_credentials_refuse_the_call(monkeypatch, stored) -> None:
    _secrets(monkeypatch, values={"toolweave/api/orders": stored})
    with pytest.raises(api_credentials.CredentialUnavailable):
        api_credentials.headers_for("orders", "https://api.example.com/v1/x")


def test_unreadable_secret_refuses_rather_than_sending_anonymously(monkeypatch) -> None:
    _secrets(monkeypatch, error="AccessDeniedException")
    with pytest.raises(api_credentials.CredentialUnavailable):
        api_credentials.headers_for("orders", "https://api.example.com/v1/x")


def test_malformed_api_id_never_reaches_secrets_manager(monkeypatch) -> None:
    fake = _secrets(monkeypatch)
    assert api_credentials.headers_for("../other", "https://api.example.com/") == {}
    assert fake.calls == 0


# ---------------------------------------------------------------------------
# Address pinning
# ---------------------------------------------------------------------------
def test_resolve_egress_returns_the_checked_address() -> None:
    def resolver(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", port)), (10, 1, 6, "", ("2606:2800::1", port, 0, 0))]

    assert plan_guard.resolve_egress("https://api.example.com/x", resolver=resolver) == "93.184.216.34"


def test_pin_keeps_host_and_sni() -> None:
    url, headers, ext = executor._pin("https://api.example.com:8443/v1/x?q=1", "93.184.216.34")
    assert url == "https://93.184.216.34:8443/v1/x?q=1"
    assert headers == {"Host": "api.example.com:8443"}
    assert ext == {"sni_hostname": "api.example.com"}
    url, headers, ext = executor._pin("http://api.example.com/x", "2606:2800::1")
    assert url == "http://[2606:2800::1]/x" and headers == {"Host": "api.example.com"} and ext == {}
    assert executor._pin("https://93.184.216.34/x", "93.184.216.34") == ("https://93.184.216.34/x", {}, {})


async def test_executor_connects_to_the_pinned_address_with_the_credential(monkeypatch) -> None:
    _secrets(monkeypatch, values={"toolweave/api/orders": {"host": "api.example.com", "type": "bearer", "token": "tkn"}})
    monkeypatch.setattr(executor, "resolve_egress", lambda url: "93.184.216.34")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.headers["host"]
        seen["auth"] = request.headers.get("authorization")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        executor.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    result = await executor.execute_get(
        "https://api.example.com/v1", "/orders/1", headers={"Authorization": "Bearer from-plan"},
        api_id="orders",
    )
    assert result.status_code == 200 and result.url == "https://api.example.com/v1/orders/1"
    assert seen == {
        "url": "https://93.184.216.34/v1/orders/1",
        "host": "api.example.com",
        "auth": "Bearer tkn",  # the stored credential wins over anything in the plan
        "sni": "api.example.com",
    }


async def test_executor_refuses_when_the_credential_is_broken(monkeypatch) -> None:
    _secrets(monkeypatch, error="AccessDeniedException")
    monkeypatch.setattr(executor, "resolve_egress", lambda url: "93.184.216.34")
    result = await executor.execute_get("https://api.example.com", "/x", api_id="orders")
    assert result.status_code == 0 and "credential" in result.error


def test_commit_uses_the_proposed_apis_credential() -> None:
    from pathlib import Path

    src = (Path(executor.__file__).parent / "server.py").read_text()
    assert '"api_id": bound.entry.api_id' in src
    assert 'api_id=str(tool_args.get("api_id") or "")' in src
