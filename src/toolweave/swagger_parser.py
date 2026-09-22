from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx
import yaml

from .models import EndpointEntry, EndpointParameter, RequestBodyField

_EXTERNAL_API_AUTH_HEADER = {"Authorization": "Bearer 123"}

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def load_spec_from_bytes(content: bytes, filename: str = "") -> dict[str, Any]:
    """Parse raw bytes (JSON or YAML) into a spec dict."""
    text = content.decode("utf-8", errors="replace")
    if filename.endswith(".json") or text.lstrip().startswith("{"):
        return json.loads(text)
    return yaml.safe_load(text)


async def load_spec_from_url(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_EXTERNAL_API_AUTH_HEADER)
        resp.raise_for_status()
        return load_spec_from_bytes(resp.content, url)


async def load_spec_from_path(path: str) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return load_spec_from_bytes(fh.read(), path)


def parse_spec(
    raw: dict[str, Any],
    api_id: str = "",
) -> tuple[list[EndpointEntry], str, str]:
    """Parse an OpenAPI 2 or 3 spec dict.

    Returns:
        (entries, base_url, api_title)
    """
    info = raw.get("info", {})
    api_title = info.get("title", "API")
    version = _detect_version(raw)

    if version == 2:
        base_url = _oas2_base_url(raw)
    else:
        base_url = _oas3_base_url(raw)

    entries: list[EndpointEntry] = []
    paths: dict[str, Any] = raw.get("paths", {})
    op_counter: dict[str, int] = {}

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            op_id = operation.get("operationId", "")
            if not op_id:
                op_id = _generate_operation_id(method, path_str, op_counter)

            parameters = _parse_parameters(
                operation.get("parameters", []) + path_item.get("parameters", []),
                raw,
            )

            body_fields: list[RequestBodyField] = []
            if version == 2:
                body_fields = _oas2_body_fields(operation, raw)
            else:
                body_fields = _oas3_body_fields(operation, raw)

            entries.append(
                EndpointEntry(
                    path=path_str,
                    method=method.upper(),
                    operation_id=op_id,
                    summary=operation.get("summary", ""),
                    description=operation.get("description", ""),
                    tags=operation.get("tags", []),
                    parameters=parameters,
                    body_fields=body_fields,
                    content_type=_detect_content_type(operation, version),
                    base_url=base_url,
                    api_id=api_id or _slug(api_title),
                    api_title=api_title,
                )
            )

    return entries, base_url, api_title


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def _detect_version(raw: dict) -> int:
    if "openapi" in raw:
        return 3
    if "swagger" in raw:
        return 2
    return 3


# ---------------------------------------------------------------------------
# Base URL extraction
# ---------------------------------------------------------------------------


def _oas2_base_url(raw: dict) -> str:
    host = raw.get("host", "")
    base = raw.get("basePath", "")
    schemes = raw.get("schemes", ["https"])
    scheme = schemes[0] if schemes else "https"
    if host:
        return f"{scheme}://{host}{base}"
    return base or ""


# An OAS3 `servers[].url` may be a template: `"{apiBaseUrl}"` with the real
# value supplied per deployment. Both sibling specs this catalog serves are
# written that way on purpose -- they instruct callers to resolve the host
# from a CloudFormation stack output rather than hardcode it.
_SERVER_TEMPLATE = re.compile(r"\{([^{}]+)\}")


def server_is_templated(raw: dict) -> bool:
    """True when `servers[0].url` carries an OAS3 variable.

    A spec published by `scripts/publish_specs.py` carries a literal URL, so a
    template means the document did not come through the publisher and its
    server is whatever placeholder the author shipped -- which for both
    sibling specs resolves to a host that does not exist.
    """
    servers = raw.get("servers", [])
    if not servers or not isinstance(servers[0], dict):
        return False
    return bool(_SERVER_TEMPLATE.search(str(servers[0].get("url", ""))))


def _oas3_base_url(raw: dict) -> str:
    """Resolve `servers[0].url`, substituting OAS3 server variables.

    Returns "" when the URL cannot be fully resolved. That is deliberate: an
    unsubstituted `{apiBaseUrl}` stored as a base URL builds request targets
    like `{apiBaseUrl}/admin/articles`, and every execution then fails with a
    message about a malformed URL rather than about a spec that was never
    given a server. The caller refuses to catalog an API with no base URL.
    """
    servers = raw.get("servers", [])
    if not servers or not isinstance(servers[0], dict):
        return ""

    url = servers[0].get("url", "")
    if not url:
        return ""

    variables = servers[0].get("variables") or {}

    def _substitute(match: re.Match) -> str:
        spec = variables.get(match.group(1))
        if isinstance(spec, dict):
            default = spec.get("default", "")
            if isinstance(default, str) and default:
                return default
        return match.group(0)  # no default -- leave the placeholder in place

    url = _SERVER_TEMPLATE.sub(_substitute, url)
    if _SERVER_TEMPLATE.search(url):
        return ""  # still templated: unresolvable, not a URL
    return url


