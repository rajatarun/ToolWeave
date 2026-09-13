from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncGenerator

import boto3

from mcp_observatory.aws import build_gate
from mcp_observatory.core.context import TraceContext
from mcp_observatory.core.wrapper_api import InvocationWrapperAPI, WrapperPolicy
from mcp_observatory.exporters.base import Exporter
from mcp_observatory.instrument import instrument_wrapper_api

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OBSERVATORY_METRICS_TABLE = os.environ.get(
    "OBSERVATORY_METRICS_TABLE", "tarun-teamweave-shared-OBSERVATORY_METRICS"
)

# ---------------------------------------------------------------------------
# mcp-observatory singletons (Lambda warm-container reuse)
#
# Wired via mcp_observatory.aws.build_gate (mcp-observatory>=0.3.0) instead of
# hand-building InMemoryStorage/CommitTokenManager/ToolProposer/CommitVerifier:
# the wiring was identical to the library's, and build_gate resolves the HMAC
# secret through mcp_observatory.utils.secrets.resolve_secret, which raises
# InsecureDefaultSecretError at construction time instead of silently falling
# back to a hardcoded default (previously "change-me-in-production" here).
# Set OBSERVATORY_SECRET_KEY in every deployed environment; set
# MCP_OBSERVATORY_ALLOW_DEV_SECRET=1 for local runs and tests only.
# ---------------------------------------------------------------------------

_proposer, _verifier, _token_manager = build_gate(
    secret_env="OBSERVATORY_SECRET_KEY",
    block_threshold_env="OBSERVATORY_BLOCK_THRESHOLD",
)

# ---------------------------------------------------------------------------
# Shared observability metrics — DynamoDB (tarun-teamweave-shared)
# Table name resolved at deploy time from the shared CloudFormation stack
# output "ObservatoryMetricsTableName" via SharedStackLookup custom resource.
# ---------------------------------------------------------------------------

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_metrics_table = _ddb.Table(OBSERVATORY_METRICS_TABLE)

logger = logging.getLogger(__name__)

# Rows in a shared table must expire rather than accumulate (contract invariant
# I4). 90 days matches the TTL TeamWeave's writer uses on the same table.
_TTL_SECONDS = 90 * 24 * 60 * 60

_metrics_write_failures = 0


def _warn_metrics_write_failed(pk: str, exc: BaseException) -> None:
    """Log a swallowed metrics-write failure without raising and without spamming.

    Metrics writes sit in the request path of every tool call and every wrapped
    model invocation, so a per-call warning would flood the log of a hot Lambda.
    This logs the first failure in the process and every 1000th afterwards, which
    is enough for a systemic failure (a rejected item shape, a missing IAM grant)
    to appear at least once while a transient throttle stays quiet.

    The whole body is guarded: a logging failure must not become the exception
    that metrics writes exist never to raise.
    """
    global _metrics_write_failures
    try:
        _metrics_write_failures += 1
        if _metrics_write_failures == 1 or _metrics_write_failures % 1000 == 0:
            logger.warning(
                "OBSERVATORY_METRICS write failed (pk=%s, table=%s, "
                "%d failure(s) in this process): %s: %s",
                pk,
                OBSERVATORY_METRICS_TABLE,
                _metrics_write_failures,
                type(exc).__name__,
                exc,
            )
    except Exception:
        pass


class DynamoDBSpanExporter(Exporter):
    """Exports InvocationWrapperAPI span telemetry to the shared OBSERVATORY_METRICS table.

    The table is provisioned in the shared CloudFormation stack (tarun-teamweave-shared)
    and its name is injected at deploy time via the OBSERVATORY_METRICS_TABLE env var,
    resolved by the SharedStackLookup custom resource from the stack output
    "ObservatoryMetricsTableName".

    Kept hand-rolled rather than mcp_observatory.aws.DynamoDBSpanExporter: this
    table is shared with other services (a "service": "toolweave" tag plus
    WRAPPER#/INVOCATION# pk prefixes distinguish rows), and the library's
    exporter writes a different item shape (pk="SPAN#...", every populated
    TraceContext field, no service tag) — swapping would silently change what
    is written to a table other consumers may already query.

    Item shape is pinned by contracts/observatory_metrics_item.json (vendored
    from mcp-observatory) and asserted by tests/test_shared_table_contract.py.

    Known limitation: the WRAPPER# and INVOCATION# namespaces this module
    writes have NO reader. TeamWeave's dashboards and DeployWeave's model
    selector query OBSERVATORY#{operation} partitions only, so these rows are
    now durable and billable but still invisible to every dashboard. Which
    namespace scheme wins across the portfolio is an open platform decision;
    it is deliberately not resolved here by renaming the namespace.
    """

    async def export(self, context: TraceContext) -> None:
        pk = f"WRAPPER#{context.method or 'unknown'}"
        try:
            ts = datetime.now(timezone.utc).isoformat()
            _metrics_table.put_item(
                Item={
                    "pk": pk,
                    "sk": f"{ts}#{context.trace_id}",
                    "timestamp": ts,
                    "ttl": Decimal(int(time.time()) + _TTL_SECONDS),
                    "service": "toolweave",
                    "source": context.method or "unknown",
                    "model": context.model or "",
                    "trace_id": context.trace_id,
                    "prompt_tokens": context.prompt_tokens or 0,
                    "completion_tokens": context.completion_tokens or 0,
                    "cost_usd": Decimal(str(round(context.cost_usd or 0.0, 6))),
                    "hallucination_risk_level": context.hallucination_risk_level or "unknown",
                    "hallucination_risk_score": Decimal(
                        str(round(context.hallucination_risk_score or 0.0, 4))
                    ),
                    "composite_risk_level": context.composite_risk_level or "unknown",
                    "composite_risk_score": Decimal(
                        str(round(context.composite_risk_score or 0.0, 4))
                    ),
                    "policy_decision": context.policy_decision or "allow",
                    "fallback_reason": context.fallback_reason or "",
                }
            )
        except Exception as exc:  # never let metrics writes crash the main flow
            _warn_metrics_write_failed(pk, exc)


