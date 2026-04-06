# Claude Guidance — ToolWeave

This file provides guidance for Claude (and other AI assistants) working on the ToolWeave codebase.

---

## What Is ToolWeave?

ToolWeave is a **FastMCP server** that translates natural-language prompts into REST API calls by:

1. Parsing OpenAPI/Swagger specs into an in-memory endpoint catalog (backed by DynamoDB)
2. Using an AI agent (Amazon Bedrock / Claude 3 Haiku) to map prompts to specific endpoints and extract required parameters
3. Executing read operations immediately and gating write operations behind a proposal/commit flow via `mcp-observatory`

The full architecture is documented in [`architecture.md`](./architecture.md).

---

## Repository Layout

```
src/toolweave/
├── server.py                 # FastMCP app + 4 MCP tools + Lambda handler
├── agent.py                  # Bedrock Converse agent loop (pre_tool logic)
├── executor.py               # Async HTTP client for target REST APIs
├── observatory.py            # mcp-observatory: proposals, risk scoring, commit tokens
├── dynamodb_client.py        # DynamoDB CRUD (catalog, proposals, metrics)
├── swagger_processor.py      # Lambda handler for S3-triggered spec ingestion
├── swagger_parser.py         # OpenAPI v2/v3 → EndpointEntry parser
├── endpoint_enricher.py      # Parallel Bedrock enrichment of endpoint metadata
├── data_dictionary_client.py # JSON-RPC bridge to DataDictionary Lambda
├── catalog_search.py         # Keyword/partial-match search over in-memory catalog
├── models.py                 # All Pydantic v2 data models
└── __init__.py

template.yaml                 # AWS SAM CloudFormation (infrastructure)
samconfig.toml                # SAM deployment config (stack: toolweave, region: us-east-1)
pyproject.toml                # Package metadata and dependencies
.env.example                  # Environment variable reference
.github/workflows/deploy.yml  # GitHub Actions CI/CD (OIDC to AWS)
docs/                         # Architecture documentation and PlantUML diagrams
```

---

## Critical Invariants

These must not be broken without careful consideration:

1. **Agent never invents field values.** In `agent.py`, the `finalize_plan` tool must not be called with guessed IDs, UUIDs, or user identifiers. If required fields are missing, the agent must return `status="needs_input"` with a single clarifying question.

2. **Write operations always go through observatory.** Any `POST`, `PUT`, `PATCH`, or `DELETE` in `post_tool` must create a proposal via `observatory.propose()`. Direct execution of writes is not allowed without a prior `commit_api_call` + token verification.

3. **The in-memory `_catalog` is the hot path.** `server.py` loads the catalog once at cold start. Do not add DynamoDB reads to the `pre_tool` critical path — use the in-memory catalog. Only `reload_catalog()` may trigger a fresh DynamoDB scan.

4. **DynamoDB serialization must preserve backward compatibility.** `dynamodb_client.py` has an `_item_to_entry()` function that handles both `example_prompts` and `sample_prompts` field names. Don't remove the fallback.

5. **EventBridge rule (not direct S3 notifications).** `template.yaml` uses an EventBridge rule to bridge S3 → Lambda. This is intentional to avoid a CloudFormation circular dependency. Do not switch to direct S3 event notifications without resolving the dependency cycle.

6. **Enrichment failures are non-fatal.** `endpoint_enricher.py` has per-endpoint (60 s) and total batch (220 s) timeouts. Timeout/error on enrichment must not block the DynamoDB write. The catalog write must succeed even with partially enriched entries.

---

## How the MCP Tools Work

### `pre_tool(prompt, session_id)`

Runs `agent.run_agent()` which executes a Bedrock Converse loop (up to 20 iterations) using 4 internal tools:

| Internal Tool | Function |
|--------------|----------|
| `search_endpoints` | `catalog_search.search()` — keyword scoring over `_catalog` |
| `get_endpoint_details` | Returns full `EndpointEntry` dict for a given `operation_id` |
| `lookup_field_metadata` | Calls `data_dictionary_client.fetch_field_metadata()` |
| `finalize_plan` | Builds and returns a `PreToolResponse` — terminates the loop |

Returns `PreToolResponse` with `status` in `["ready", "needs_input", "no_match", "error"]`.

### `post_tool(api_call_plan)`

Receives a `PreToolResponse` dict. Branches on `execution_type`:

- `"immediate"` (GET) → `executor.execute_get()` → `ImmediateExecutionResult`
- `"proposal"` (write) → `observatory.propose()` → `ProposalResult`

### `commit_api_call(proposal_id, commit_token)`

1. `dynamodb_client.get_proposal_data(proposal_id)` — fetches from Proposals table
2. `observatory.verify(proposal_id, commit_token, ...)` — HMAC + re-score
3. `executor.execute_write()` — executes if verify passes
4. Returns `CommitResult`

### `reload_catalog()`

Calls `dynamodb_client.load_full_catalog()` and replaces `server._catalog`. Triggers after new specs are ingested.

