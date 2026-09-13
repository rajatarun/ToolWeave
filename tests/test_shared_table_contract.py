"""Conformance of ToolWeave's OBSERVATORY_METRICS writers to the shared contract.

The shared ``OBSERVATORY_METRICS`` table (provisioned by the
``tarun-teamweave-shared`` stack) is written by several services and read by
several dashboards, none of which can see each other's code. The item shape is
therefore a cross-repository interface, pinned in ``contracts/`` — a verbatim
copy of the canonical files in ``rajatarun/mcp-observatory``.

These tests drive the **real** writers in ``toolweave.observatory`` with the
DynamoDB table handle swapped for a capturing double, so what is asserted is
the item the deployed code would actually PutItem — not a re-implementation of
it in the test.

Following ``tests/test_observatory.py``, the module is imported under a
monkeypatched environment because it builds its proposer/verifier triple at
import time, and is dropped from ``sys.modules`` afterwards.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

from contracts.conformance import check_item, load_contract

MODULE_NAME = "toolweave.observatory"


class _CapturingTable:
    """Stand-in for ``boto3.resource('dynamodb').Table(...)``.

    Records every item instead of calling AWS. It deliberately does **not**
    validate the key schema — the point of these tests is that the contract
    checker catches what a permissive double would let through, which is
    exactly what the swallowed ValidationException used to hide in production.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, Item: dict) -> dict:  # noqa: N803 - boto3 kwarg name
        self.items.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _ExplodingTable:
    def put_item(self, Item: dict) -> dict:  # noqa: N803 - boto3 kwarg name
        raise RuntimeError("ValidationException: Missing the key pk in the item")


@pytest.fixture
def observatory(monkeypatch):
    monkeypatch.setenv("OBSERVATORY_SECRET_KEY", "a-real-strong-secret-value")
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)
    yield module
    sys.modules.pop(MODULE_NAME, None)


@pytest.fixture
def table(observatory, monkeypatch):
    capturing = _CapturingTable()
    monkeypatch.setattr(observatory, "_metrics_table", capturing)
    return capturing


def _make_context():
    from mcp_observatory.core.context import TraceContext

    return TraceContext(
        service="toolweave",
        model="anthropic.claude-3-haiku-20240307-v1:0",
        method="invoke_agent",
        prompt_tokens=120,
        completion_tokens=45,
        cost_usd=0.0012345678,
        hallucination_risk_level="low",
        hallucination_risk_score=0.1234567,
        composite_risk_level="low",
        composite_risk_score=0.2345678,
        policy_decision="allow",
    )


async def _export_span(observatory, table):
    await observatory.DynamoDBSpanExporter().export(_make_context())
    assert len(table.items) == 1, "exporter wrote no item"
    return table.items[0]


def _write_invocation(observatory, table):
    observatory._write_invocation_metric(
        tool_name="search_catalog",
        inputs={"query": "orders"},
        duration_ms=12.3456,
        status="success",
    )
    assert len(table.items) == 1, "invocation writer wrote no item"
    return table.items[0]


# ---------------------------------------------------------------------------
# Contract conformance — both writers
# ---------------------------------------------------------------------------


async def test_span_exporter_item_conforms_to_contract(observatory, table):
    item = await _export_span(observatory, table)
    assert check_item(item, load_contract()) == []


def test_invocation_metric_item_conforms_to_contract(observatory, table):
    item = _write_invocation(observatory, table)
    assert check_item(item, load_contract()) == []


# ---------------------------------------------------------------------------
# Contract v2.0.0 -- SpanTimelineIndex GSI keys, both writers
# ---------------------------------------------------------------------------


async def test_span_exporter_item_carries_the_span_timeline_index_keys(observatory, table):
    """Both writers must emit the GSI's key attributes (named in the contract
    file, not retyped here) and they must agree on the UTC day (I6/I7) -- a
    GSI indexes only items carrying both keys, so a writer omitting either is
    as invisible as it was before this migration."""
    contract = load_contract()
    item = await _export_span(observatory, table)
    gsi = contract["gsi"]

    assert gsi["partition_key"] in item
    assert gsi["sort_key"] in item
    assert item[gsi["partition_key"]] == item[gsi["sort_key"]][:10]
    assert check_item(item, contract) == []


