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

import json
import logging
import os
from typing import Any

import boto3

from .models import EndpointEntry

logger = logging.getLogger(__name__)

# Haiku is fast and cheap for structured enrichment; override via env var if needed.
_ENRICHER_MODEL_ID = os.environ.get(
    "ENRICHER_MODEL_ID",
    "anthropic.claude-3-haiku-20240307-v1:0",
)
_BATCH_SIZE = 15  # endpoints per Bedrock call

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


def _client() -> Any:
    return boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


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


def _enrich_batch(
    entries: list[EndpointEntry],
    api_title: str,
    all_operation_ids: list[str],
) -> dict[str, dict]:
    """Single Bedrock Converse call for one batch.

    Returns a map of operation_id → enrichment dict.
    """
    user_msg = (
        f"API title: {api_title}\n"
        f"All operation IDs in this API (context only): "
        f"{', '.join(all_operation_ids)}\n\n"
        f"Enrich the following endpoints:\n"
        + json.dumps([_entry_to_dict(e) for e in entries], indent=2)
    )

    logger.info(
        "Calling Bedrock enricher for batch_size=%d (first_operation_id=%r)",
        len(entries),
        entries[0].operation_id if entries else None,
    )

    response = _client().converse(
        modelId=_ENRICHER_MODEL_ID,
        system=[{"text": _SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": 4096, "temperature": 0},
    )

    logger.info(
        "Received Bedrock enricher response for batch_size=%d",
        len(entries),
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
        "Parsing Bedrock enricher JSON payload for batch_size=%d",
        len(entries),
    )
    parsed: Any = json.loads(raw)
    if isinstance(parsed, dict):
        # Some model runs wrap the list under an object key.
        for key in ("items", "results", "endpoints", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break

    if not isinstance(parsed, list):
        raise ValueError("Enricher output must be a JSON array of endpoint objects")

    by_operation_id: dict[str, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        op_id = item.get("operation_id") or item.get("operationId")
        if not op_id:
            continue
        normalized = dict(item)
        normalized["operation_id"] = op_id
        by_operation_id[str(op_id)] = normalized
    logger.info(
        "Mapped Bedrock enrichment output to %d operation_id values (batch_size=%d)",
        len(by_operation_id),
        len(entries),
    )
    return by_operation_id


def enrich_endpoints(entries: list[EndpointEntry]) -> list[EndpointEntry]:
    """Enrich all entries in batches of up to _BATCH_SIZE.

    Enrichment failures for any batch are logged and skipped — the original
    entry is kept unchanged so the catalog write always succeeds.
    """
    if not entries:
        return entries

    api_title = entries[0].api_title
    all_op_ids = [e.operation_id for e in entries]
    enriched_map: dict[str, dict] = {}

    logger.info(
        "Starting enrichment across %d endpoints with batch_size=%d",
        len(entries),
        _BATCH_SIZE,
    )

    for i in range(0, len(entries), _BATCH_SIZE):
        batch = entries[i : i + _BATCH_SIZE]
        try:
            enriched_map.update(_enrich_batch(batch, api_title, all_op_ids))
            logger.info(
                "Enriched batch %d-%d for %r", i, i + len(batch), api_title
            )
        except Exception:
            logger.warning(
                "Enrichment failed for batch %d-%d of %r — storing unenriched",
                i,
                i + len(batch),
                api_title,
                exc_info=True,
            )

    result: list[EndpointEntry] = []
    single_fallback: dict | None = None
    if len(entries) == 1 and len(enriched_map) == 1:
        single_fallback = next(iter(enriched_map.values()))

    for entry in entries:
        data = enriched_map.get(entry.operation_id)
        if not data and single_fallback:
            logger.info(
                "Using single-entry enrichment fallback for %r",
                entry.operation_id,
            )
            data = single_fallback
        if not data:
            result.append(entry)
            continue
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

    return result
