from __future__ import annotations

"""S3-event Lambda handler.

Triggered when an OpenAPI/Swagger file is uploaded to the ApiSpecsBucket.
Events arrive via EventBridge (detail-type "Object Created") — NOT the legacy
direct S3 notification format (Records array).

Parses the spec, enriches each endpoint entry via Bedrock Converse, then
writes to DynamoDB (ApiMetaTable + ApiCatalogTable), replacing any previous
data for the same S3 key (idempotent).
"""

import logging
import os
import re
from typing import Any
from urllib.parse import unquote_plus

import boto3

from . import dynamodb_client, endpoint_enricher
from .swagger_parser import api_id_from_s3_key, load_spec_from_bytes, parse_spec

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Process S3 ObjectCreated events from EventBridge."""

    # EventBridge S3 event format:
    # { "source": "aws.s3", "detail-type": "Object Created",
    #   "detail": { "bucket": {"name": "..."}, "object": {"key": "..."} } }
    if event.get("source") == "aws.s3" and "detail" in event:
        detail = event["detail"]
        bucket = detail.get("bucket", {}).get("name", "")
        key = unquote_plus(detail.get("object", {}).get("key", ""))
        if not bucket or not key:
            logger.warning("EventBridge event missing bucket/key: %s", event)
            return {"processed": 0, "errors": 1}
        try:
            _process_file(bucket, key)
            return {"processed": 1, "errors": 0}
        except Exception:
            logger.error(
                "Failed to process s3://%s/%s", bucket, key, exc_info=True
            )
            return {"processed": 0, "errors": 1}

    # Fallback: legacy direct S3 notification format (Records array).
    # Kept for local testing / manual invocations.
    processed = 0
    errors = 0
    for record in event.get("Records", []):
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name", "")
        key = unquote_plus(s3_info.get("object", {}).get("key", ""))
        if not bucket or not key:
            logger.warning("Skipping record with missing bucket/key: %s", record)
            continue
        try:
            _process_file(bucket, key)
            processed += 1
        except Exception:
            logger.error(
                "Failed to process s3://%s/%s", bucket, key, exc_info=True
            )
            errors += 1

    return {"processed": processed, "errors": errors}


def _process_file(bucket: str, key: str) -> None:
    logger.info("Processing s3://%s/%s", bucket, key)

    response = _s3.get_object(Bucket=bucket, Key=key)
    content: bytes = response["Body"].read()

    raw = load_spec_from_bytes(content, filename=key)
    api_id = api_id_from_s3_key(key)
    entries, base_url, api_title = parse_spec(raw, api_id=api_id)

    if not entries:
        logger.warning("No endpoints found in %s — skipping DynamoDB write.", key)
        return

    context_name = re.sub(r"[^a-zA-Z0-9]+", "", api_title)

    # Enrich entries via Bedrock before persisting
    entries = endpoint_enricher.enrich_endpoints(entries)

    dynamodb_client.delete_api_entries(api_id)

    dynamodb_client.write_api_meta(
        api_id=api_id,
        s3_key=key,
        api_title=api_title,
        base_url=base_url,
        context_name=context_name,
        endpoint_count=len(entries),
    )

    dynamodb_client.write_endpoint_batch(api_id, entries)

    logger.info(
        "Processed %d endpoints from s3://%s/%s (api_id=%s, title=%r)",
        len(entries),
        bucket,
        key,
        api_id,
        api_title,
    )
