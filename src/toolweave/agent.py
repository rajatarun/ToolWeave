from __future__ import annotations

"""Bedrock Converse agent that powers pre_tool.

The agent runs a tool-use loop using the AWS Bedrock Converse API.  For each
user message it:
  1. Calls search_endpoints  → finds candidate API endpoints
  2. Calls get_endpoint_details → inspects the required fields
  3. Calls lookup_field_metadata → enriches fields via DataDictionary
  4. Calls finalize_plan  → builds the PreToolResponse once values are known

If a required field cannot be extracted from the conversation the agent asks
the user ONE follow-up question (multi-turn, keyed by session_id).
"""

import asyncio
import json
import logging
import os
import re
from typing import Any

import boto3

from . import catalog_search, data_dictionary_client, observatory as _obs
from .models import EndpointEntry, FieldMapping, PreToolResponse

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Bedrock client (module-level; boto3 is thread-safe for separate clients)
# ---------------------------------------------------------------------------

_bedrock = boto3.client(
    "bedrock-runtime",
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID",
    "anthropic.claude-3-haiku-20240307-v1:0",
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_LOG_CONVERSE_MESSAGES = _env_flag("TOOLWEAVE_LOG_CONVERSE_MESSAGES", default=False)
_LOG_CONVERSE_RAW_RESPONSES = _env_flag(
    "TOOLWEAVE_LOG_CONVERSE_RAW_RESPONSES",
    default=False,
)
_LOG_PREVIEW_CHARS = int(os.environ.get("TOOLWEAVE_LOG_PREVIEW_CHARS", "4000"))

logger.info(
    "Agent logging config converse_messages=%s raw_responses=%s preview_chars=%s",
    _LOG_CONVERSE_MESSAGES,
    _LOG_CONVERSE_RAW_RESPONSES,
    _LOG_PREVIEW_CHARS,
)

# ---------------------------------------------------------------------------
# Session store (in-memory; warm-container lifetime)
# ---------------------------------------------------------------------------

_sessions: dict[str, list[dict[str, Any]]] = {}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are ToolWeave, an AI assistant that maps natural-language requests to REST API calls.\n\n"
    "Your workflow:\n"
    "1. Call search_endpoints with the user's intent to find candidate endpoints.\n"
    "2. Call get_endpoint_details on the best match to see required/optional fields.\n"
    "3. Call lookup_field_metadata to understand what each field means and expects.\n"
    "4. Extract field values from the conversation history using the metadata as context.\n"
    "5. Call finalize_plan with ALL extracted values and a list of any missing REQUIRED fields.\n\n"
    "Rules:\n"
    "- If every required field has a value, call finalize_plan immediately.\n"
    "- If one or more required fields are missing, ask the user ONE concise question "
    "  to obtain them, then stop (do NOT call finalize_plan yet).\n"
    "- Never invent or guess IDs, UUIDs, or numeric identifiers not stated by the user.\n"
    "- For path params such as {orderId}, only use values explicitly provided.\n"
    "- Prefer the most specific endpoint that matches the user's intent."
)

