"""Unit tests for ToolWeave ToolPolicyGuard and ToolPipeline trace_id hardening."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from toolweave.local_catalog import SQLiteCatalogStore
from toolweave.models import EndpointEntry, EndpointParameter
from toolweave.pipeline import PipelineStep, ToolPipeline
from toolweave.policy_guard import REDACTED_VALUE, ToolPolicyGuard


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
                ),
                EndpointParameter(
                    name="api_key",
                    location="header",
                    required=False,
                    data_type="string",
                ),
            ],
        ),
        EndpointEntry(
            api_id="orders",
            operation_id="delete_order",
            method="DELETE",
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
    ]
    store.save_entries("orders", entries)


def test_policy_guard_allowlist_denylist_and_quotas() -> None:
    guard = ToolPolicyGuard(
        allowed_tools={"orders:get_order", "orders:delete_order"},
        denied_tools={"orders:delete_order"},
        max_calls_per_tool={"orders:get_order": 2},
    )

    # Denied takes precedence over allowed
    denied = guard.check_call("orders:delete_order", {"orderId": "1"})
    assert denied["allowed"] is False
    assert denied["reason"] == "tool_denied:orders:delete_order"

    # Unlisted tool blocked by allowlist
    unlisted = guard.check_call("billing:charge", {})
    assert unlisted["allowed"] is False
    assert unlisted["reason"] == "tool_not_allowed:billing:charge"

    # Quota enforced across calls
    assert guard.check_call("orders:get_order", {"orderId": "1"})["allowed"] is True
    guard.record_call("orders:get_order")
    assert guard.check_call("orders:get_order", {"orderId": "2"})["allowed"] is True
    guard.record_call("orders:get_order")
    assert guard.get_call_count("orders:get_order") == 2

    over_quota = guard.check_call("orders:get_order", {"orderId": "3"})
    assert over_quota["allowed"] is False
    assert over_quota["reason"] == "tool_quota_exceeded:orders:get_order:2/2"

    guard.reset_counts()
    assert guard.check_call("orders:get_order", {"orderId": "3"})["allowed"] is True


def test_policy_guard_redacts_sensitive_args_recursively() -> None:
    guard = ToolPolicyGuard()
    raw_args = {
        "orderId": "ord-42",
        "api_key": "sk-live-secret",
        "password": "hunter2",
        "nested": {
            "token": "tok-abc",
            "safe": "visible",
            "items": [{"Authorization": "Bearer xyz", "label": "ok"}],
        },
    }
    decision = guard.check_call("orders:get_order", raw_args)
    assert decision["allowed"] is True
    sanitized = decision["sanitized_args"]
    assert sanitized["orderId"] == "ord-42"
    assert sanitized["api_key"] == REDACTED_VALUE
    assert sanitized["password"] == REDACTED_VALUE
    assert sanitized["nested"]["token"] == REDACTED_VALUE
    assert sanitized["nested"]["safe"] == "visible"
    assert sanitized["nested"]["items"][0]["Authorization"] == REDACTED_VALUE
    assert sanitized["nested"]["items"][0]["label"] == "ok"
    # Original input dict must not be mutated
    assert raw_args["api_key"] == "sk-live-secret"


def test_policy_guard_from_agent_policy() -> None:
    policy_dict = {
        "schema": "agent-policy-v1",
        "policy_id": "pol-edge-1",
        "enforcement_mode": "enforce",
        "allowed_tools": ["orders:get_order"],
        "denied_tools": ["orders:delete_order"],
        "max_calls_per_tool": {"orders:get_order": 1},
        "redact_arg_keys": ["api_key", "custom_secret"],
    }
    guard = ToolPolicyGuard.from_agent_policy(policy_dict)
    res = guard.check_call("orders:get_order", {"custom_secret": "s3cr3t", "id": "1"})
    assert res["allowed"] is True
    assert res["sanitized_args"]["custom_secret"] == REDACTED_VALUE
    assert res["sanitized_args"]["id"] == "1"

    assert guard.check_call("orders:delete_order")["allowed"] is False

    # Disabled enforcement mode permits calls while still sanitizing
    disabled = ToolPolicyGuard.from_agent_policy(
        {
            "schema": "agent-policy-v1",
            "enforcement_mode": "disabled",
            "denied_tools": ["orders:delete_order"],
        }
    )
    assert disabled.check_call("orders:delete_order", {"token": "abc"})["allowed"] is True
    assert (
        disabled.check_call("orders:delete_order", {"token": "abc"})["sanitized_args"]["token"]
        == REDACTED_VALUE
    )

    with pytest.raises(ValueError, match="Unsupported policy schema"):
        ToolPolicyGuard.from_agent_policy({"schema": "unknown-v99"})


@pytest.mark.parametrize("raw_trace_id", [None, "", "   ", "\t\n"])
def test_pipeline_normalizes_blank_trace_id(tmp_path: Path, raw_trace_id: str | None) -> None:
    store = SQLiteCatalogStore(tmp_path / "catalog.db")
    _seed_catalog(store)
    events: list[dict[str, Any]] = []

    def _fake_http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        _ = (method, url, kwargs)
        return {"status_code": 200, "body": {"id": "ord-1", "status": "ok"}}

    pipeline = ToolPipeline(
        store,
        http_executor=_fake_http,
        telemetry_sink=events.append,
    )

    result = pipeline.execute(
        [PipelineStep(step_id="s1", tool_id="get_order", arguments={"orderId": "ord-1"})],
        trace_id=raw_trace_id,
    )
    assert result.status == "completed"
    assert len(events) == 1
    trace_id = events[0]["trace_id"]
    assert isinstance(trace_id, str)
    assert trace_id.startswith("trace-")
    assert len(trace_id) > len("trace-")


def test_pipeline_policy_guard_blocks_before_http_and_sanitizes_telemetry(tmp_path: Path) -> None:
    store = SQLiteCatalogStore(tmp_path / "catalog.db")
    _seed_catalog(store)
    events: list[dict[str, Any]] = []
    http_calls: list[str] = []

    def _fake_http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        http_calls.append(f"{method} {url}")
        return {"status_code": 200, "body": {"ok": True}}

    guard = ToolPolicyGuard(
        allowed_tools={"get_order"},
        denied_tools={"delete_order"},
        max_calls_per_tool={"get_order": 1},
    )
    pipeline = ToolPipeline(
        store,
        http_executor=_fake_http,
        telemetry_sink=events.append,
        policy_guard=guard,
    )

    # First call succeeds and emits sanitized telemetry
    ok_res = pipeline.execute(
        [
            PipelineStep(
                step_id="s1",
                tool_id="get_order",
                arguments={"orderId": "ord-1", "api_key": "super-secret-key"},
            )
        ],
        trace_id="  trace-custom-1  ",
    )
    assert ok_res.status == "completed"
    assert len(http_calls) == 1
    assert events[-1]["status"] == "ok"
    assert events[-1]["trace_id"] == "trace-custom-1"
    assert events[-1]["attributes"]["sanitized_args"]["api_key"] == REDACTED_VALUE

    # Second call exceeds quota -> fails closed before HTTP and emits aborted telemetry
    quota_res = pipeline.execute(
        [
            PipelineStep(
                step_id="s2",
                tool_id="get_order",
                arguments={"orderId": "ord-2", "api_key": "another-secret"},
            )
        ],
        trace_id="   ",
    )
    assert quota_res.status == "blocked"
    assert "tool_quota_exceeded" in (quota_res.error or "")
    assert len(http_calls) == 1  # HTTP was NOT invoked
    assert events[-1]["status"] == "aborted"
    assert events[-1]["trace_id"].startswith("trace-")
    assert events[-1]["attributes"]["sanitized_args"]["api_key"] == REDACTED_VALUE
    assert "another-secret" not in str(events[-1])

    # Denied tool -> fails closed before HTTP
    denied_res = pipeline.execute(
        [PipelineStep(step_id="s3", tool_id="delete_order", arguments={"orderId": "ord-1"})],
        trace_id=None,
    )
    assert denied_res.status == "blocked"
    assert "tool_denied:delete_order" in (denied_res.error or "")
    assert len(http_calls) == 1
    assert events[-1]["status"] == "aborted"
