from __future__ import annotations

"""ToolWeave FastMCP server.

Exposes four MCP tools:
  pre_tool        — Bedrock Converse agent that maps a prompt to an API call plan
  post_tool       — Executes (GET) or proposes (write) the plan from pre_tool
  commit_api_call — Commits a previously proposed write operation
  reload_catalog  — Reloads the endpoint catalog from DynamoDB

Lambda entry point: lambda_handler (via Mangum).

Every /mcp request must send ``Authorization: Bearer <token>``. The token
is the SecretString of the Secrets Manager secret named by
``MCP_AUTH_TOKEN_SECRET_NAME``. A missing, wrong, or unavailable token is
HTTP 401 with a JSON-RPC error. Other routes are not checked.
"""

import contextlib
import json
import logging
import os
import uuid
from typing import Any, Optional

from fastmcp import FastMCP
from mangum import Mangum

from . import agent, dynamodb_client, executor, mcp_auth, observatory
from .models import CommitResult, ImmediateExecutionResult, PreToolResponse, ProposalResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Module-level state — populated at cold start
# ---------------------------------------------------------------------------

_catalog: list = []  # list[EndpointEntry], populated by lifespan
_dd_context: str = ""

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "ToolWeave",
    # Reported as serverInfo; without it the SDK reports its own version.
    version="0.1.0",
    instructions=(
        "ToolWeave bridges natural-language requests to REST API calls via OpenAPI specs. "
        "Workflow: call pre_tool with a prompt → receive a plan → call post_tool with the plan "
        "→ for write operations receive a proposal → call commit_api_call to execute."
    ),
)