---

## Changing the Data Models

All models live in `src/toolweave/models.py` (Pydantic v2). When adding a new field:

- Add default `None` or a sensible default so existing DynamoDB items deserialize correctly.
- Update `_entry_to_item()` and `_item_to_entry()` in `dynamodb_client.py` to persist/restore the field.
- If the field is enrichment-related, update the Bedrock prompt in `endpoint_enricher.py` and the JSON schema in the system prompt.

---

## Adding a New MCP Tool

1. Define the function in `server.py` and decorate with `@mcp.tool()`.
2. If it requires a new DynamoDB table, add it to `template.yaml` and update `ToolWeaveLambdaRole` permissions.
3. If it calls Bedrock, use the existing `bedrock_client` in `agent.py` (avoid creating a separate client).
4. If it involves write operations, route through `observatory.propose()` + `commit_api_call`.

---

## Changing the Swagger Ingestion Pipeline

The flow is: `swagger_processor.py` → `swagger_parser.py` → `endpoint_enricher.py` → `dynamodb_client.py`.

- Parser output is `list[EndpointEntry]` + `base_url` + `api_title`.
- Enricher must return the same-length list (enriched or original entries on failure).
- `api_id` is a 12-char SHA256 hex derived from the S3 key — do not change this hashing logic or existing catalog entries will be orphaned.

---

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_swagger_parser.py
```

Tests use `moto` to mock DynamoDB and S3. AWS credentials are not required for local testing.

Set `AWS_DEFAULT_REGION=us-east-1` and dummy `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` if moto complains about credentials.

---

## Local Development

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# Install package in editable mode
pip install -e .

# Copy and populate environment variables
cp .env.example .env

# Start the FastMCP server locally (Uvicorn on port 8080)
toolweave
```

The local server does not use Lambda/Mangum. It runs Uvicorn directly with auto-reload disabled. You can test MCP tools by connecting any MCP client to `http://localhost:8080`.

---

## Deployment

```bash
# Build and deploy (uses samconfig.toml)
sam build && sam deploy

# Validate template
sam validate

# Check deployed stack outputs
aws cloudformation describe-stacks \
  --stack-name toolweave \
  --query "Stacks[0].Outputs"
```

GitHub Actions handles production deploys via OIDC — no manual `sam deploy` is needed for `main` branch changes.

---

## Key Environment Variables to Set Locally

```bash
# Required for DynamoDB access
AWS_REGION=us-east-1

# Required for Bedrock
BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0

# Required for proposal gating (use a strong secret in production)
OBSERVATORY_SECRET_KEY=your-secret-here
OBSERVATORY_BLOCK_THRESHOLD=0.45

# Required if DataDictionary is deployed
DATA_DICTIONARY_URL=https://<lambda-url>

# Table names (match deployed CloudFormation outputs)
CATALOG_TABLE_NAME=toolweave-ApiCatalogTable
META_TABLE_NAME=toolweave-ApiMetaTable
PROPOSALS_TABLE_NAME=toolweave-ProposalsTable
```

---

## Common Pitfalls

| Pitfall | Correct Approach |
|---------|-----------------|
| Adding a DynamoDB scan to the `pre_tool` hot path | Use the module-level `_catalog` list; call `reload_catalog` explicitly when needed |
| Calling `finalize_plan` with guessed field values | Always check `missing_required_fields`; return `needs_input` if any required field is absent |
| Executing write operations without a proposal | Always call `observatory.propose()` first; never skip commit verification |
| Hardcoding `api_id` | Use the SHA256-of-S3-key scheme in `swagger_parser._api_id_from_key()` |
| Catching all exceptions in enricher and silently failing | Log the exception with `operation_id` context before returning the original entry |
| Changing DynamoDB key schema on existing tables | Key schema changes require table replacement — plan with a migration or rename strategy |
| Importing external PlantUML URLs in diagrams | Use local includes from `docs/assets/` — all diagram assets must be offline-capable |

---

## Glossary

| Term | Meaning |
|------|---------|
| **MCP** | Model Context Protocol — standardized interface for AI tool invocation |
| **EndpointEntry** | Normalized representation of one REST endpoint from an OpenAPI spec |
| **operation_id** | Stable identifier for an endpoint (from spec or auto-generated from method + path) |
| **api_id** | 12-char hex SHA256 of the S3 spec key — stable identifier for an entire API |
| **proposal** | A proposed write operation pending human/system approval |
| **commit_token** | HMAC-signed token authorizing execution of a specific proposal |
| **composite_score** | Risk score (0–1) from mcp-observatory; above `BLOCK_THRESHOLD` → blocked |
| **enrichment** | AI-generated metadata added to each endpoint: `agent_hint`, `example_prompts`, etc. |
| **DataDictionary** | Shared Lambda service providing field-level semantic metadata |
| **Converse API** | Amazon Bedrock's multi-turn tool-use API used by the planning agent |
