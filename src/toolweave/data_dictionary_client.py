from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

from .models import DataElementMeta

DATA_DICTIONARY_URL = os.environ.get(
    "DATA_DICTIONARY_URL",
    "https://ft5yitoykshqz73sejd7miatze0syqds.lambda-url.us-east-1.on.aws/",
)

# MCP endpoint — the DataDictionary Lambda uses stateless_http=True so every
# POST to /mcp is a self-contained request/response session.
_MCP_ENDPOINT = DATA_DICTIONARY_URL.rstrip("/") + "/mcp"


async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Send a single MCP tool call to the DataDictionary Lambda and return the result."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _MCP_ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        body = resp.json()

    # MCP response: {"result": {"content": [{"type": "text", "text": "..."}]}}
    result = body.get("result", {})
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        text = content[0]["text"]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return result


async def get_data_element(data_element: str) -> Optional[dict[str, Any]]:
    result = await _call_tool("get_data_element", {"dataElement": data_element})
    if isinstance(result, dict) and "error" not in result:
        return result
    return None


async def search_data_elements(query: str) -> list[dict[str, Any]]:
    result = await _call_tool("search_data_elements", {"query": query})
    if isinstance(result, list):
        return result
    return []


async def get_elements_by_context(context: str) -> list[dict[str, Any]]:
    result = await _call_tool("get_elements_by_context", {"context": context})
    if isinstance(result, list):
        return result
    return []


async def fetch_field_metadata(
    field_names: list[str],
    context: str = "",
) -> dict[str, Optional[DataElementMeta]]:
    """Fetch DataDictionary metadata for a list of field names.

    Strategy:
    1. Try exact `get_data_element` lookup per field name
    2. Fallback: `search_data_elements` for fields not found
    3. Fallback: bulk `get_elements_by_context` filtered by name

    Returns a mapping {field_name: DataElementMeta | None}.
    """
    result: dict[str, Optional[DataElementMeta]] = {n: None for n in field_names}
    missing: list[str] = list(field_names)

    # Pass 1 — exact lookup
    for name in list(missing):
        item = await get_data_element(name)
        if item:
            try:
                result[name] = DataElementMeta(**item)
                missing.remove(name)
            except Exception:
                pass

    if not missing:
        return result

    # Pass 2 — search lookup for remaining fields
    still_missing: list[str] = []
    for name in missing:
        items = await search_data_elements(name)
        # Pick best match: exact name match first, otherwise first result
        matched = next((i for i in items if i.get("dataElement") == name), None)
        if matched is None and items:
            matched = items[0]
        if matched:
            try:
                result[name] = DataElementMeta(**matched)
            except Exception:
                still_missing.append(name)
        else:
            still_missing.append(name)

    if not still_missing or not context:
        return result

    # Pass 3 — bulk context fetch
    all_items = await get_elements_by_context(context)
    by_name = {i.get("dataElement", ""): i for i in all_items}
    for name in still_missing:
        item = by_name.get(name)
        if item:
            try:
                result[name] = DataElementMeta(**item)
            except Exception:
                pass

    return result