# ---------------------------------------------------------------------------
# Parameter parsing
# ---------------------------------------------------------------------------


def _parse_parameters(
    params: list[Any],
    raw: dict,
) -> list[EndpointParameter]:
    seen: set[str] = set()
    result: list[EndpointParameter] = []
    for p in params:
        p = _resolve_ref(p, raw)
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        loc = p.get("in", "query")
        if loc not in ("path", "query", "header", "cookie"):
            continue
        key = f"{loc}:{name}"
        if key in seen:
            continue
        seen.add(key)

        schema = p.get("schema", p)
        result.append(
            EndpointParameter(
                name=name,
                location=loc,  # type: ignore[arg-type]
                required=p.get("required", loc == "path"),
                data_type=schema.get("type", "string"),
                description=p.get("description", ""),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Body fields
# ---------------------------------------------------------------------------


def _oas2_body_fields(operation: dict, raw: dict) -> list[RequestBodyField]:
    for p in operation.get("parameters", []):
        p = _resolve_ref(p, raw)
        if isinstance(p, dict) and p.get("in") == "body":
            schema = _resolve_ref(p.get("schema", {}), raw)
            required_set = set(schema.get("required", []))
            return _flatten_schema_properties(schema, raw, "", required_set)
    return []


def _oas3_body_fields(operation: dict, raw: dict) -> list[RequestBodyField]:
    rb = operation.get("requestBody", {})
    rb = _resolve_ref(rb, raw)
    if not isinstance(rb, dict):
        return []
    content = rb.get("content", {})
    for media_type in ("application/json", "application/x-www-form-urlencoded", "*/*"):
        media = content.get(media_type, {})
        if media:
            schema = _resolve_ref(media.get("schema", {}), raw)
            required_set = set(schema.get("required", []))
            return _flatten_schema_properties(schema, raw, "", required_set)
    # Try first available content type
    for media in content.values():
        schema = _resolve_ref(media.get("schema", {}), raw)
        required_set = set(schema.get("required", []))
        return _flatten_schema_properties(schema, raw, "", required_set)
    return []


def _flatten_schema_properties(
    schema: dict,
    raw: dict,
    prefix: str,
    required_set: set[str],
    depth: int = 0,
) -> list[RequestBodyField]:
    if depth > 5:
        return []
    fields: list[RequestBodyField] = []
    props = schema.get("properties", {})
    for name, prop_schema in props.items():
        prop_schema = _resolve_ref(prop_schema, raw)
        full_name = f"{prefix}.{name}" if prefix else name
        prop_type = prop_schema.get("type", "string")
        fields.append(
            RequestBodyField(
                name=full_name,
                required=name in required_set,
                data_type=prop_type,
                description=prop_schema.get("description", ""),
            )
        )
        if prop_type == "object":
            nested_required = set(prop_schema.get("required", []))
            fields.extend(
                _flatten_schema_properties(
                    prop_schema, raw, full_name, nested_required, depth + 1
                )
            )
    # Handle array items if top-level type is array
    if schema.get("type") == "array":
        items = _resolve_ref(schema.get("items", {}), raw)
        if items.get("type") == "object":
            nested_required = set(items.get("required", []))
            fields.extend(
                _flatten_schema_properties(items, raw, prefix, nested_required, depth + 1)
            )
    return fields


# ---------------------------------------------------------------------------
# Content-type detection
# ---------------------------------------------------------------------------


def _detect_content_type(operation: dict, version: int) -> str:
    if version == 3:
        rb = operation.get("requestBody", {})
        content = rb.get("content", {}) if isinstance(rb, dict) else {}
        if content:
            return next(iter(content))
    else:
        consumes = operation.get("consumes", [])
        if consumes:
            return consumes[0]
    return "application/json"


# ---------------------------------------------------------------------------
# Ref resolution
# ---------------------------------------------------------------------------


def _resolve_ref(obj: Any, raw: dict, _depth: int = 0) -> Any:
    if _depth > 10 or not isinstance(obj, dict):
        return obj
    ref = obj.get("$ref")
    if not ref:
        return obj
    parts = ref.lstrip("#/").split("/")
    node: Any = raw
    for part in parts:
        if not isinstance(node, dict):
            return obj
        node = node.get(part, {})
    return _resolve_ref(node, raw, _depth + 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_operation_id(
    method: str,
    path: str,
    counter: dict[str, int],
) -> str:
    words = [method.lower()]
    for segment in path.strip("/").split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            words.append("by_" + segment[1:-1])
        else:
            words.append(segment)
    base = "_".join(w for w in words if w)
    n = counter.get(base, 0)
    counter[base] = n + 1
    return base if n == 0 else f"{base}_{n}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def api_id_from_s3_key(s3_key: str) -> str:
    """Stable 12-char hex id derived from the S3 object key."""
    return hashlib.sha256(s3_key.encode()).hexdigest()[:12]
