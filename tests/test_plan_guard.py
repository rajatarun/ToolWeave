"""post_tool's plan is a tool argument: it must be bound to the catalog and sent only outward.

Each refusal below is a request the Lambda would have made from inside the
VPC, with the response handed back to the caller, before plan_guard existed.
"""

from __future__ import annotations

import pytest

from toolweave import plan_guard
from toolweave.models import EndpointEntry, EndpointParameter, PreToolResponse

BASE = "https://api.example.com/v1"


def _entry(method="GET", path="/orders/{orderId}", **kw) -> EndpointEntry:
    params = kw.pop(
        "parameters",
        [
            EndpointParameter(name="orderId", location="path", required=True, data_type="string"),
            EndpointParameter(name="expand", location="query", required=False, data_type="string"),
            EndpointParameter(
                name="X-Request-Id", location="header", required=False, data_type="string"
            ),
        ],
    )
    return EndpointEntry(
        method=method, path=path, base_url=BASE, operation_id=f"{method}{path}", parameters=params, **kw
    )


CATALOG = [_entry(), _entry("POST", "/orders", parameters=[])]


def _plan(**kw) -> PreToolResponse:
    data = {
        "session_id": "s",
        "status": "ready",
        "method": "GET",
        "path": "/orders/ORD-1",
        "base_url": BASE,
    }
    data.update(kw)
    return PreToolResponse.model_validate(data)


def test_a_cataloged_plan_binds() -> None:
    bound = plan_guard.bind(
        _plan(query_params={"expand": "items"}, headers={"X-Request-Id": "r1"}), CATALOG
    )
    assert bound.base_url == BASE and bound.path == "/orders/ORD-1"
    assert bound.headers == {"X-Request-Id": "r1"}


@pytest.mark.parametrize(
    ("plan", "why"),
    [
        ({"base_url": "http://10.0.0.5:7687"}, "VPC host: Memgraph"),
        ({"base_url": "http://127.0.0.1:9001"}, "Lambda runtime API"),
        ({"base_url": "https://api.example.com.evil.test/v1"}, "look-alike host"),
        ({"path": "/admin/users"}, "uncataloged path"),
        ({"path": "/orders/../admin"}, "traversal"),
        ({"path": "/orders/%2e%2e%2fadmin"}, "encoded traversal"),
        ({"path": "/orders/%252e%252e%252fadmin"}, "double-encoded traversal"),
        ({"path": "/orders/a/b"}, "extra segment"),
        ({"path": "http://169.254.169.254/latest"}, "absolute URL as path"),
        ({"method": "DELETE"}, "method the spec does not have"),
        ({"query_params": {"debug": "1"}}, "undeclared query parameter"),
        ({"headers": {"Authorization": "Bearer stolen"}}, "authority header"),
        ({"headers": {"Host": "internal"}}, "host header"),
        ({"headers": {"X-Forwarded-For": "1.2.3.4"}}, "forwarding header"),
        ({"headers": {"X-Other": "v"}}, "undeclared header"),
        ({"headers": {"X-Request-Id": "a\r\nInjected: 1"}}, "header injection"),
        ({"body": {"x": 1}}, "GET with a body"),
    ],
)
def test_plans_off_the_catalog_are_rejected(plan, why) -> None:
    with pytest.raises(plan_guard.PlanRejected):
        plan_guard.bind(_plan(**plan), CATALOG)


def _resolver(addresses):
    def resolve(host, port):
        return [(2, 1, 6, "", (a, port)) for a in addresses]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://0.0.0.0/",
        "http://localhost/",
        "http://memgraph.internal/",
        "ftp://example.com/",
        "https://user:pw@example.com/",
    ],
)
def test_egress_refuses_non_public_destinations(url, monkeypatch) -> None:
    monkeypatch.delenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, raising=False)
    with pytest.raises(plan_guard.EgressDenied):
        plan_guard.check_egress(url, resolver=_resolver(["93.184.216.34"]))


def test_egress_refuses_a_name_that_resolves_inside(monkeypatch) -> None:
    monkeypatch.delenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, raising=False)
    with pytest.raises(plan_guard.EgressDenied):
        plan_guard.check_egress(
            "https://api.example.com/", resolver=_resolver(["93.184.216.34", "10.0.0.7"])
        )


def test_egress_allows_public_and_the_dev_override(monkeypatch) -> None:
    monkeypatch.delenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, raising=False)
    plan_guard.check_egress("https://api.example.com/", resolver=_resolver(["93.184.216.34"]))
    monkeypatch.setenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, "true")
    plan_guard.check_egress("http://127.0.0.1:8080/")


async def test_executor_refuses_before_sending(monkeypatch) -> None:
    from toolweave import executor

    monkeypatch.delenv(plan_guard.ENV_ALLOW_PRIVATE_EGRESS, raising=False)
    sent = []

    class Boom:
        def __init__(self, *a, **k):
            sent.append(1)

    monkeypatch.setattr(executor.httpx, "AsyncClient", Boom)
    result = await executor.execute_get("http://169.254.169.254", "/latest/meta-data/")
    assert result.status_code == 0 and "refused" in (result.error or "")
    assert sent == []


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server

    monkeypatch.setattr(server, "_catalog", list(CATALOG))
    return server


async def test_post_tool_rejects_a_forged_plan_without_a_request(server, monkeypatch) -> None:
    calls = []

    async def fake_get(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(server.executor, "execute_get", fake_get)
    forged = _plan(base_url="http://10.0.0.5:7687", path="/").model_dump()
    result = await server.post_tool(forged)
    assert "Plan rejected" in result["error"]
    assert calls == []


async def test_post_tool_executes_a_bound_plan_with_catalog_values(server, monkeypatch) -> None:
    calls = []

    async def fake_get(**kwargs):
        calls.append(kwargs)
        from toolweave.models import ImmediateExecutionResult

        return ImmediateExecutionResult(method="GET", url="u", status_code=200)

    monkeypatch.setattr(server.executor, "execute_get", fake_get)
    result = await server.post_tool(_plan(base_url=BASE + "/").model_dump())
    assert result["status_code"] == 200
    assert calls[0]["base_url"] == BASE


async def test_post_tool_never_runs_a_write_immediately(server, monkeypatch) -> None:
    gets = []

    async def fake_get(**kwargs):
        gets.append(kwargs)

    async def fake_propose(**kwargs):
        return {"status": "blocked", "proposal_id": "p1"}

    monkeypatch.setattr(server.executor, "execute_get", fake_get)
    monkeypatch.setattr(server.observatory, "propose", fake_propose)
    plan = _plan(method="POST", path="/orders", body={"a": 1}, execution_type="immediate")
    result = await server.post_tool(plan.model_dump())
    assert gets == []
    assert result["execution_type"] == "proposal"