# ---------------------------------------------------------------------------
# Tool definitions passed to Bedrock Converse
# ---------------------------------------------------------------------------

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "search_endpoints",
            "description": (
                "Search the loaded API catalog by keyword. "
                "Returns up to 5 matching endpoints with their operation_id, method, path, and summary."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search terms"}},
                    "required": ["query"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_endpoint_details",
            "description": (
                "Retrieve the full schema for a specific endpoint: path params, query params, "
                "and request body fields including which are required."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "operation_id": {
                            "type": "string",
                            "description": "The operation_id returned by search_endpoints",
                        }
                    },
                    "required": ["operation_id"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "lookup_field_metadata",
            "description": (
                "Fetch DataDictionary metadata for a list of field names. "
                "Returns meaning, data type, examples, and constraints for each field."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "field_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Field names to look up",
                        },
                        "context": {
                            "type": "string",
                            "description": "API context name (e.g. 'OrdersAPI')",
                        },
                    },
                    "required": ["field_names", "context"],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "finalize_plan",
            "description": (
                "Call this when you have identified the endpoint and extracted all possible field "
                "values. Provide extracted values and a list of any still-missing required fields."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "operation_id": {"type": "string"},
                        "path_params": {
                            "type": "object",
                            "description": "Values for path parameters",
                        },
                        "query_params": {
                            "type": "object",
                            "description": "Values for query parameters",
                        },
                        "body": {
                            "type": "object",
                            "description": "Request body fields and values",
                        },
                        "missing_required": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Names of required fields whose values could not be extracted",
                        },
                    },
                    "required": ["operation_id"],
                }
            },
        }
    },
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_agent(
    user_message: str,
    session_id: str,
    catalog: list[EndpointEntry],
    dd_context: str,
) -> PreToolResponse:
    """Run the Bedrock Converse agent loop wrapped with InvocationWrapperAPI telemetry.

    Spans the full multi-turn loop; emits a policy decision (allow/review/block)
    and exports metrics to the shared OBSERVATORY_METRICS DynamoDB table.
    """

    async def _inner(**kwargs: Any) -> PreToolResponse:
        return await _run_agent_inner(**kwargs)

    result = await _obs.get_agent_wrapper().invoke(
        source="agent",
        model=BEDROCK_MODEL_ID,
        prompt=user_message,
        input_payload={"session_id": session_id, "catalog_size": len(catalog)},
        call=_inner,
        user_message=user_message,
        session_id=session_id,
        catalog=catalog,
        dd_context=dd_context,
    )

    logger.info(
        "Agent wrapper session_id=%s decision=%s cost_usd=%.4f "
        "hallucination_risk=%s composite_risk=%s",
        session_id,
        result.decision.action,
        result.span.cost_usd or 0.0,
        result.span.hallucination_risk_level,
        result.span.composite_risk_level,
    )

    if result.decision.action == "block":
        return PreToolResponse(
            session_id=session_id,
            status="error",
            error=f"Agent invocation blocked by observatory: {result.decision.reason}",
        )

    return result.output


async def _run_agent_inner(
    user_message: str,
    session_id: str,
    catalog: list[EndpointEntry],
    dd_context: str,
) -> PreToolResponse:
    """Internal Bedrock Converse agent loop for one user turn.

    Maintains conversation history in _sessions[session_id].
    Returns a PreToolResponse with status 'ready', 'needs_input', 'no_match', or 'error'.
    """
    logger.info("Agent run started session_id=%s", session_id)

    # Restore or create conversation history
    history: list[dict[str, Any]] = _sessions.get(session_id, [])
    history.append({"role": "user", "content": [{"text": user_message}]})

    _finalized: list[PreToolResponse] = []

    loop = asyncio.get_event_loop()

    for _iteration in range(20):  # safety cap on tool-use loops
        # ---- Bedrock Converse call (sync boto3 wrapped in executor) ----
        converse_kwargs: dict[str, Any] = {
            "modelId": BEDROCK_MODEL_ID,
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": history,
            "toolConfig": {"tools": AGENT_TOOLS},
            "inferenceConfig": {"maxTokens": 2048, "temperature": 0.0},
        }
        _log_converse_request(session_id, _iteration, history)

        try:
            kw = converse_kwargs

            async def _converse() -> dict[str, Any]:
                return await loop.run_in_executor(None, lambda: _bedrock.converse(**kw))

            model_result = await _obs.get_model_wrapper().invoke(
                source="model",
                model=BEDROCK_MODEL_ID,
                prompt=json.dumps(history[-1], default=str),
                input_payload={"modelId": kw.get("modelId", ""), "iteration": _iteration},
                call=_converse,
            )
            logger.info(
                "Model call session_id=%s iteration=%s decision=%s cost_usd=%.4f hallucination_risk=%s",
                session_id,
                _iteration,
                model_result.decision.action,
                model_result.span.cost_usd or 0.0,
                model_result.span.hallucination_risk_level,
            )
            response = model_result.output
        except Exception as exc:
            logger.exception(
                "Agent Bedrock converse failed session_id=%s iteration=%s",
                session_id,
                _iteration,
            )
            _sessions[session_id] = history
            return PreToolResponse(
                session_id=session_id,
                status="error",
                error=f"Bedrock error: {exc}",
            )
        _log_converse_response(session_id, _iteration, response)

        assistant_msg = response["output"]["message"]
        stop_reason = response["stopReason"]
        history.append(assistant_msg)

        # ---- end_turn → agent produced a text answer (question to user) ----
        if stop_reason == "end_turn":
            _sessions[session_id] = history
            text = _extract_text(assistant_msg)
            return PreToolResponse(
                session_id=session_id,
                status="needs_input",
                question=text or "Could you provide more information?",
            )

        # ---- tool_use → process each tool call ----
        if stop_reason == "tool_use":
            tool_results: list[dict[str, Any]] = []

            for block in assistant_msg.get("content", []):
                # Bedrock tool blocks are typically shaped as:
                # {"type":"toolUse","name":...,"input":...,"toolUseId":...}
                # Some SDK responses instead return:
                # {"toolUse":{"name":...,"input":...,"toolUseId":...}}
                # Normalize both formats so tool calls are never skipped.
                block_type = block.get("type") if isinstance(block, dict) else None
                if block_type not in (None, "toolUse"):
                    continue
                tool_block = (
                    block.get("toolUse")
                    if isinstance(block, dict) and "toolUse" in block
                    else block
                )
                if not isinstance(tool_block, dict):
                    continue

                tool_name = tool_block.get("name")
                tool_use_id = tool_block.get("toolUseId")
                if not tool_name or not tool_use_id:
                    continue
                tool_input: dict[str, Any] = tool_block.get("input", {})

                result_content = await _dispatch_tool(
                    tool_name, tool_input, catalog, dd_context, _finalized
                )

                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [{"text": json.dumps(result_content, default=str)}],
                        }
                    }
                )

            # Bedrock requires each message to include at least one ContentBlock.
            # In rare cases a model can return stopReason=tool_use without any
            # toolUse blocks, which would make tool_results empty and cause the
            # next converse call to fail validation.
            if not tool_results:
                _sessions[session_id] = history
                text = _extract_text(assistant_msg)
                if text:
                    return PreToolResponse(
                        session_id=session_id,
                        status="needs_input",
                        question=text,
                    )
                return PreToolResponse(
                    session_id=session_id,
                    status="error",
                    error=(
                        "Model returned stopReason=tool_use without any tool requests; "
                        "unable to continue safely."
                    ),
                )

            history.append({"role": "user", "content": tool_results})

            # If finalize_plan was called, return the result immediately
            if _finalized:
                _sessions[session_id] = history
                logger.info(
                    "Agent finalized plan session_id=%s status=%s operation_id=%s",
                    session_id,
                    _finalized[0].status,
                    _finalized[0].operation_id,
                )
                return _finalized[0]

        else:
            # Unexpected stop reason
            _sessions[session_id] = history
            return PreToolResponse(
                session_id=session_id,
                status="error",
                error=f"Unexpected stop_reason: {stop_reason}",
            )

    _sessions[session_id] = history
    return PreToolResponse(
        session_id=session_id,
        status="error",
        error="Agent exceeded maximum tool-use iterations.",
    )


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