# ---------------------------------------------------------------------------
# Tool 1 — pre_tool
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Map a natural-language prompt to a REST API call plan. "
        "Returns status='ready' with a full plan, or status='needs_input' with a follow-up "
        "question. If needs_input, call again with the same session_id and the user's answer."
    )
)
async def pre_tool(
    prompt: str,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Identify the right API endpoint and extract field values from the prompt.

    Args:
        prompt: Natural-language request (e.g. 'Get order ORD-123').
        session_id: Conversation session ID for multi-turn interactions. Omit on first call.

    Returns:
        PreToolResponse as a dict. Pass the whole dict to post_tool when status=='ready'.
    """
    sid = session_id or str(uuid.uuid4())
    logger.info("pre_tool invoked session_id=%s", sid)

    async with observatory.track_invocation("pre_tool", {"prompt": prompt[:200], "session_id": sid}):
        if not _catalog:
            return PreToolResponse(
                session_id=sid,
                status="error",
                error=(
                    "API catalog is empty. Upload a Swagger/OpenAPI file to the S3 bucket "
                    "and call reload_catalog, or check DynamoDB table population."
                ),
            ).model_dump()

        response = await agent.run_agent(prompt, sid, _catalog, _dd_context)
        logger.info(
            "pre_tool completed session_id=%s status=%s execution_type=%s",
            sid,
            response.status,
            response.execution_type,
        )

        if response.status == "ready" and not response.execution_type:
            response.execution_type = (
                "immediate" if response.method == "GET" else "proposal"
            )

        return response.model_dump()


# ---------------------------------------------------------------------------
# Tool 2 — post_tool
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Execute or propose the API call plan produced by pre_tool. "
        "GET requests are executed immediately. POST/PUT/PATCH/DELETE create a proposal "
        "that requires commit_api_call to finalise."
    )
)
async def post_tool(api_call_plan: dict[str, Any]) -> dict[str, Any]:
    """Execute (GET) or propose (write) the plan from pre_tool.

    Args:
        api_call_plan: The dict returned by pre_tool when status=='ready'.

    Returns:
        ImmediateExecutionResult (for GET) or ProposalResult (for writes).
    """
    async with observatory.track_invocation(
        "post_tool",
        {
            "method": api_call_plan.get("method"),
            "path": api_call_plan.get("path"),
            "execution_type": api_call_plan.get("execution_type"),
        },
    ):
        try:
            plan = PreToolResponse.model_validate(api_call_plan)
        except Exception as exc:
            return {"error": f"Invalid api_call_plan: {exc}"}

        if plan.status != "ready":
            return {
                "error": f"Plan status is '{plan.status}' — only 'ready' plans can be executed."
            }

        if plan.execution_type == "immediate":
            result: ImmediateExecutionResult = await executor.execute_get(
                base_url=plan.base_url,
                path=plan.path,
                query_params=plan.query_params or None,
                headers=plan.headers or None,
            )
            return result.model_dump()

        # --- proposal for write operations ---
        tool_args = {
            "method": plan.method,
            "url": executor._build_url(plan.base_url, plan.path),
            "query_params": plan.query_params,
            "body": plan.body,
        }
        proposal = await observatory.propose(
            tool_name="commit_api_call",
            tool_args=tool_args,
            prompt=plan.prompt or f"{plan.method} {plan.path}",
            method=plan.method,
            path=plan.path,
        )

        status = proposal.get("status", "blocked")
        result_obj = ProposalResult(
            method=plan.method,
            url=executor._build_url(plan.base_url, plan.path),
            body=plan.body,
            status=status,  # type: ignore[arg-type]
            proposal_id=proposal.get("proposal_id", ""),
            commit_token=proposal.get("commit_token"),
            composite_score=float(proposal.get("composite_score", 0.0)),
            signals=proposal.get("signals", {}),
            next_step=(
                f"Call commit_api_call(proposal_id='{proposal.get('proposal_id')}', "
                f"commit_token='...') to execute this {plan.method} request."
            )
            if status == "allowed"
            else "Proposal was blocked by mcp-observatory risk scoring.",
        )
        return result_obj.model_dump()


# ---------------------------------------------------------------------------
# Tool 3 — commit_api_call
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Commit a previously proposed write operation. "
        "Requires the proposal_id and commit_token returned by post_tool."
    )
)
async def commit_api_call(proposal_id: str, commit_token: str) -> dict[str, Any]:
    """Execute a proposed API call after user confirmation.

    Args:
        proposal_id: The proposal ID returned by post_tool.
        commit_token: The commit token returned by post_tool.

    Returns:
        CommitResult with status 'committed', 'blocked', or 'error'.
    """
    async with observatory.track_invocation(
        "commit_api_call", {"proposal_id": proposal_id}
    ):
        proposal = await observatory.get_proposal(proposal_id)
        if not proposal:
            return CommitResult(
                status="error",
                proposal_id=proposal_id,
                reason=f"Proposal '{proposal_id}' not found or has expired.",
            ).model_dump()

        tool_args: dict[str, Any] = proposal.get("tool_args", {})
        tool_name: str = proposal.get("tool_name", "commit_api_call")

        verification = await observatory.verify(proposal_id, commit_token, tool_name, tool_args)
        if not verification.ok:
            return CommitResult(
                status="blocked",
                proposal_id=proposal_id,
                reason=getattr(verification, "failure_reason", "Verification failed."),
            ).model_dump()

        method: str = tool_args.get("method", "POST")
        url: str = tool_args.get("url", "")
        body = tool_args.get("body")
        query_params = tool_args.get("query_params") or {}

        exec_result: ImmediateExecutionResult = await executor.execute_write(
            method=method,
            base_url="",   # url is already fully resolved
            path=url,
            query_params=query_params or None,
            body=body,
        )

        if exec_result.error:
            return CommitResult(
                status="error",
                proposal_id=proposal_id,
                reason=exec_result.error,
                method=method,
                url=url,
            ).model_dump()

        return CommitResult(
            status="committed",
            commit_id=str(uuid.uuid4()),
            proposal_id=proposal_id,
            method=method,
            url=url,
            response_body=exec_result.response_body,
        ).model_dump()


# ---------------------------------------------------------------------------
# health — liveness, for callers checking that their token and the route work
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Report that ToolWeave is up and how many endpoints are loaded. "
        "Read-only; calls no API and does not reload the catalog."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def health() -> dict[str, Any]:
    """Liveness check. No DynamoDB, Bedrock, or outbound API call."""
    return {
        "status": "ok",
        "service": "toolweave",
        "endpoint_count": len(_catalog),
    }


# ---------------------------------------------------------------------------
# Tool 4 — reload_catalog
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Reload the API endpoint catalog from DynamoDB. "
        "Call this after uploading new Swagger files to the S3 bucket and allowing "
        "the SwaggerProcessorFunction time to process them."
    )
)
async def reload_catalog() -> dict[str, Any]:
    """Refresh the in-memory endpoint catalog from DynamoDB.

    Returns:
        Status, endpoint count, and number of distinct APIs loaded.
    """
    global _catalog, _dd_context

    async with observatory.track_invocation("reload_catalog", {}):
        _catalog = dynamodb_client.load_full_catalog()
        api_ids = {e.api_id for e in _catalog}
        logger.info(
            "Catalog reloaded: %d endpoints across %d APIs", len(_catalog), len(api_ids)
        )
        return {
            "status": "ok",
            "endpoint_count": len(_catalog),
            "api_count": len(api_ids),
        }


# ---------------------------------------------------------------------------
# Lambda handler (stateless per DataDictionary pattern)
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    # Reject unauthenticated /mcp calls before any catalog or tool work.
    # A missing secret fails closed here; it must not fall through to Mangum.
    rejection = mcp_auth.reject_if_unauthorized(event)
    if rejection is not None:
        return rejection

    # Load catalog on first (cold) invocation.  Subsequent warm invocations
    # reuse the module-level _catalog list unless reload_catalog is called.
    global _catalog, _dd_context
    if not _catalog:
        _catalog = dynamodb_client.load_full_catalog()
        _dd_context = os.environ.get("DATA_DICTIONARY_CONTEXT", "API")
        logger.info("Cold start: loaded %d endpoints", len(_catalog))

    # Create a fresh ASGI app per invocation to avoid Mangum/lifespan reuse issues
    app = mcp.http_app(stateless_http=True)
    return Mangum(app, lifespan="auto")(event, context)


def main() -> None:
    import uvicorn

    global _catalog, _dd_context
    _catalog = dynamodb_client.load_full_catalog()
    _dd_context = os.environ.get("DATA_DICTIONARY_CONTEXT", "API")
    uvicorn.run(mcp.http_app(), host="0.0.0.0", port=8080)
