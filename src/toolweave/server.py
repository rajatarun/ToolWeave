from __future__ import annotations

"""ToolWeave FastMCP server.

Exposes four MCP tools:
  pre_tool        — Bedrock Converse agent that maps a prompt to an API call plan
  post_tool       — Executes (GET) or proposes (write) the plan from pre_tool
  commit_api_call — Commits a previously proposed write operation
  reload_catalog  — Reloads the endpoint catalog from DynamoDB

Lambda entry point: lambda_handler (via Mangum).

Every /mcp request must send ``Authorization: Bearer <token>`` (a
CipherWeave token for ``toolweave``, or the legacy static token while
allowed) or ``DPoP <token>`` with a proof. A missing, wrong, or unavailable
token is HTTP 401 with a JSON-RPC error; a tool the token lacks the scope for
is HTTP 403 (``mcp_auth``). ``post_tool`` binds its plan to the catalog and
the executor refuses non-public destinations (``plan_guard``).
GET /.well-known/oauth-protected-resource serves RFC 9728 metadata.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError

from . import (
    agent,
    catalog_search,
    dynamodb_client,
    executor,
    mcp_auth,
    observatory,
    plan_guard,
)
from .local_catalog import LanAllowlistGuard, SQLiteCatalogStore
from .models import (
    CommitResult,
    ImmediateExecutionResult,
    PreToolResponse,
    ProposalResult,
)
from .pipeline import ToolPipeline
from .spec_linter import lint_openapi_spec, load_openapi_spec

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Lazy-imported inside lambda_handler so toolweave.server works without [aws]/mangum.
# Kept as a module attribute so tests can monkeypatch ``server.Mangum``.
Mangum: Any = None

# ---------------------------------------------------------------------------
# Module-level state — populated at cold start or via local CLI
# ---------------------------------------------------------------------------

_catalog: list = []  # list[EndpointEntry], populated by lifespan or local SQLite
_dd_context: str = ""
_local_store: SQLiteCatalogStore | None = None
_local_specs_dir: Path | None = None

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
    session_id: str | None = None,
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
        except ValidationError as exc:
            return {"error": f"Invalid api_call_plan: {exc}"}

        if plan.status != "ready":
            return {
                "error": f"Plan status is '{plan.status}' — only 'ready' plans can be executed."
            }

        # The plan is a tool argument: bind it to the catalog, so only a
        # cataloged endpoint can be called, whatever the caller sent.
        try:
            bound = plan_guard.bind(plan, _catalog)
        except plan_guard.PlanRejected as exc:
            logger.warning("post_tool plan rejected reason=%s", exc)
            return {"error": f"Plan rejected: {exc}"}

        # Reads run now; anything else is a proposal, whatever the plan's
        # execution_type says -- the method decides, not the caller.
        if bound.method == "GET":
            result: ImmediateExecutionResult = await executor.execute_get(
                base_url=bound.base_url,
                path=bound.path,
                query_params=bound.query_params or None,
                headers=bound.headers or None,
                api_id=bound.entry.api_id,
            )
            return result.model_dump()

        # --- proposal for write operations ---
        tool_args = {
            "method": bound.method,
            "url": executor._build_url(bound.base_url, bound.path),
            "query_params": bound.query_params,
            "headers": bound.headers,
            "body": bound.body,
            # Bound under the commit token like the rest: the commit uses the
            # credential of the API that was proposed, not one it names later.
            "api_id": bound.entry.api_id,
        }
        proposal = await observatory.propose(
            tool_name="commit_api_call",
            tool_args=tool_args,
            prompt=plan.prompt or f"{plan.method} {plan.path}",
            method=bound.method,
            path=bound.path,
        )

        status = proposal.get("status", "blocked")
        result_obj = ProposalResult(
            method=bound.method,
            url=executor._build_url(bound.base_url, bound.path),
            body=bound.body,
            status=status,  # type: ignore[arg-type]
            proposal_id=proposal.get("proposal_id", ""),
            commit_token=proposal.get("commit_token"),
            composite_score=float(proposal.get("composite_score", 0.0)),
            signals=proposal.get("signals", {}),
            next_step=(
                f"Call commit_api_call(proposal_id='{proposal.get('proposal_id')}', "
                f"commit_token='...') to execute this {bound.method} request."
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
            reason = (
                getattr(verification, "reason", None)
                or getattr(verification, "failure_reason", None)
                or "Verification failed."
            )
            return CommitResult(
                status="blocked",
                proposal_id=proposal_id,
                reason=reason,
            ).model_dump()

        method: str = tool_args.get("method", "POST")
        url: str = tool_args.get("url", "")
        body = tool_args.get("body")
        query_params = tool_args.get("query_params") or {}
        headers = tool_args.get("headers") or {}

        exec_result: ImmediateExecutionResult = await executor.execute_write(
            method=method,
            base_url="",   # url is already fully resolved
            path=url,
            query_params=query_params or None,
            headers=headers or None,
            body=body,
            api_id=str(tool_args.get("api_id") or ""),
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
# Tool 4 — search_catalog (offline/local & cloud endpoint search)
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Search the loaded OpenAPI endpoint catalog by keyword without calling Bedrock. "
        "Works offline with local --specs-dir / SQLite catalogs as well as DynamoDB catalogs."
    ),
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def search_catalog(query: str, top_k: int = 10) -> dict[str, Any]:
    """Search loaded OpenAPI endpoints by keyword."""
    entries = _local_store.load_all() if _local_store is not None else _catalog
    hits = catalog_search.search(query, entries, top_k=top_k)
    return {
        "status": "ok",
        "query": query,
        "count": len(hits),
        "endpoints": [
            h if isinstance(h, dict) else h.model_dump()
            for h in hits
        ],
    }


# ---------------------------------------------------------------------------
# Tool 5 — reload_catalog
# ---------------------------------------------------------------------------


@mcp.tool(
    description=(
        "Reload the API endpoint catalog from the local SQLite/specs store (when configured) "
        "or from DynamoDB."
    )
)
async def reload_catalog() -> dict[str, Any]:
    """Refresh the in-memory endpoint catalog from SQLite or DynamoDB.

    Returns:
        Status, endpoint count, and number of distinct APIs loaded.
    """
    global _catalog

    async with observatory.track_invocation("reload_catalog", {}):
        if _local_store is not None:
            if _local_specs_dir is not None:
                _local_store.ingest_directory(_local_specs_dir)
            _catalog = _local_store.load_all()
        else:
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
# Local SQLite catalog configuration helper
# ---------------------------------------------------------------------------


def configure_local_catalog(
    db_path: str | Path,
    specs_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Initialize local SQLite catalog, optionally ingesting OpenAPI specs from ``specs_dir``."""
    global _catalog, _local_store, _local_specs_dir

    if _local_store is not None:
        _local_store.close()
    _local_store = SQLiteCatalogStore(db_path)
    _local_specs_dir = Path(specs_dir) if specs_dir is not None else None
    ingested: dict[str, int] = {}
    if _local_specs_dir is not None:
        ingested = _local_store.ingest_directory(_local_specs_dir)
    _catalog = _local_store.load_all()
    return {
        "db_path": str(db_path),
        "specs_dir": str(_local_specs_dir) if _local_specs_dir is not None else None,
        "ingested": ingested,
        "endpoint_count": len(_catalog),
    }


