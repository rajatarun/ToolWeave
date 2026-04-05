# ToolWeave

ToolWeave is a FastMCP server that turns natural-language requests into safe REST API executions using OpenAPI/Swagger specs.

It combines:
- **Cataloging** of API specs from S3 into DynamoDB.
- **Planning** via an AWS Bedrock Converse agent.
- **Execution controls** using proposal/commit gating through `mcp-observatory`.
- **Field understanding** through a DataDictionary MCP service.

## What ToolWeave does

ToolWeave exposes four MCP tools:

1. **`pre_tool`**
   - Accepts a user prompt.
   - Finds the best API endpoint from the catalog.
   - Extracts parameters/body fields.
   - Returns either:
     - `status="ready"` with a full call plan, or
     - `status="needs_input"` with one follow-up question.

2. **`post_tool`**
   - Accepts the `pre_tool` plan.
   - Executes `GET` calls immediately.
   - Converts write operations (`POST/PUT/PATCH/DELETE`) into proposals.

3. **`commit_api_call`**
   - Finalizes a proposal using `proposal_id` + `commit_token`.
   - Executes the write operation if verification passes.

4. **`reload_catalog`**
   - Reloads in-memory endpoint catalog from DynamoDB.

---

## Architecture at a glance

```text
OpenAPI/Swagger file upload to S3
    ↓ (EventBridge: Object Created)
SwaggerProcessor Lambda
    ↓ parses + enriches
DynamoDB (ApiMeta + ApiCatalog)
    ↓ cold start / reload
FastMCP ToolWeave server
    ↓
pre_tool -> post_tool -> (optional) commit_api_call
```

### Main components

- `src/toolweave/server.py`
  - FastMCP server and 4 tool endpoints.
  - Lambda entrypoint (`lambda_handler`) via Mangum.

- `src/toolweave/swagger_processor.py`
  - S3-triggered ingestion pipeline.
  - Parses OpenAPI/Swagger and writes catalog metadata.

- `src/toolweave/agent.py`
  - Bedrock Converse tool-use loop for plan generation.

- `src/toolweave/swagger_parser.py`
  - OpenAPI v2/v3 parser into normalized endpoint records.

- `src/toolweave/dynamodb_client.py`
  - Catalog + proposal persistence helpers.

- `src/toolweave/observatory.py`
  - Proposal scoring/verification and invocation metrics.

- `src/toolweave/data_dictionary_client.py`
  - MCP JSON-RPC bridge to DataDictionary service.

---

## Core workflow

### 1) Ingest API specs

1. Upload `.yaml/.yml/.json` spec to the API specs bucket.
2. EventBridge invokes `SwaggerProcessorFunction`.
3. Processor:
   - Loads spec from S3.
   - Parses endpoints and body fields.
   - Enriches endpoint metadata via Bedrock.
   - Replaces existing rows for that API in DynamoDB.

### 2) Plan API calls from user prompts

`pre_tool` runs a Bedrock loop that uses internal tools:
- `search_endpoints`
- `get_endpoint_details`
- `lookup_field_metadata`
- `finalize_plan`

If required values are missing, it asks exactly one concise follow-up question.

### 3) Execute or propose

- `GET` plan → executed immediately by `post_tool`.
- Write plan → `post_tool` requests an observatory proposal.

### 4) Commit write operations

`commit_api_call` verifies the commit token and executes the persisted write request.

---

## Project layout

```text
.
├── src/toolweave/
│   ├── server.py
│   ├── agent.py
│   ├── executor.py
│   ├── observatory.py
│   ├── dynamodb_client.py
│   ├── swagger_processor.py
│   ├── swagger_parser.py
│   ├── endpoint_enricher.py
│   ├── data_dictionary_client.py
│   ├── catalog_search.py
│   └── models.py
├── template.yaml
├── pyproject.toml
├── samconfig.toml
└── README.md
```

---

## Prerequisites

- Python **3.12+**
- AWS account with access to:
  - Lambda
  - DynamoDB
  - S3
  - EventBridge
  - Bedrock Runtime
- SAM CLI (for infrastructure deployment)

---

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Install dev dependencies:

```bash
pip install -e .[dev]
```

---

## Configuration

ToolWeave reads configuration from environment variables (with defaults in code/template).

### Important runtime variables

| Variable | Purpose |
|---|---|
| `CATALOG_TABLE_NAME` | DynamoDB table for endpoint catalog rows |
| `META_TABLE_NAME` | DynamoDB table for API metadata rows |
| `PROPOSALS_TABLE_NAME` | DynamoDB table for proposal persistence |
| `OBSERVATORY_METRICS_TABLE` | Shared metrics table |
| `OBSERVATORY_SECRET_KEY` | HMAC secret for proposal commit tokens |
| `OBSERVATORY_BLOCK_THRESHOLD` | Risk score threshold to block proposals |
| `BEDROCK_MODEL_ID` | Bedrock model for planning agent |
| `ENRICHER_MODEL_ID` | Bedrock model for endpoint enrichment |
| `DATA_DICTIONARY_URL` | Base URL of DataDictionary MCP Lambda |
| `AWS_REGION` | AWS region for SDK clients |

### Helpful debug flags

| Variable | Effect |
|---|---|
| `TOOLWEAVE_LOG_CONVERSE_MESSAGES` | Logs prompt/tool conversation messages |
| `TOOLWEAVE_LOG_CONVERSE_RAW_RESPONSES` | Logs full raw Bedrock responses |
| `TOOLWEAVE_LOG_PREVIEW_CHARS` | Max chars for preview in logs |

---

## Run locally

### Option A: local FastMCP HTTP server

```bash
toolweave
```

This starts Uvicorn on `0.0.0.0:8080` via `toolweave.server:main`.

### Option B: SAM local (Lambda emulation)

```bash
sam build
sam local start-api
```

---

## Deploy to AWS (SAM)

```bash
sam build
sam deploy
```

`samconfig.toml` provides default deploy settings (stack name `toolweave`, region `us-east-1`).

---

## Typical MCP usage flow

Pseudo-sequence for a client:

1. Call `pre_tool(prompt="Update order ORD-123 status to shipped")`
2. If response status is `needs_input`, ask user and call `pre_tool` again with same `session_id`.
3. When response status is `ready`, pass full payload to `post_tool(api_call_plan=...)`.
4. If `post_tool` returns:
   - `execution_type="immediate"`: done.
   - `execution_type="proposal"` + `status="allowed"`: ask for confirmation and call `commit_api_call`.

---

## Security and safety model

- **Read operations (`GET`)** execute directly.
- **Write operations** are gated through proposal/commit workflow.
- Proposals are scored by `mcp-observatory` and can be blocked before execution.
- Commits require token verification.
- Proposals are persisted in DynamoDB with TTL for cross-invocation durability.

---

## Notes and assumptions

- Current HTTP executor always merges an external auth header placeholder:
  - `Authorization: Bearer 123`
- Endpoint enrichment and planning depend on Bedrock availability and permissions.
- DataDictionary lookups are best-effort with fallback strategies.

---

## Development checks

Run tests and linting (when present in your environment):

```bash
pytest
ruff check .
```

---

## License

See [LICENSE](LICENSE).
