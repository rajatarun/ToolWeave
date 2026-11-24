"""Unit and CLI tests for ToolPipeline retries/backoff, telemetry_sink, and OpenAPI spec linter."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from toolweave.local_catalog import LanAllowlistGuard, SQLiteCatalogStore
from toolweave.models import EndpointEntry, EndpointParameter
from toolweave.pipeline import PipelineStep, ToolPipeline
from toolweave.spec_linter import lint_openapi_spec

_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_ALLOWED_TELEMETRY_KEYS = {
    "schema",
    "event_id",
    "source_repo",
    "event_type",
    "status",
    "latency_ms",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "trace_id",
    "timestamp",
    "attributes",
    "span_id",
    "parent_span_id",
    "error_message",
}


def _seed_catalog(store: SQLiteCatalogStore) -> None:
    entries = [
        EndpointEntry(
            api_id="orders",
            operation_id="get_order",
            method="GET",
            path="/orders/{orderId}",
            base_url="http://192.168.1.50:8080",
            parameters=[
                EndpointParameter(
                    name="orderId",
                    location="path",
                    required=True,
                    data_type="string",
                )
            ],
        ),
        EndpointEntry(
            api_id="orders",
            operation_id="list_orders",
            method="GET",
            path="/orders",
            base_url="http://192.168.1.50:8080",
        ),
    ]
    store.save_entries("orders", entries)


def _assert_valid_weave_telemetry_v1(event: dict[str, Any]) -> None:
    assert set(event.keys()).issubset(_ALLOWED_TELEMETRY_KEYS)
    assert event["schema"] == "weave-telemetry-v1"
    assert isinstance(event["event_id"], str) and len(event["event_id"]) > 0
    assert event["source_repo"] == "ToolWeave"
    assert isinstance(event["event_type"], str) and len(event["event_type"]) > 0
    assert event["status"] in {"ok", "error", "timeout", "aborted", "pending"}
    assert isinstance(event["latency_ms"], (int, float)) and event["latency_ms"] >= 0
    assert isinstance(event["tokens_in"], int) and event["tokens_in"] >= 0
    assert isinstance(event["tokens_out"], int) and event["tokens_out"] >= 0
    assert isinstance(event["cost_usd"], (int, float)) and event["cost_usd"] >= 0
    assert isinstance(event["trace_id"], str) and len(event["trace_id"]) > 0
    assert isinstance(event["timestamp"], str) and _RFC3339_RE.match(event["timestamp"])
    assert isinstance(event["attributes"], dict)


def _clean_openapi_spec() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Orders API", "version": "1.0.0"},
        "servers": [{"url": "https://api.orders.example.com/v1"}],
        "paths": {
            "/orders": {
                "get": {
                    "operationId": "listOrders",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": {
                    "operationId": "createOrder",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"item": {"type": "string"}},
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            }
        },
    }


def test_flaky_step_succeeds_on_retry_2_with_backoff_and_telemetry() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    call_count = 0
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []

    def flaky_http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        _ = (method, url, kwargs)
        call_count += 1
        if call_count == 1:
            raise ConnectionError("transient reset")
        if call_count == 2:
            raise TimeoutError("transient timeout")
        return {"status_code": 200, "body": {"id": "ORD-100", "status": "shipped"}}

    pipeline = ToolPipeline(
        store,
        http_executor=flaky_http,
        telemetry_sink=events.append,
        sleep_fn=sleeps.append,
    )

    result = pipeline.execute(
        [
            PipelineStep(
                step_id="s1",
                tool_id="get_order",
                arguments={"orderId": "ORD-100"},
                max_retries=2,
                backoff_ms=50,
            )
        ],
        trace_id="trace-retry-1",
    )

    assert result.status == "completed"
    assert call_count == 3
    assert len(result.steps) == 1
    assert result.steps[0].status == "ok"
    assert result.steps[0].attempts == 3
    assert result.steps[0].to_dict()["attempts"] == 3
    assert result.outputs["s1"] == {"id": "ORD-100", "status": "shipped"}
    # Exponential backoff: 50ms -> 0.05s, 100ms -> 0.1s
    assert sleeps == [0.05, 0.1]

    assert len(events) == 1
    _assert_valid_weave_telemetry_v1(events[0])
    assert events[0]["status"] == "ok"
    assert events[0]["trace_id"] == "trace-retry-1"
    assert events[0]["attributes"]["step_id"] == "s1"
    assert events[0]["attributes"]["tool_id"] == "get_order"
    assert events[0]["attributes"]["attempts"] == 3
    store.close()


def test_exhausted_retries_fail_cleanly_and_emit_telemetry() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    call_count = 0
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []

    def always_timeout(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        _ = (method, url, kwargs)
        call_count += 1
        raise TimeoutError("upstream timed out")

    pipeline = ToolPipeline(
        store,
        http_executor=always_timeout,
        telemetry_sink=events.append,
        sleep_fn=sleeps.append,
    )

    result = pipeline.execute(
        [
            PipelineStep(
                step_id="s1",
                tool_id="list_orders",
                max_retries=2,
                backoff_ms=25,
            )
        ],
        trace_id="trace-exhausted",
    )

    assert result.status == "error"
    assert result.error == "http_execution_error:upstream timed out"
    assert call_count == 3
    assert result.steps[0].status == "error"
    assert result.steps[0].attempts == 3
    assert sleeps == [0.025, 0.05]

    assert len(events) == 1
    _assert_valid_weave_telemetry_v1(events[0])
    assert events[0]["status"] == "timeout"
    assert events[0]["error_message"] == "http_execution_error:upstream timed out"
    assert events[0]["attributes"]["attempts"] == 3
    store.close()


def test_non_retryable_exception_fails_immediately_without_retry() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)

    call_count = 0
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []

    def bad_value(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        _ = (method, url, kwargs)
        call_count += 1
        raise ValueError("malformed payload")

    pipeline = ToolPipeline(
        store,
        http_executor=bad_value,
        telemetry_sink=events.append,
        sleep_fn=sleeps.append,
    )

    result = pipeline.execute(
        [
            PipelineStep(
                step_id="s1",
                tool_id="list_orders",
                max_retries=3,
                backoff_ms=100,
                retry_on_exceptions=(TimeoutError, ConnectionError),
            )
        ]
    )

    assert result.status == "error"
    assert call_count == 1
    assert sleeps == []
    assert result.steps[0].attempts == 1
    assert len(events) == 1
    _assert_valid_weave_telemetry_v1(events[0])
    assert events[0]["status"] == "error"
    store.close()


def test_telemetry_sink_emits_for_multi_step_and_blocked_step() -> None:
    store = SQLiteCatalogStore(":memory:")
    _seed_catalog(store)
    events: list[dict[str, Any]] = []

    pipeline = ToolPipeline(
        store,
        allowlist=LanAllowlistGuard(["10.0.0.1"]),
        telemetry_sink=events.append,
    )
    result = pipeline.execute(
        [PipelineStep(step_id="s1", tool_id="list_orders")],
        trace_id="trace-blocked",
    )
    assert result.status == "blocked"
    assert len(events) == 1
    _assert_valid_weave_telemetry_v1(events[0])
    assert events[0]["status"] == "aborted"
    assert events[0]["trace_id"] == "trace-blocked"
    assert "allowlist_blocked" in events[0]["error_message"]
    store.close()


def test_pipeline_step_from_dict_parses_and_validates_retry_fields() -> None:
    step = PipelineStep.from_dict(
        {
            "step_id": "s1",
            "tool_id": "list_orders",
            "max_retries": 3,
            "backoff_ms": 250,
        }
    )
    assert step.max_retries == 3
    assert step.backoff_ms == 250

    with pytest.raises(ValueError, match="max_retries"):
        PipelineStep.from_dict({"step_id": "s1", "tool_id": "t1", "max_retries": -1})

    with pytest.raises(ValueError, match="backoff_ms"):
        PipelineStep.from_dict({"step_id": "s1", "tool_id": "t1", "backoff_ms": -5})


def test_lint_openapi_spec_passes_clean_spec() -> None:
    report = lint_openapi_spec(_clean_openapi_spec())
    assert report["ok"] is True
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["issues"] == []


def test_lint_openapi_spec_catches_missing_operation_id_schemas_and_unsafe_urls() -> None:
    bad_spec = {
        "openapi": "3.0.1",
        "info": {"title": "Broken API", "version": "0.1.0"},
        "servers": [
            {"url": "http://api.insecure.example.com"},
            {"url": "https://*.wildcard.example.com"},
        ],
        "paths": {
            "/items": {
                "get": {
                    # missing operationId
                    "parameters": [
                        {"name": "q", "in": "query"},  # missing schema
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": {
                    "operationId": "dupOp",
                    "requestBody": {
                        "content": {
                            "application/json": {},  # missing schema
                        }
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/items/{id}": {
                "delete": {
                    "operationId": "dupOp",  # duplicate operationId
                    "responses": {"204": {"description": "Deleted"}},
                }
            },
        },
    }

    report = lint_openapi_spec(bad_spec)
    assert report["ok"] is False
    assert report["error_count"] >= 6

    messages = [issue["message"] for issue in report["issues"]]
    paths = [issue["path"] for issue in report["issues"]]

    assert any("non-HTTPS" in msg for msg in messages)
    assert any("wildcard" in msg for msg in messages)
    assert "paths./items.get.operationId" in paths
    assert "paths./items.get.parameters[0].schema" in paths
    assert "paths./items.post.requestBody.content.application/json.schema" in paths
    assert any("Duplicate operationId" in msg for msg in messages)


def test_lint_openapi_spec_rejects_non_3x_and_empty_paths() -> None:
    report = lint_openapi_spec({"swagger": "2.0", "paths": {}})
    assert report["ok"] is False
    assert report["error_count"] >= 2
    issue_paths = {issue["path"] for issue in report["issues"]}
    assert "openapi" in issue_paths
    assert "paths" in issue_paths


def test_cli_lint_spec_json_and_yaml_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("MCP_OBSERVATORY_ALLOW_DEV_SECRET", "1")
    from toolweave import server

    clean_json = tmp_path / "clean.json"
    clean_json.write_text(json.dumps(_clean_openapi_spec()), encoding="utf-8")

    rc_clean_json = server.main(["--lint-spec", str(clean_json)])
    assert rc_clean_json == 0
    out_clean = json.loads(capsys.readouterr().out)
    assert out_clean["ok"] is True
    assert out_clean["error_count"] == 0

    clean_yaml = tmp_path / "clean.yaml"
    clean_yaml.write_text(yaml.safe_dump(_clean_openapi_spec()), encoding="utf-8")

    rc_clean_yaml = server.main(["--lint-spec", str(clean_yaml)])
    assert rc_clean_yaml == 0
    out_yaml = json.loads(capsys.readouterr().out)
    assert out_yaml["ok"] is True

    bad_spec = _clean_openapi_spec()
    bad_spec["servers"] = [{"url": "http://insecure.example.com"}]
    del bad_spec["paths"]["/orders"]["get"]["operationId"]
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(yaml.safe_dump(bad_spec), encoding="utf-8")

    rc_bad = server.main(["--lint-spec", str(bad_yaml)])
    assert rc_bad == 1
    out_bad = json.loads(capsys.readouterr().out)
    assert out_bad["ok"] is False
    assert out_bad["error_count"] == 2

    rc_missing = server.main(["--lint-spec", str(tmp_path / "does_not_exist.json")])
    assert rc_missing == 1
    out_missing = json.loads(capsys.readouterr().out)
    assert out_missing["ok"] is False
    assert out_missing["error_count"] == 1
