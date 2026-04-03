from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key

from .models import EndpointEntry, EndpointParameter, RequestBodyField

_dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))

CATALOG_TABLE_NAME = os.environ.get("CATALOG_TABLE_NAME", "toolweave-ApiCatalogTable")
META_TABLE_NAME = os.environ.get("META_TABLE_NAME", "toolweave-ApiMetaTable")
PROPOSALS_TABLE_NAME = os.environ.get("PROPOSALS_TABLE_NAME", "toolweave-ProposalsTable")

_catalog_table = _dynamodb.Table(CATALOG_TABLE_NAME)
_meta_table = _dynamodb.Table(META_TABLE_NAME)
_proposals_table = _dynamodb.Table(PROPOSALS_TABLE_NAME)


# ---------------------------------------------------------------------------
# Catalog load (called at Lambda cold start)
# ---------------------------------------------------------------------------


def load_full_catalog() -> list[EndpointEntry]:
    """Scan the entire ApiCatalogTable and return all entries as EndpointEntry objects."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {}
    while True:
        response = _catalog_table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    entries: list[EndpointEntry] = []
    for item in items:
        try:
            entries.append(_item_to_entry(item))
        except Exception:
            pass  # skip malformed items
    return entries


# ---------------------------------------------------------------------------
# Catalog write (called by SwaggerProcessorFunction)
# ---------------------------------------------------------------------------


def write_api_meta(
    api_id: str,
    s3_key: str,
    api_title: str,
    base_url: str,
    context_name: str,
    endpoint_count: int,
) -> None:
    import datetime

    _meta_table.put_item(
        Item={
            "api_id": api_id,
            "source_s3_key": s3_key,
            "api_title": api_title,
            "base_url": base_url,
            "context_name": context_name,
            "endpoint_count": endpoint_count,
            "parsed_at": datetime.datetime.utcnow().isoformat() + "Z",
        }
    )


def write_endpoint_batch(api_id: str, entries: list[EndpointEntry]) -> None:
    """Batch-write endpoint entries to ApiCatalogTable (25 items per request)."""
    for i in range(0, len(entries), 25):
        chunk = entries[i : i + 25]
        with _catalog_table.batch_writer() as batch:
            for entry in chunk:
                batch.put_item(Item=_entry_to_item(entry, api_id))


def delete_api_entries(api_id: str) -> None:
    """Delete all catalog entries for a given api_id (idempotent update)."""
    # Query by PK (api_id is the partition key)
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("api_id").eq(api_id),
        "ProjectionExpression": "api_id, operation_id",
    }
    while True:
        response = _catalog_table.query(**kwargs)
        items = response.get("Items", [])
        if items:
            with _catalog_table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(
                        Key={"api_id": item["api_id"], "operation_id": item["operation_id"]}
                    )
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key


# ---------------------------------------------------------------------------
# Proposal persistence (cross-invocation, like DataDictionary pattern)
# ---------------------------------------------------------------------------


def save_proposal(proposal_id: str, data: dict[str, Any], ttl_seconds: int = 3600) -> None:
    _proposals_table.put_item(
        Item={
            "proposal_id": proposal_id,
            "_expires_at": int(time.time()) + ttl_seconds,
            **data,
        }
    )


def get_proposal_data(proposal_id: str) -> Optional[dict[str, Any]]:
    response = _proposals_table.get_item(Key={"proposal_id": proposal_id})
    item = response.get("Item")
    if not item:
        return None
    return {k: v for k, v in item.items() if k not in ("proposal_id", "_expires_at")}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _entry_to_item(entry: EndpointEntry, api_id: str) -> dict[str, Any]:
    return {
        "api_id": api_id,
        "operation_id": entry.operation_id or f"{entry.method}_{entry.path}",
        "method": entry.method,
        "path": entry.path,
        "summary": entry.summary,
        "description": entry.description,
        "tags": entry.tags,
        "parameters": json.dumps([p.model_dump() for p in entry.parameters]),
        "body_fields": json.dumps([f.model_dump() for f in entry.body_fields]),
        "content_type": entry.content_type,
        "base_url": entry.base_url,
        "api_title": entry.api_title,
    }


def _item_to_entry(item: dict[str, Any]) -> EndpointEntry:
    parameters = [
        EndpointParameter(**p)
        for p in json.loads(item.get("parameters") or "[]")
    ]
    body_fields = [
        RequestBodyField(**f)
        for f in json.loads(item.get("body_fields") or "[]")
    ]
    return EndpointEntry(
        path=item.get("path", ""),
        method=item.get("method", "GET"),
        operation_id=item.get("operation_id", ""),
        summary=item.get("summary", ""),
        description=item.get("description", ""),
        tags=item.get("tags") or [],
        parameters=parameters,
        body_fields=body_fields,
        content_type=item.get("content_type", "application/json"),
        base_url=item.get("base_url", ""),
        api_id=item.get("api_id", ""),
        api_title=item.get("api_title", ""),
    )