# ---------------------------------------------------------------------------
# InvocationWrapperAPI singletons — agent-level and model-level telemetry
# Span metrics are exported to the shared OBSERVATORY_METRICS DynamoDB table.
# ---------------------------------------------------------------------------

_span_exporter = DynamoDBSpanExporter()

_agent_wrapper: InvocationWrapperAPI = instrument_wrapper_api(
    "toolweave-agent",
    exporter=_span_exporter,
    policy=WrapperPolicy(max_cost_usd=1.0, max_latency_ms=60_000.0),
)

_model_wrapper: InvocationWrapperAPI = instrument_wrapper_api(
    "toolweave-model",
    exporter=_span_exporter,
    policy=WrapperPolicy(max_cost_usd=0.25, max_latency_ms=20_000.0),
)


def get_agent_wrapper() -> InvocationWrapperAPI:
    """Return the agent-level InvocationWrapperAPI singleton."""
    return _agent_wrapper


def get_model_wrapper() -> InvocationWrapperAPI:
    """Return the model-level InvocationWrapperAPI singleton."""
    return _model_wrapper


def _write_invocation_metric(
    tool_name: str,
    inputs: dict[str, Any],
    duration_ms: float,
    status: str,
    error_msg: str = "",
) -> None:
    """Write one invocation record to the shared OBSERVATORY_METRICS DynamoDB table."""
    pk = f"INVOCATION#{tool_name}"
    try:
        ts = datetime.now(timezone.utc).isoformat()
        # No TraceContext here, so the sk discriminator is a fresh id; the
        # contract requires sk to be "{iso8601}#{id}" and unique per row.
        _metrics_table.put_item(
            Item={
                "pk": pk,
                "sk": f"{ts}#{uuid.uuid4().hex}",
                "timestamp": ts,
                "ttl": Decimal(int(time.time()) + _TTL_SECONDS),
                "tool_name": tool_name,
                "service": "toolweave",
                "inputs": json.dumps(inputs, default=str)[:1000],
                "duration_ms": Decimal(str(round(duration_ms, 2))),
                "status": status,
                "error": error_msg[:500] if error_msg else "",
            }
        )
    except Exception as exc:  # never let metrics writes crash the main flow
        _warn_metrics_write_failed(pk, exc)


# ---------------------------------------------------------------------------
# Invocation tracking context manager — wraps ALL four MCP tools
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def track_invocation(
    tool_name: str,
    inputs: dict[str, Any],
) -> AsyncGenerator[None, None]:
    """Async context manager that records timing and status to the shared metrics table."""
    start = time.monotonic()
    try:
        yield
        duration_ms = (time.monotonic() - start) * 1000
        _write_invocation_metric(tool_name, inputs, duration_ms, "success")
    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        _write_invocation_metric(tool_name, inputs, duration_ms, "error", str(exc))
        raise


# ---------------------------------------------------------------------------
# Proposal / commit — cross-invocation persistence via DynamoDB
# ---------------------------------------------------------------------------


async def propose(
    tool_name: str,
    tool_args: dict[str, Any],
    prompt: str,
    method: str = "",
    path: str = "",
) -> dict[str, Any]:
    """Propose a write operation via mcp-observatory and persist it to DynamoDB."""
    candidate_a = f"{method} {path} — proposed via ToolWeave"
    candidate_b = f"Execute {tool_name}: {json.dumps(tool_args, default=str)[:120]}"

    result = await _proposer.propose(
        tool_name=tool_name,
        tool_args=tool_args,
        prompt=prompt,
        candidate_output_a=candidate_a,
        candidate_output_b=candidate_b,
    )

    proposal_id = result.get("proposal_id")
    if proposal_id:
        from . import dynamodb_client

        dynamodb_client.save_proposal(
            proposal_id,
            {"tool_name": tool_name, "tool_args": tool_args},
        )

    return result


async def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    """Retrieve a stored proposal from DynamoDB."""
    from . import dynamodb_client

    return dynamodb_client.get_proposal_data(proposal_id)


async def verify(
    proposal_id: str,
    commit_token: str,
    tool_name: str,
    tool_args: dict[str, Any],
) -> Any:
    """Verify a commit token. Returns object with .ok (bool) and .failure_reason."""
    return await _verifier.verify_commit(
        proposal_id=proposal_id,
        commit_token=commit_token,
        tool_name=tool_name,
        tool_args=tool_args,
    )