def test_invocation_metric_item_carries_the_span_timeline_index_keys(observatory, table):
    contract = load_contract()
    item = _write_invocation(observatory, table)
    gsi = contract["gsi"]

    assert gsi["partition_key"] in item
    assert gsi["sort_key"] in item
    assert item[gsi["partition_key"]] == item[gsi["sort_key"]][:10]
    assert check_item(item, contract) == []


# ---------------------------------------------------------------------------
# Regression guard for the key-case bug
# ---------------------------------------------------------------------------


async def test_span_exporter_uses_lower_case_key_attributes(observatory, table):
    """Regression test for the bug that made this writer a no-op.

    The item used to spell its key attributes ``PK``/``SK`` while the table
    declares ``pk`` (HASH) / ``sk`` (RANGE). DynamoDB attribute names are case
    sensitive, so every PutItem was rejected with a ValidationException, and
    the bare ``except Exception: pass`` around the call reported that rejection
    as a success. ToolWeave emitted no telemetry at all and nothing said so.
    """
    item = await _export_span(observatory, table)
    assert "pk" in item and "sk" in item
    assert "PK" not in item and "SK" not in item


def test_invocation_metric_uses_lower_case_key_attributes(observatory, table):
    """Same regression guard for the INVOCATION# writer, which had the bug too."""
    item = _write_invocation(observatory, table)
    assert "pk" in item and "sk" in item
    assert "PK" not in item and "SK" not in item


# ---------------------------------------------------------------------------
# Historical: pk-based reachability (superseded by the SpanTimelineIndex GSI)
# ---------------------------------------------------------------------------


async def test_span_exporter_pk_is_unchanged_by_the_v2_migration(observatory, table):
    """pk reachability is no longer the question (contract v2.0.0 supersedes
    I5 -- ``namespace_registry`` is now ``legacy-informational``: reads go
    through the SpanTimelineIndex GSI, not a pk a reader has to enumerate).

    ``WRAPPER#`` having "no readers" in that registry was true before this
    migration and is retained here only as a historical note about rows
    written before it landed; it says nothing about whether a dashboard sees
    a row written today, which is exactly what
    ``test_span_exporter_item_carries_the_span_timeline_index_keys`` checks.
    v2 deliberately leaves pk alone -- it is the writer's own business now --
    so this only pins that the discriminator itself didn't move.
    """
    item = await _export_span(observatory, table)
    assert item["pk"].startswith("WRAPPER#")


def test_invocation_metric_pk_is_unchanged_by_the_v2_migration(observatory, table):
    """Same historical note for ``INVOCATION#`` -- see the test above."""
    item = _write_invocation(observatory, table)
    assert item["pk"].startswith("INVOCATION#")


# ---------------------------------------------------------------------------
# The failure path stays swallowed, but stops being silent
# ---------------------------------------------------------------------------


async def test_write_failure_is_logged_and_never_propagates(observatory, monkeypatch, caplog):
    """A rejected write must warn once — the original bug hid for lack of this."""
    monkeypatch.setattr(observatory, "_metrics_table", _ExplodingTable())
    monkeypatch.setattr(observatory, "_metrics_write_failures", 0)

    with caplog.at_level(logging.WARNING, logger=MODULE_NAME):
        await observatory.DynamoDBSpanExporter().export(_make_context())

    assert any(
        "OBSERVATORY_METRICS write failed" in record.message
        or "OBSERVATORY_METRICS write failed" in record.getMessage()
        for record in caplog.records
    )


async def test_write_failure_logging_does_not_spam_the_hot_path(observatory, monkeypatch, caplog):
    """Metrics writes sit in every tool call, so repeated failures must stay quiet."""
    monkeypatch.setattr(observatory, "_metrics_table", _ExplodingTable())
    monkeypatch.setattr(observatory, "_metrics_write_failures", 0)

    with caplog.at_level(logging.WARNING, logger=MODULE_NAME):
        for _ in range(50):
            await observatory.DynamoDBSpanExporter().export(_make_context())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected one warning for 50 failures, got {len(warnings)}"
