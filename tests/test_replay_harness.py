"""Unit and integration tests for ToolWeave ToolReplayHarness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from toolweave.local_catalog import SQLiteCatalogStore
from toolweave.models import EndpointEntry, EndpointParameter
from toolweave.pipeline import PipelineStep, ToolPipeline
from toolweave.policy_guard import REDACTED_VALUE, ToolPolicyGuard
from toolweave.replay_harness import ReplayMissError, ToolReplayHarness


def test_record_redacts_secrets_and_replays_offline(tmp_path: Path) -> None:
    fixture = tmp_path / "fixtures" / "tools.jsonl"
    guard = ToolPolicyGuard()

    live_calls = 0

    def _live_tool(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal live_calls
        live_calls += 1
        return {"ok": True, "order_id": args["order_id"], "items": [1, 2]}

    recorder = ToolReplayHarness(fixture, mode="record", policy_guard=guard)
    res = recorder.execute(
        "orders:get",
        {
            "order_id": "ord-101",
            "api_key": "sk-super-secret-999",
            "token": "tok-live-xyz",
            "nested": {"password": "my-secret-password"},
        },
        _live_tool,
    )
    assert res == {"ok": True, "order_id": "ord-101", "items": [1, 2]}
    assert live_calls == 1

    raw_text = fixture.read_text(encoding="utf-8")
    assert "sk-super-secret-999" not in raw_text
    assert "tok-live-xyz" not in raw_text
    assert "my-secret-password" not in raw_text

    recorded = json.loads(raw_text.splitlines()[0])
    assert recorded["tool_name"] == "orders:get"
    assert recorded["redacted_args"]["api_key"] == REDACTED_VALUE
    assert recorded["redacted_args"]["token"] == REDACTED_VALUE
    assert recorded["redacted_args"]["nested"]["password"] == REDACTED_VALUE
    assert recorded["error"] is None

    # Replay mode returns identical response with zero live calls
    replayer = ToolReplayHarness(fixture, mode="replay", policy_guard=guard)
    replayed = replayer.execute(
        "orders:get",
        {
            "order_id": "ord-101",
            "api_key": "different-dummy-key",
            "token": "different-dummy-token",
            "nested": {"password": "other"},
        },
    )
    assert replayed == {"ok": True, "order_id": "ord-101", "items": [1, 2]}
    assert live_calls == 1


def test_replay_miss_raises_replay_miss_error(tmp_path: Path) -> None:
    fixture = tmp_path / "empty.jsonl"
    replayer = ToolReplayHarness(fixture, mode="replay")

    with pytest.raises(ReplayMissError, match="orders:missing"):
        replayer.execute("orders:missing", {"id": "404"})


@pytest.mark.asyncio
async def test_async_wrap_record_and_replay_including_error(tmp_path: Path) -> None:
    fixture = tmp_path / "async.jsonl"

    async def _async_ok(args: dict[str, Any]) -> dict[str, Any]:
        return {"echo": args["msg"]}

    async def _async_fail(args: dict[str, Any]) -> dict[str, Any]:
        _ = args
        raise ValueError("upstream timeout")

    recorder = ToolReplayHarness(fixture, mode="record")
    wrapped_ok = recorder.wrap("chat:echo", _async_ok)
    assert await wrapped_ok({"msg": "hello", "secret": "s3cr3t"}) == {"echo": "hello"}

    wrapped_fail = recorder.wrap("chat:fail", _async_fail)
    with pytest.raises(ValueError, match="upstream timeout"):
        await wrapped_fail({"msg": "boom"})

    assert "s3cr3t" not in fixture.read_text(encoding="utf-8")

    replayer = ToolReplayHarness(fixture, mode="replay")
    assert await replayer.aexecute("chat:echo", {"msg": "hello", "secret": "any"}) == {"echo": "hello"}
    with pytest.raises(RuntimeError, match="upstream timeout"):
        await replayer.aexecute("chat:fail", {"msg": "boom"})


def test_tool_pipeline_record_and_offline_replay(tmp_path: Path) -> None:
    store = SQLiteCatalogStore(tmp_path / "catalog.db")
    store.save_entries(
        "orders",
        [
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
            )
        ],
    )

    fixture = tmp_path / "pipeline.jsonl"
    network_calls = 0

    def _fake_network(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal network_calls
        network_calls += 1
        _ = (method, url, kwargs)
        return {"status_code": 200, "body": {"id": "ord-7", "total": 42}}

    # Record phase
    rec_harness = ToolReplayHarness(fixture, mode="record")
    rec_pipeline = ToolPipeline(
        store,
        http_executor=rec_harness.make_http_executor(_fake_network),
    )
    rec_res = rec_pipeline.execute(
        [
            PipelineStep(
                step_id="fetch",
                tool_id="get_order",
                arguments={"orderId": "ord-7", "api_key": "sk-pipeline-secret"},
            )
        ]
    )
    assert rec_res.status == "completed"
    assert network_calls == 1
    assert "sk-pipeline-secret" not in fixture.read_text(encoding="utf-8")

    # Replay phase: zero network calls
    rep_harness = ToolReplayHarness(fixture, mode="replay")
    rep_pipeline = ToolPipeline(
        store,
        http_executor=rep_harness.make_http_executor(None),
    )
    rep_res = rep_pipeline.execute(
        [
            PipelineStep(
                step_id="fetch",
                tool_id="get_order",
                arguments={"orderId": "ord-7", "api_key": "sk-redacted"},
            )
        ]
    )
    assert rep_res.status == "completed"
    assert rep_res.steps[0].output == {"id": "ord-7", "total": 42}
    assert network_calls == 1
