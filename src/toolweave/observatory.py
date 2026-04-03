from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncGenerator

import boto3

from mcp_observatory import ToolProposer
from mcp_observatory.proposal_commit import CommitTokenManager
from mcp_observatory.proposal_commit.proposer import ProposalConfig
from mcp_observatory.proposal_commit.storage import InMemoryStorage
from mcp_observatory.proposal_commit.verifier import CommitVerifier

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SECRET_KEY = os.environ.get("OBSERVATORY_SECRET_KEY", "change-me-in-production")
_BLOCK_THRESHOLD = float(os.environ.get("OBSERVATORY_BLOCK_THRESHOLD", "0.45"))
OBSERVATORY_METRICS_TABLE = os.environ.get(
    "OBSERVATORY_METRICS_TABLE", "tarun-teamweave-shared-OBSERVATORY_METRICS"
)

# ---------------------------------------------------------------------------
# mcp-observatory singletons (Lambda warm-container reuse)
# ---------------------------------------------------------------------------

_storage = InMemoryStorage()
_token_manager = CommitTokenManager(secret=_SECRET_KEY)
_proposer = ToolProposer(
    storage=_storage,
    config=ProposalConfig(block_threshold=_BLOCK_THRESHOLD),
    token_manager=_token_manager,
)
_verifier = CommitVerifier(
    storage=_storage,
    token_manager=_token_manager,
)

# ---------------------------------------------------------------------------
# Shared observability metrics — DynamoDB (tarun-teamweave-shared)
# ---------------------------------------------------------------------------

_ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_metrics_table = _ddb.Table(OBSERVATORY_METRICS_TABLE)


def _write_invocation_metric(
    tool_name: str,
    inputs: dict[str, Any],
    duration_ms: float,
    status: str,
    error_msg: str = "",
) -> None:
    """Write one invocation record to the shared OBSERVATORY_METRICS DynamoDB table."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        _metrics_table.put_item(
            Item={
                "PK": f"INVOCATION#{tool_name}",
                "SK": ts,
                "tool_name": tool_name,
                "service": "toolweave",
                "inputs": json.dumps(inputs, default=str)[:1000],
                "duration_ms": Decimal(str(round(duration_ms, 2))),
                "status": status,
                "error": error_msg[:500] if error_msg else "",
            }
        )
    except Exception:
        pass  # never let metrics writes crash the main flow


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