async def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    catalog: list[EndpointEntry],
    dd_context: str,
    finalized: list[PreToolResponse],
) -> Any:
    logger.info(
        "Agent tool invoked tool=%s input_keys=%s",
        tool_name,
        sorted(tool_input.keys()),
    )
    if tool_name == "search_endpoints":
        return _tool_search_endpoints(tool_input.get("query", ""), catalog)

    if tool_name == "get_endpoint_details":
        return _tool_get_endpoint_details(tool_input.get("operation_id", ""), catalog)

    if tool_name == "lookup_field_metadata":
        field_names: list[str] = tool_input.get("field_names", [])
        context: str = tool_input.get("context", dd_context)
        return await _tool_lookup_field_metadata(field_names, context)

    if tool_name == "finalize_plan":
        result = _tool_finalize_plan(tool_input, catalog)
        finalized.append(result)
        return {"status": "plan_finalized", "session_id": result.session_id}

    return {"error": f"Unknown tool: {tool_name}"}


# ---------------------------------------------------------------------------
# Individual tool implementations
# ---------------------------------------------------------------------------


def _tool_search_endpoints(query: str, catalog: list[EndpointEntry]) -> list[dict[str, Any]]:
    results = catalog_search.search(query, catalog, top_k=5)
    if not results:
        return [{"message": "No matching endpoints found. Try different keywords."}]
    return results


