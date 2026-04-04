from __future__ import annotations

"""Enrich EndpointEntry catalog entries using a Bedrock Converse call.

Called once per Swagger upload (inside SwaggerProcessorFunction) to add
agent-friendly metadata that reduces hallucinations during API call planning:

  agent_hint       — when to use this endpoint vs similar ones in the API
  example_prompts  — sample NL queries that map to this endpoint
  parameter_notes  — format/validation hints per parameter/field name
  response_hint    — key fields returned by this endpoint
  idempotent       — whether calling twice has no additional side-effect
"""

import concurrent.futures
import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config

from .models import EndpointEntry

logger = logging.getLogger(__name__)

# Haiku is fast and cheap for structured enrichment; override via env var if needed.
_ENRICHER_MODEL_ID = os.environ.get(
    "ENRICHER_MODEL_ID",
    "anthropic.claude-3-haiku-20240307-v1:0",
)
_CONNECT_TIMEOUT_SECONDS = int(os.environ.get("ENRICHER_CONNECT_TIMEOUT_SECONDS", "5"))
_READ_TIMEOUT_SECONDS = int(os.environ.get("ENRICHER_READ_TIMEOUT_SECONDS", "20"))
_MAX_ATTEMPTS = int(os.environ.get("ENRICHER_MAX_ATTEMPTS", "2"))
# Hard wall-clock limit per individual endpoint enrichment call.
_PER_ENDPOINT_TIMEOUT_SECONDS = int(
    os.environ.get("ENRICHER_PER_ENDPOINT_TIMEOUT_SECONDS", "60")
)
# Hard wall-clock limit for the entire enrich_endpoints() call.
# SwaggerProcessorFunction has a 300 s Lambda timeout; we reserve ~60 s for
# DynamoDB writes, leaving the rest for enrichment.
_TOTAL_ENRICHMENT_TIMEOUT_SECONDS = int(
    os.environ.get("ENRICHER_TOTAL_TIMEOUT_SECONDS", "220")
)

_SYSTEM_PROMPT = """\
You are an API catalog enricher. Your output is stored in a vector database \
and used by an AI planning agent to select and invoke REST API endpoints. \
Your goal is to add metadata that lets the agent pick the right endpoint, \
supply correctly-formatted values, and avoid hallucinations.

For each endpoint in the input JSON array, produce one enriched object with:

operation_id  (string) — echo back unchanged, used as the key.

agent_hint  (string, 1-3 sentences) — explain WHEN to call this endpoint, \
how it differs from similar endpoints in this same API, and any critical \
ordering requirements or side-effects the agent must know before calling it.

example_prompts  (array of 3-5 strings) — realistic natural-language phrases \
a user might say that should trigger THIS endpoint and not any other. \
Be specific enough that a keyword-match would prefer this endpoint.

parameter_notes  (object) — map of every path parameter, required query \
parameter, and required request-body field name to a concise string \
describing: value format, enum list, length/range limits, ID prefixes \
(e.g. "ORD-{digits}"), date formats, or casing rules. \
Omit parameters with no meaningful constraints.

response_hint  (string, 1-2 sentences) — what key fields the agent \
should extract from the response (e.g. IDs, status values, URLs, counts).

idempotent  (boolean or null) — true for GET/HEAD/PUT-by-id; \
false for POST that creates resources or DELETE that removes them; \
null when genuinely unknown.

Return ONLY a valid JSON array of enriched objects in the same order as \
the input. No prose, no markdown fences, no trailing commas.\
"""


# Cached at module level so we pay the client initialisation cost once per
# Lambda container, not once per batch call (which could trigger credential
# resolution or connection-pool setup on every invocation).
_BEDROCK_CLIENT: Any = None


