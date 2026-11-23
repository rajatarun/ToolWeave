"""Unit and CLI tests for ToolWeave multi-step ToolPipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from toolweave.local_catalog import LanAllowlistGuard, SQLiteCatalogStore
from toolweave.models import EndpointEntry, EndpointParameter, RequestBodyField
from toolweave.pipeline import PipelineStep, ToolPipeline


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server as srv

    return srv


def _seed_catalog(store: SQLiteCatalogStore) -> None:
    entries = [
        EndpointEntry(
            api_id="orders",
            operation_id="search_orders",
            method="GET",
            path="/orders",
            base_url="http://192.168.1.50:8080",
            parameters=[
                EndpointParameter(
                    name="customer",
                    location="query",
                    required=True,
                    data_type="string",
                ),
                EndpointParameter(
                    name="limit",
                    location="query",
                    required=False,
                    data_type="integer",
                ),
            ],
        ),
        EndpointEntry(
            api_id="orders",
            operation_id="get_order_detail",
            method="GET",
            path="/orders/{orderId}",
            base_url="http://192.168.1.50:8080",
            parameters=[
                EndpointParameter(
                    name="orderId",
                    location="path",
                    required=True,
                    data_type="string",
                ),
            ],
        ),
        EndpointEntry(
            api_id="tickets",
            operation_id="create_ticket",
            method="POST",
            path="/tickets",
            base_url="http://192.168.1.50:8080",
            body_fields=[
                RequestBodyField(name="order_id", required=True, data_type="string"),
                RequestBodyField(name="priority", required=True, data_type="integer"),
                RequestBodyField(name="summary", required=False, data_type="string"),
            ],
        ),
        EndpointEntry(
            api_id="external",
            operation_id="notify_external",
            method="POST",
            path="/hooks",
            base_url="http://10.99.99.99:9000",
            body_fields=[
                RequestBodyField(name="message", required=True, data_type="string"),
            ],
        ),
    ]
    store.save_entries("orders", entries[:2])
    store.save_entries("tickets", [entries[2]])
    store.save_entries("external", [entries[3]])


def test_two_step_output_chaining_with_mocked_http_executor() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    calls: list[dict[str, Any]] = []

    def mock_http(
        method: str,
        url: str,
        *,
        query_params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        calls.append(
            {
                "method": method,
                "url": url,
                "query_params": query_params,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if url.endswith("/orders"):
            return {"status_code": 200, "body": {"id": "ORD-42", "items": [{"sku": "SKU-9"}]}}
        if url.endswith("/orders/ORD-42"):
            return {"status_code": 200, "body": {"order_id": "ORD-42", "status": "delayed"}}
        if url.endswith("/tickets"):
            return {"status_code": 201, "body": {"ticket_id": "TCK-7", "echo": payload}}
        raise AssertionError(f"Unexpected URL: {url}")

    allowlist = LanAllowlistGuard(["192.168.1.50"])
    pipeline = ToolPipeline(store, allowlist=allowlist, http_executor=mock_http)

    result = pipeline.execute(
        [
            PipelineStep(
                step_id="s1",
                tool_id="search_orders",
                arguments={"customer": "alice", "limit": 5},
            ),
            PipelineStep(
                step_id="s2",
                tool_id="get_order_detail",
                arguments={"orderId": "$steps.s1.id"},
            ),
            PipelineStep(
                step_id="s3",
                tool_id="create_ticket",
                arguments={
                    "order_id": "$steps.s2.order_id",
                    "priority": 1,
                    "summary": "Delayed order $steps.s1.id ($steps.s1.items.0.sku)",
                },
            ),
        ]
    )

    assert result.status == "completed"
    assert result.dry_run is False
    assert len(calls) == 3
    assert calls[0]["url"] == "http://192.168.1.50:8080/orders"
    assert calls[0]["query_params"] == {"customer": "alice", "limit": 5}
    assert calls[1]["url"] == "http://192.168.1.50:8080/orders/ORD-42"
    assert calls[2]["payload"] == {
        "order_id": "ORD-42",
        "priority": 1,
        "summary": "Delayed order ORD-42 (SKU-9)",
    }
    assert result.outputs["s3"]["ticket_id"] == "TCK-7"
    assert all(step.status == "ok" for step in result.steps)
    store.close()


def test_dry_run_produces_resolved_plan_with_zero_http_calls() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    http_calls = 0

    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("HTTP executor must not be invoked during dry_run")

    allowlist = LanAllowlistGuard(["192.168.1.50"])
    pipeline = ToolPipeline(store, allowlist=allowlist, http_executor=fail_if_called)

    result = pipeline.execute(
        [
            PipelineStep("s1", "search_orders", {"customer": "bob"}),
            PipelineStep("s2", "get_order_detail", {"orderId": "$steps.s1.id"}),
        ],
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert http_calls == 0
    assert len(result.steps) == 2
    assert result.steps[0].status == "dry_run"
    assert result.steps[0].url == "http://192.168.1.50:8080/orders"
    assert result.steps[1].url == "http://192.168.1.50:8080/orders/$steps.s1.id"
    store.close()


def test_allowlist_blocks_disallowed_host_mid_pipeline() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    called_urls: list[str] = []

    def mock_http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        _ = (method, kwargs)
        called_urls.append(url)
        return {"status_code": 200, "body": {"id": "ORD-99"}}

    allowlist = LanAllowlistGuard(["192.168.1.50"])
    pipeline = ToolPipeline(store, allowlist=allowlist, http_executor=mock_http)

    result = pipeline.execute(
        [
            PipelineStep("s1", "search_orders", {"customer": "carol"}),
            PipelineStep("s2", "notify_external", {"message": "Order $steps.s1.id"}),
        ]
    )

    assert result.status == "blocked"
    assert result.error is not None and "allowlist_blocked" in result.error
    assert len(called_urls) == 1
    assert result.steps[0].status == "ok"
    assert result.steps[1].status == "blocked"
    assert result.steps[1].url == "http://10.99.99.99:9000/hooks"
    store.close()


def test_missing_tool_id_and_schema_validation_halt_cleanly() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    called = False

    def mock_http(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        return {"status_code": 200, "body": {}}

    pipeline = ToolPipeline(store, http_executor=mock_http)

    # 1. Missing tool ID
    missing_res = pipeline.execute([PipelineStep("s1", "nonexistent_tool", {})])
    assert missing_res.status == "error"
    assert missing_res.error == "unknown_tool_id:nonexistent_tool"
    assert called is False

    # 2. Missing required parameter
    missing_param_res = pipeline.execute([PipelineStep("s1", "search_orders", {})])
    assert missing_param_res.status == "error"
    assert missing_param_res.error == "missing_required_parameter:customer"
    assert called is False

    # 3. Wrong type for body field (priority expects integer)
    bad_type_res = pipeline.execute(
        [
            PipelineStep(
                "s1",
                "create_ticket",
                {"order_id": "ORD-1", "priority": "high"},
            )
        ]
    )
    assert bad_type_res.status == "error"
    assert bad_type_res.error == "invalid_type_for_priority:expected_integer"
    assert called is False
    store.close()


def test_cli_dry_run_pipeline_and_server_export(
    server: Any, tmp_path: Path, capsys: Any
) -> None:
    assert server.ToolPipeline is ToolPipeline

    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Local Orders", "version": "1.0.0"},
        "servers": [{"url": "http://192.168.1.50:8080"}],
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "search_orders",
                    "parameters": [
                        {
                            "name": "customer",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/orders/{orderId}": {
                "get": {
                    "operationId": "get_order_detail",
                    "parameters": [
                        {
                            "name": "orderId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    (specs_dir / "orders.json").write_text(json.dumps(openapi_spec), encoding="utf-8")

    db_path = tmp_path / "catalog.db"
    pipeline_file = tmp_path / "pipeline.json"
    pipeline_file.write_text(
        json.dumps(
            {
                "allowed_hosts": ["192.168.1.50"],
                "steps": [
                    {
                        "step_id": "s1",
                        "tool_id": "search_orders",
                        "arguments": {"customer": "alice"},
                    },
                    {
                        "step_id": "s2",
                        "tool_id": "get_order_detail",
                        "arguments": {"orderId": "$steps.s1.id"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = server.main(
        [
            "--db",
            str(db_path),
            "--specs-dir",
            str(specs_dir),
            "--dry-run-pipeline",
            str(pipeline_file),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["status"] == "dry_run"
    assert output["dry_run"] is True
    assert len(output["steps"]) == 2
    assert output["steps"][0]["url"] == "http://192.168.1.50:8080/orders"

    # Disallowed host should exit non-zero
    blocked_file = tmp_path / "blocked.json"
    blocked_file.write_text(
        json.dumps(
            {
                "allowed_hosts": ["192.168.1.99"],
                "steps": [
                    {
                        "step_id": "s1",
                        "tool_id": "search_orders",
                        "arguments": {"customer": "alice"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rc_blocked = server.main(
        [
            "--db",
            str(db_path),
            "--dry-run-pipeline",
            str(blocked_file),
        ]
    )
    assert rc_blocked == 1
    blocked_out = json.loads(capsys.readouterr().out)
    assert blocked_out["status"] == "blocked"