# ---------------------------------------------------------------------------
# Pure-Python ASGI HTTP API v2 fallback when Mangum is not installed
# ---------------------------------------------------------------------------


def _invoke_asgi_apigw_v2(app: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Invoke an ASGI3 app directly for an API Gateway HTTP API v2 event."""

    async def _run() -> dict[str, Any]:
        # Run ASGI lifespan startup/shutdown cleanly around the request
        startup_complete = asyncio.Event()
        shutdown_trigger = asyncio.Event()
        shutdown_complete = asyncio.Event()
        lifespan_failed = False

        async def lifespan_receive() -> dict[str, Any]:
            if not startup_complete.is_set():
                return {"type": "lifespan.startup"}
            await shutdown_trigger.wait()
            return {"type": "lifespan.shutdown"}

        async def lifespan_send(message: dict[str, Any]) -> None:
            nonlocal lifespan_failed
            msg_type = message.get("type")
            if msg_type == "lifespan.startup.complete":
                startup_complete.set()
            elif msg_type == "lifespan.startup.failed":
                lifespan_failed = True
                startup_complete.set()
            elif msg_type in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                shutdown_complete.set()

        lifespan_task = asyncio.create_task(
            app({"type": "lifespan", "asgi": {"version": "3.0"}}, lifespan_receive, lifespan_send)
        )
        await startup_complete.wait()

        try:
            http_ctx = (event.get("requestContext") or {}).get("http") or {}
            method = (http_ctx.get("method") or event.get("httpMethod") or "GET").upper()
            raw_path = event.get("rawPath") or http_ctx.get("path") or event.get("path") or "/"
            raw_qs = event.get("rawQueryString") or ""
            raw_headers = event.get("headers") or {}
            asgi_headers = [
                (k.lower().encode("latin-1"), str(v).encode("latin-1"))
                for k, v in raw_headers.items()
                if v is not None
            ]
            raw_body = event.get("body") or ""
            if event.get("isBase64Encoded"):
                body_bytes = base64.b64decode(raw_body)
            elif isinstance(raw_body, bytes):
                body_bytes = raw_body
            else:
                body_bytes = str(raw_body).encode("utf-8")

            domain = (event.get("requestContext") or {}).get("domainName") or "localhost"
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method,
                "scheme": "https",
                "path": raw_path,
                "raw_path": raw_path.encode("utf-8"),
                "query_string": raw_qs.encode("latin-1"),
                "root_path": "",
                "headers": asgi_headers,
                "server": (domain, 443),
                "client": (http_ctx.get("sourceIp") or "127.0.0.1", 0),
            }

            body_sent = False
            response_complete = asyncio.Event()

            async def http_receive() -> dict[str, Any]:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                await response_complete.wait()
                return {"type": "http.disconnect"}

            status_code = 500
            resp_headers: dict[str, str] = {}
            chunks: list[bytes] = []

            async def http_send(message: dict[str, Any]) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    for k, v in message.get("headers") or []:
                        resp_headers[k.decode("latin-1").lower()] = v.decode("latin-1")
                elif message["type"] == "http.response.body":
                    chunk = message.get("body") or b""
                    if chunk:
                        chunks.append(chunk)
                    if not message.get("more_body", False):
                        response_complete.set()

            await app(scope, http_receive, http_send)
            response_complete.set()
            return {
                "statusCode": status_code,
                "headers": resp_headers,
                "body": b"".join(chunks).decode("utf-8", errors="replace"),
                "isBase64Encoded": False,
            }
        finally:
            if not lifespan_failed:
                shutdown_trigger.set()
                try:
                    await asyncio.wait_for(shutdown_complete.wait(), timeout=2.0)
                except TimeoutError:
                    logger.debug("ASGI lifespan shutdown timed out")
            if not lifespan_task.done():
                lifespan_task.cancel()
                try:
                    await lifespan_task
                except asyncio.CancelledError:
                    logger.debug("ASGI lifespan task cancelled")

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Lambda handler (stateless per DataDictionary pattern)
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    # Reject unauthenticated /mcp calls before any catalog or tool work.
    # A missing secret fails closed here; it must not fall through to Mangum.
    if mcp_auth.is_resource_metadata_path(event):
        return mcp_auth.resource_metadata_response(event)
    rejection, principal = mcp_auth.authenticate(event)
    if rejection is not None:
        return rejection
    # Per-tool scopes, before the body reaches FastMCP.
    refusal = mcp_auth.authorize_request(event, principal)
    if refusal is not None:
        return refusal

    # Load catalog on first (cold) invocation.  Subsequent warm invocations
    # reuse the module-level _catalog list unless reload_catalog is called.
    global _catalog, _dd_context
    if not _catalog:
        if _local_store is not None:
            _catalog = _local_store.load_all()
        else:
            _catalog = dynamodb_client.load_full_catalog()
        _dd_context = os.environ.get("DATA_DICTIONARY_CONTEXT", "API")
        logger.info("Cold start: loaded %d endpoints", len(_catalog))

    # Create a fresh ASGI app per invocation to avoid Mangum/lifespan reuse issues
    app = mcp.http_app(stateless_http=True)
    mangum_cls = Mangum
    if mangum_cls is None:
        try:
            from mangum import Mangum as _Mangum

            mangum_cls = _Mangum
        except ModuleNotFoundError:
            return _invoke_asgi_apigw_v2(app, event)
    return mangum_cls(app, lifespan="auto")(event, context)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolweave",
        description="ToolWeave MCP server (local SQLite/specs mode or cloud DynamoDB mode).",
    )
    parser.add_argument(
        "--specs-dir",
        default=os.environ.get("TOOLWEAVE_SPECS_DIR") or None,
        help="Directory of local OpenAPI/Swagger (.yaml/.yml/.json) specs to ingest.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("TOOLWEAVE_DB_PATH") or None,
        help="Path to local SQLite catalog database (enables local mode without DynamoDB/S3).",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "uds"),
        default="http",
        help="MCP transport mode (default: http).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host when --transport=http (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port when --transport=http (default: 8080).",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("TOOLWEAVE_SOCKET_PATH", "/tmp/toolweave.sock"),
        help="Unix Domain Socket path when --transport=uds.",
    )
    parser.add_argument(
        "--dry-run-pipeline",
        default=None,
        help="Validate a multi-step pipeline JSON file against the local catalog without making HTTP calls.",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="Allowlisted host for --dry-run-pipeline (may be repeated).",
    )
    parser.add_argument(
        "--lint-spec",
        default=None,
        help="Offline-lint an OpenAPI 3.x specification file (.json/.yaml/.yml) and print JSON report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    global _catalog, _dd_context

    args = build_arg_parser().parse_args(argv)
    _dd_context = os.environ.get("DATA_DICTIONARY_CONTEXT", "API")

    if args.lint_spec:
        try:
            spec = load_openapi_spec(args.lint_spec)
        except (OSError, ValueError, TypeError) as exc:
            report = {
                "ok": False,
                "error_count": 1,
                "warning_count": 0,
                "issues": [
                    {
                        "severity": "error",
                        "path": "$",
                        "message": f"Failed to load OpenAPI spec '{args.lint_spec}': {exc}",
                    }
                ],
            }
            print(json.dumps(report, indent=2))
            return 1
        report = lint_openapi_spec(spec)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    if args.dry_run_pipeline:
        db_path = args.db or ":memory:"
        configure_local_catalog(db_path=db_path, specs_dir=args.specs_dir)
        assert _local_store is not None
        raw_plan = json.loads(Path(args.dry_run_pipeline).read_text(encoding="utf-8"))
        if isinstance(raw_plan, list):
            raw_steps = raw_plan
            file_hosts: list[str] = []
        elif isinstance(raw_plan, dict) and isinstance(raw_plan.get("steps"), list):
            raw_steps = raw_plan["steps"]
            file_hosts = [str(h) for h in (raw_plan.get("allowed_hosts") or [])]
        else:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "pipeline JSON must be a list of steps or an object with 'steps'",
                    }
                )
            )
            return 1

        allowed_hosts = [*file_hosts, *(args.allow_host or [])]
        allowlist = LanAllowlistGuard(allowed_hosts) if allowed_hosts else None
        pipeline = ToolPipeline(_local_store, allowlist=allowlist)
        result = pipeline.execute(raw_steps, dry_run=True)
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.status in ("dry_run", "completed") else 1

    if args.specs_dir or args.db:
        db_path = args.db or "toolweave_catalog.db"
        summary = configure_local_catalog(db_path=db_path, specs_dir=args.specs_dir)
        logger.info(
            "Local catalog ready: %d endpoints in %s",
            summary["endpoint_count"],
            summary["db_path"],
        )
    else:
        _catalog = dynamodb_client.load_full_catalog()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return 0

    import uvicorn

    if args.transport == "uds":
        sock = Path(args.socket)
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        uvicorn.run(mcp.http_app(), uds=str(sock))
        return 0

    uvicorn.run(mcp.http_app(), host=args.host, port=args.port)
    return 0