def _client() -> Any:
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        logger.info(
            "Initialising Bedrock client (connect_timeout=%ss, read_timeout=%ss, max_attempts=%d)",
            _CONNECT_TIMEOUT_SECONDS,
            _READ_TIMEOUT_SECONDS,
            _MAX_ATTEMPTS,
        )
        _BEDROCK_CLIENT = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(
                connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                read_timeout=_READ_TIMEOUT_SECONDS,
                retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
            ),
        )
    return _BEDROCK_CLIENT


def _entry_to_dict(entry: EndpointEntry) -> dict:
    """Minimal representation forwarded to the enrichment prompt."""
    return {
        "operation_id": entry.operation_id,
        "method": entry.method,
        "path": entry.path,
        "summary": entry.summary,
        "description": entry.description[:500] if entry.description else "",
        "tags": entry.tags,
        "parameters": [
            {
                "name": p.name,
                "in": p.location,
                "required": p.required,
                "type": p.data_type,
                "description": p.description[:200] if p.description else "",
            }
            for p in entry.parameters
        ],
        "body_fields": [
            {
                "name": f.name,
                "required": f.required,
                "type": f.data_type,
                "description": f.description[:200] if f.description else "",
            }
            for f in entry.body_fields
        ],
    }


def _enrich_one(
    entry: EndpointEntry,
    api_title: str,
    all_operation_ids: list[str],
) -> dict:
    """Single Bedrock Converse call for one endpoint.

    Returns the enrichment dict for that endpoint.
    """
    user_msg = (
        f"API title: {api_title}\n"
        f"All operation IDs in this API (context only): "
        f"{', '.join(all_operation_ids)}\n\n"
        f"Enrich the following endpoints:\n"
        + json.dumps([_entry_to_dict(entry)], indent=2)
    )

    logger.info(
        "Calling Bedrock enricher for operation_id=%r "
        "(connect_timeout=%ss, read_timeout=%ss, max_attempts=%d)",
        entry.operation_id,
        _CONNECT_TIMEOUT_SECONDS,
        _READ_TIMEOUT_SECONDS,
        _MAX_ATTEMPTS,
    )

    response = _client().converse(
        modelId=_ENRICHER_MODEL_ID,
        system=[{"text": _SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0},
    )

    logger.info(
        "Received Bedrock enricher response for operation_id=%r",
        entry.operation_id,
    )

    content_blocks = response["output"]["message"].get("content", [])
    raw = "".join(
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("text")
    ).strip()

    # Strip markdown code fences if the model wraps its output
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    logger.info(
        "Parsing Bedrock enricher JSON payload for operation_id=%r",
        entry.operation_id,
    )
    parsed: Any = json.loads(raw)
    if isinstance(parsed, dict):
        # Some model runs wrap the list under an object key.
        for key in ("items", "results", "endpoints", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        # Single-object response — treat it as the enrichment directly.
        if isinstance(parsed, dict):
            return parsed

    if isinstance(parsed, list) and parsed:
        return parsed[0] if isinstance(parsed[0], dict) else {}

    raise ValueError(
        f"Enricher output for {entry.operation_id!r} was not a usable object or array"
    )


def _enrich_one_with_timeout(
    entry: EndpointEntry,
    api_title: str,
    all_operation_ids: list[str],
) -> dict | None:
    """Run _enrich_one in a fresh thread with a hard per-endpoint wall-clock limit.

    A dedicated executor is created per call and shut down with wait=False on
    timeout so a hung Bedrock thread does not hold the worker slot and block
    the next endpoint.  Returns the enrichment dict on success, or None if the
    call timed out or raised an exception (both cases are logged).
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_enrich_one, entry, api_title, all_operation_ids)
    try:
        result = future.result(timeout=_PER_ENDPOINT_TIMEOUT_SECONDS)
        executor.shutdown(wait=False)
        return result
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Enrichment timed out for operation_id=%r after %ds — "
            "skipping enrichment for this endpoint",
            entry.operation_id,
            _PER_ENDPOINT_TIMEOUT_SECONDS,
        )
        executor.shutdown(wait=False)
        return None
    except Exception:
        logger.warning(
            "Enrichment failed for operation_id=%r — skipping enrichment for this endpoint",
            entry.operation_id,
            exc_info=True,
        )
        executor.shutdown(wait=False)
        return None


def _run_enrichment_loop(
    entries: list[EndpointEntry],
) -> list[EndpointEntry]:
    """Per-endpoint enrichment loop — runs inside a thread so the total
    operation can be time-bounded externally.  Each individual endpoint call is
    additionally bounded by _PER_ENDPOINT_TIMEOUT_SECONDS with its own thread
    so a single hung Bedrock call does not block subsequent endpoints.
    """
    api_title = entries[0].api_title
    all_op_ids = [e.operation_id for e in entries]
    total = len(entries)
    succeeded = 0
    skipped = 0

    logger.info(
        "Starting per-endpoint enrichment for %d endpoints "
        "(per_endpoint_timeout=%ss, total_timeout=%ss)",
        total,
        _PER_ENDPOINT_TIMEOUT_SECONDS,
        _TOTAL_ENRICHMENT_TIMEOUT_SECONDS,
    )

    result: list[EndpointEntry] = []
    for idx, entry in enumerate(entries, start=1):
        logger.info(
            "Enriching endpoint %d/%d operation_id=%r",
            idx,
            total,
            entry.operation_id,
        )
        data = _enrich_one_with_timeout(entry, api_title, all_op_ids)

        if data is None:
            skipped += 1
            result.append(entry)
        else:
            succeeded += 1
            result.append(
                entry.model_copy(
                    update={
                        "agent_hint": data.get("agent_hint", ""),
                        "example_prompts": data.get("example_prompts", []),
                        "parameter_notes": data.get("parameter_notes", {}),
                        "response_hint": data.get("response_hint", ""),
                        "idempotent": data.get("idempotent"),
                    }
                )
            )
        logger.info(
            "Enrichment progress: %d/%d done (succeeded=%d, skipped=%d)",
            idx,
            total,
            succeeded,
            skipped,
        )

    logger.info(
        "Per-endpoint enrichment complete: total=%d succeeded=%d skipped=%d",
        total,
        succeeded,
        skipped,
    )
    return result


def enrich_endpoints(entries: list[EndpointEntry]) -> list[EndpointEntry]:
    """Enrich all entries one endpoint at a time.

    Each endpoint gets its own Bedrock call bounded by
    _PER_ENDPOINT_TIMEOUT_SECONDS.  The entire loop is additionally bounded by
    _TOTAL_ENRICHMENT_TIMEOUT_SECONDS so the Lambda never silently hangs
    regardless of how many endpoints are present.

    On total timeout the function logs which endpoint was in-flight, how many
    were already enriched, and returns a mixed list (enriched entries so far +
    unenriched remainder) so the catalog write always succeeds.
    """
    if not entries:
        return entries

    logger.info(
        "Starting endpoint enrichment: total=%d "
        "(per_endpoint_timeout=%ss, total_timeout=%ss)",
        len(entries),
        _PER_ENDPOINT_TIMEOUT_SECONDS,
        _TOTAL_ENRICHMENT_TIMEOUT_SECONDS,
    )
    future = concurrent.futures.ThreadPoolExecutor(max_workers=1).submit(
        _run_enrichment_loop, entries
    )
    try:
        return future.result(timeout=_TOTAL_ENRICHMENT_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Total enrichment timeout (%ds) exceeded with %d endpoints — "
            "returning partially enriched list so catalog write can proceed",
            _TOTAL_ENRICHMENT_TIMEOUT_SECONDS,
            len(entries),
        )
        return entries
    except Exception:
        logger.warning(
            "Endpoint enrichment raised an unexpected error — "
            "returning %d unenriched entries",
            len(entries),
            exc_info=True,
        )
        return entries