def _tool_get_endpoint_details(
    operation_id: str,
    catalog: list[EndpointEntry],
) -> dict[str, Any]:
    entry = next((e for e in catalog if e.operation_id == operation_id), None)
    if not entry:
        return {"error": f"operation_id '{operation_id}' not found in catalog."}

    return {
        "operation_id": entry.operation_id,
        "method": entry.method,
        "path": entry.path,
        "summary": entry.summary,
        "base_url": entry.base_url,
        "path_params": [p.model_dump() for p in entry.parameters if p.location == "path"],
        "query_params": [p.model_dump() for p in entry.parameters if p.location == "query"],
        "header_params": [p.model_dump() for p in entry.parameters if p.location == "header"],
        "body_fields": [f.model_dump() for f in entry.body_fields],
    }


async def _tool_lookup_field_metadata(
    field_names: list[str],
    context: str,
) -> dict[str, Any]:
    metadata = await data_dictionary_client.fetch_field_metadata(field_names, context)
    return {
        name: meta.model_dump() if meta else None
        for name, meta in metadata.items()
    }


def _tool_finalize_plan(
    tool_input: dict[str, Any],
    catalog: list[EndpointEntry],
) -> PreToolResponse:
    import uuid

    operation_id: str = tool_input.get("operation_id", "")
    path_params: dict[str, Any] = tool_input.get("path_params") or {}
    query_params: dict[str, Any] = tool_input.get("query_params") or {}
    body: dict[str, Any] | None = tool_input.get("body") or None
    missing_required: list[str] = tool_input.get("missing_required") or []

    entry = next((e for e in catalog if e.operation_id == operation_id), None)
    if not entry:
        return PreToolResponse(
            session_id=str(uuid.uuid4()),
            status="error",
            error=f"operation_id '{operation_id}' not found when finalizing plan.",
        )

    # Resolve path template
    resolved_path = entry.path
    for key, value in path_params.items():
        resolved_path = resolved_path.replace(f"{{{key}}}", str(value))

    # Build field mappings for traceability
    field_mappings: list[FieldMapping] = []
    for param in entry.parameters:
        if param.location == "path":
            v = path_params.get(param.name)
        elif param.location == "query":
            v = query_params.get(param.name)
        else:
            continue
        field_mappings.append(
            FieldMapping(
                field_name=param.name,
                location=param.location,
                extracted_value=v,
                value_present=v is not None,
            )
        )
    for bf in entry.body_fields:
        # Support dot-notation for nested body fields
        parts = bf.name.split(".")
        v: Any = body
        for part in parts:
            v = v.get(part) if isinstance(v, dict) else None
        field_mappings.append(
            FieldMapping(
                field_name=bf.name,
                location="body",
                extracted_value=v,
                value_present=v is not None,
            )
        )

    execution_type = "immediate" if entry.method == "GET" else "proposal"

    return PreToolResponse(
        session_id=str(uuid.uuid4()),
        status="ready",
        matched_endpoint=f"{entry.method} {entry.path}",
        operation_id=operation_id,
        execution_type=execution_type,  # type: ignore[arg-type]
        method=entry.method,
        path=resolved_path,
        base_url=entry.base_url,
        path_params=path_params,
        query_params=query_params,
        body=body,
        field_mappings=field_mappings,
        missing_required_fields=missing_required,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(message: dict[str, Any]) -> str:
    for block in message.get("content", []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and "text" in block:
            return block["text"]
        if "text" in block and block.get("type") is None:
            return block["text"]
    return ""


def _preview_json(value: Any) -> str:
    text = json.dumps(value, default=str)
    if len(text) > _LOG_PREVIEW_CHARS:
        return f"{text[:_LOG_PREVIEW_CHARS]}... (truncated)"
    return text


def _log_converse_request(session_id: str, iteration: int, history: list[dict[str, Any]]) -> None:
    if not _LOG_CONVERSE_MESSAGES:
        return
    logger.info(
        "Agent converse request session_id=%s iteration=%s history=%s",
        session_id,
        iteration,
        _preview_json(history),
    )


def _log_converse_response(session_id: str, iteration: int, response: dict[str, Any]) -> None:
    stop_reason = response.get("stopReason", "unknown")
    if _LOG_CONVERSE_MESSAGES:
        assistant_msg = response.get("output", {}).get("message", {})
        logger.info(
            "Agent converse response session_id=%s iteration=%s stop_reason=%s message=%s",
            session_id,
            iteration,
            stop_reason,
            _preview_json(assistant_msg),
        )
    if _LOG_CONVERSE_RAW_RESPONSES:
        logger.info(
            "Agent converse raw response session_id=%s iteration=%s response=%s",
            session_id,
            iteration,
            _preview_json(response),
        )
