# Extending ToolWeave

This document describes supported extension points. Prefer these over forking
core safety logic (`plan_guard`, `observatory`, `mcp_auth`).

## 1. Add an OpenAPI spec to the catalog

**Path:** `src/Swagger/<name>.yaml`

1. Add a resolved `servers` URL (literal base URL, not an unresolved template).
2. Run or extend `scripts/publish_specs.py` so deploy uploads to the specs bucket.
3. EventBridge triggers `swagger_processor.py`, which parses and writes DynamoDB.
4. Call `reload_catalog` on the MCP server (or wait for cold start).

**Acceptance:** `health` reports increased endpoint count; `pre_tool` can search
new operations.

See `README.md` and `docs/architecture.md` for why unresolved server templates are
refused.

## 2. Add a new MCP tool

**Path:** `src/toolweave/server.py`

1. Define an async function and register it with FastMCP (`@mcp.tool()`).
2. Add scope policy in `src/toolweave/mcp_auth.py` if the tool must be gated.
3. If the tool performs writes to external APIs, route through
   `observatory.propose()` and only execute on `commit_api_call`.
4. Update `template.yaml` IAM only when new AWS resources are required.
5. Add tests under `tests/` (moto or unit).

**Do not** bypass `plan_guard.bind` for outbound HTTP.

## 3. Customize catalog search

**Path:** `src/toolweave/catalog_search.py`

The in-memory `_catalog` in `server.py` is the hot path. Extend scoring or
tokenization here. Call `reload_catalog` after DynamoDB changes.

## 4. Per-API outbound credentials

**Path:** `src/toolweave/api_credentials.py` (read); Secrets Manager (write)

Store JSON at secret name pattern `toolweave/api/<api_id>`:

```json
{"host": "api.example.com", "type": "bearer", "token": "..."}
```

Types `header` and `basic` are also supported. The host must match the request
host or the credential is not sent.

## 5. Planner behavior

**Path:** `src/toolweave/agent.py`

Internal tools: `search_endpoints`, `get_endpoint_details`,
`lookup_field_metadata`, `finalize_plan`.

Invariant: the agent must not invent required field values; return
`status="needs_input"` with one question.

## 6. Observatory and metrics contract

**Path:** `contracts/conformance.py`, `contracts/observatory_metrics_item.json`

When emitting metrics rows, validate with:

```python
from contracts.conformance import load_contract, check_item
problems = check_item(item, load_contract())
```

## 7. Vendored identity and cost modules

Do **not** edit in place:

- `src/cipherweave_verify/` — sync via `scripts/sync_cipherweave_verify.sh`
- `src/toolweave/bedrock_cost.py`, `src/toolweave/modelweave_roles.py` — checksum
  files at repo root

## Plugin format (conceptual)

There is no dynamic plugin loader today. Extensions are **in-tree modules** or
**sibling packages** that:

1. Import `toolweave` as a library, or
2. Speak MCP to a deployed ToolWeave endpoint.

A future plugin API might load catalog enrichers or search backends from entry
points (`pyproject.toml` `[project.entry-points]`) — **planned**, not implemented.
