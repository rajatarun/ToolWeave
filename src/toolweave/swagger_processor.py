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
from .swagger_parser import (
    api_id_from_s3_key,
    load_spec_from_bytes,
    parse_spec,
    server_is_templated,
)

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
            if event.get("detail-type") == "Object Deleted":
                _forget_file(key)
            else:
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


def _forget_file(key: str) -> None:
    """Drop an API from the catalog when its spec is removed from the bucket.

    Withdrawing a spec was only ever half an operation: the processor listened
    for `Object Created` alone, so deleting the object left every one of its
    endpoints in DynamoDB and the MCP server went on offering an API nobody
    had published for as long as the table lived. Nothing failed, and the
    catalog is the only place that would have shown it.
    """
    api_id = api_id_from_s3_key(key)
    logger.info("Forgetting api_id=%s (spec %s was removed from the bucket)", api_id, key)
    dynamodb_client.delete_api_entries(api_id)
    dynamodb_client.delete_api_meta(api_id)


def _process_file(bucket: str, key: str) -> None:
    logger.info("Processing s3://%s/%s", bucket, key)

    logger.info("Fetching OpenAPI object from S3 (bucket=%s, key=%s)", bucket, key)
    response = _s3.get_object(Bucket=bucket, Key=key)
    content: bytes = response["Body"].read()
    logger.info(
        "Fetched %d bytes from s3://%s/%s",
        len(content),
        bucket,
        key,
    )

    logger.info("Loading OpenAPI document for key=%s", key)
    raw = load_spec_from_bytes(content, filename=key)
    api_id = api_id_from_s3_key(key)
    logger.info("Derived api_id=%s from key=%s", api_id, key)

    logger.info("Parsing endpoints from spec for api_id=%s", api_id)
    entries, base_url, api_title = parse_spec(raw, api_id=api_id)
    logger.info(
        "Parsed spec for api_id=%s (title=%r, base_url=%r, endpoints=%d)",
        api_id,
        api_title,
        base_url,
        len(entries),
    )

    if not entries:
        logger.warning("No endpoints found in %s — skipping DynamoDB write.", key)
        return

    if not base_url or server_is_templated(raw):
        # Two different ways to have no usable address, refused together.
        #
        # An unresolved template (`{apiBaseUrl}`) would be cataloged as a base
        # URL with a placeholder in it. The subtler one is a template that
        # *does* resolve: both sibling specs ship a variable default of
        # `https://example.execute-api.us-east-1.amazonaws.com/prod`, which is
        # a syntactically valid URL pointing at a host that does not exist. A
        # spec uploaded by hand rather than published would catalog every one
        # of its endpoints against that address, and the agent would plan
        # calls the executor can only fail.
        #
        # `publish_specs.py` writes a literal server, so a template here means
        # the document did not come through it.
        logger.error(
            "Refusing to catalog %s: its servers[0].url is missing or still "
            "carries an OAS3 variable (%r). Publish it with "
            "scripts/publish_specs.py, which substitutes the real URL from the "
            "sibling stack's output; uploading the spec verbatim would point "
            "every endpoint at the placeholder host in its variable default.",
            key,
            base_url,
        )
        return

    context_name = re.sub(r"[^a-zA-Z0-9]+", "", api_title)
    logger.info(
        "Computed context_name=%r for api_id=%s",
        context_name,
        api_id,
    )

    # Enrich entries via Bedrock before persisting
    logger.info("Starting endpoint enrichment for api_id=%s", api_id)
    entries = endpoint_enricher.enrich_endpoints(entries)
    enriched_count = sum(
        1
        for entry in entries
        if entry.agent_hint
        or entry.example_prompts
        or entry.parameter_notes
        or entry.response_hint
        or entry.idempotent is not None
    )
    logger.info(
        "Completed endpoint enrichment for api_id=%s (endpoints=%d, enriched=%d, skipped=%d)",
        api_id,
        len(entries),
        enriched_count,
        len(entries) - enriched_count,
    )
    if enriched_count == 0:
        logger.warning(
            "No endpoint metadata was enriched for api_id=%s. "
            "Inspect endpoint_enricher logs for Bedrock call failures/timeouts.",
            api_id,
        )

    logger.info("Deleting existing DynamoDB records for api_id=%s", api_id)
    dynamodb_client.delete_api_entries(api_id)

    logger.info("Writing API metadata row for api_id=%s", api_id)
    dynamodb_client.write_api_meta(
        api_id=api_id,
        s3_key=key,
        api_title=api_title,
        base_url=base_url,
        context_name=context_name,
        endpoint_count=len(entries),
    )

    logger.info(
        "Writing %d endpoint rows to ApiCatalogTable for api_id=%s",
        len(entries),
        api_id,
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
