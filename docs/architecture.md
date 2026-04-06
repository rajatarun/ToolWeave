# ToolWeave — Architecture

## Overview

ToolWeave is a **FastMCP server** that bridges natural-language requests to REST API execution via OpenAPI/Swagger specifications. It combines AWS serverless infrastructure with AI-powered planning and a proposal/commit safety gate for write operations.

```
MCP Client (natural-language prompt)
        │
        ▼
  API Gateway HTTP API
        │
        ▼
  ToolWeave Lambda (FastMCP)
  ┌─────────────────────────────────────────────────┐
  │  pre_tool  ──► Bedrock Agent ──► API plan        │
  │  post_tool ──► Executor (GET) / Observatory      │
  │  commit_api_call ──► Verify + Execute (write)    │
  │  reload_catalog ──► Refresh in-memory catalog    │
  └─────────────────────────────────────────────────┘
        │                       │
        ▼                       ▼
  DynamoDB (catalog,       Target REST APIs
  proposals, metrics)
```

---

## C4 Diagrams

| Diagram | File | Level |
|---------|------|-------|
| System Context | [c4-context.puml](./c4-context.puml) | C4 Level 1 |
| Containers | [c4-container.puml](./c4-container.puml) | C4 Level 2 |
| Components | [c4-component.puml](./c4-component.puml) | C4 Level 3 |
| AWS Deployment | [aws-architecture.puml](./aws-architecture.puml) | Deployment |

All PlantUML diagrams use local assets in [`./assets/`](./assets/) — no remote URL imports.

---

## System Context (C4 Level 1)

| Actor / System | Role |
|---|---|
| **Developer / MCP Client** | Issues natural-language API requests via MCP tools |
| **API Owner** | Uploads OpenAPI/Swagger specs to S3 to populate the catalog |
| **ToolWeave** | Core system — translates prompts, gates write operations |
| **Target REST APIs** | External services that ToolWeave executes on behalf of the caller |
| **Amazon Bedrock** | Claude 3 Haiku — AI planning and endpoint enrichment |
| **DataDictionary MCP** | Shared Lambda providing field-level metadata |
| **mcp-observatory** | Risk scoring, HMAC proposal/commit tokens, metrics |

---

## Containers (C4 Level 2)

### ToolWeave Lambda (`src/toolweave/server.py`)

The main application. Runs as a FastMCP server wrapped by Mangum (ASGI → Lambda adapter). Exposes four MCP tools:

| Tool | Method | Description |
|------|--------|-------------|
| `pre_tool` | read | Maps a natural-language prompt to an API call plan via a Bedrock agent loop |
| `post_tool` | read/write | Executes GET immediately; converts writes to proposals via mcp-observatory |
| `commit_api_call` | write | Verifies a proposal's commit token and executes the write operation |
| `reload_catalog` | read | Reloads the in-memory endpoint catalog from DynamoDB |

**State:**
- `_catalog` — module-level list of `EndpointEntry` objects; populated at cold start.
- `_dd_context` — DataDictionary context name, inferred from the first API title in the catalog.

### SwaggerProcessor Lambda (`src/toolweave/swagger_processor.py`)

Triggered by EventBridge when a new spec file lands in S3. Workflow:

1. Fetch `.yaml/.yml/.json` from S3 (`GetObject`)
2. Parse via `swagger_parser.parse_spec()` → list of `EndpointEntry`
3. Enrich every endpoint via `endpoint_enricher.enrich_endpoints()` (parallel Bedrock calls)
4. Delete existing entries for that `api_id` (idempotent)
5. Write `ApiMeta` row + endpoint batch to DynamoDB

### DynamoDB Tables

| Table | Key Schema | Purpose |
|-------|-----------|---------|
| `ApiCatalog` | PK: `api_id` / SK: `operation_id` | Normalized endpoint entries with enrichment data |
| `ApiMeta` | PK: `api_id` | Per-API metadata: title, base_url, context_name, endpoint_count |
| `Proposals` | PK: `proposal_id` / TTL: 3600 s | Write proposals persisted across Lambda invocations |

### API Specs S3 Bucket

Versioning-enabled S3 bucket. EventBridge notifications fire on object creation, triggering SwaggerProcessor. Bucket name: `toolweave-api-specs-{account}-{region}`.

### EventBridge Rule

Translates S3 `ObjectCreated` events into Lambda invocations for SwaggerProcessor. Using EventBridge (instead of direct S3 event notifications) avoids a CloudFormation circular dependency between the bucket, the Lambda function, and the IAM role.

---

## Components (C4 Level 3) — ToolWeave Lambda

```
Mangum → server.py
              │
        ┌─────┴─────────────────────────────────┐
        │                                       │
     pre_tool                              post_tool / commit
        │                                       │
     agent.py ──► catalog_search.py         executor.py
        │                                   observatory.py
        │──► data_dictionary_client.py ──► dd_lambda
        │
        └──► Amazon Bedrock (Converse API)
```

| Module | Responsibility |
|--------|---------------|
| `server.py` | FastMCP app, tool registration, cold-start catalog load |
| `agent.py` | Multi-turn Bedrock Converse agent; 4 internal tools; session history |
| `executor.py` | Async httpx client; GET (immediate) and write operations |
| `observatory.py` | mcp-observatory wrapper: risk scoring, proposal, verify |
| `dynamodb_client.py` | DynamoDB CRUD for catalog, proposals, and metrics |
| `data_dictionary_client.py` | 3-pass field metadata lookup via JSON-RPC |
| `catalog_search.py` | Keyword + partial-match scoring over in-memory catalog |
| `models.py` | Pydantic models for all data structures |
| `swagger_parser.py` | OpenAPI v2/v3 parser → `EndpointEntry` |
| `swagger_processor.py` | S3-triggered Lambda handler for spec ingestion |
| `endpoint_enricher.py` | Bedrock-powered parallel enrichment (60 s/endpoint, 220 s total) |

---

## Key Flows

### 1. Spec Ingestion (API Owner → DynamoDB)

```
API Owner ──uploads──► S3 Bucket
                           │ EventBridge ObjectCreated
                           ▼
                  SwaggerProcessorFunction
                           │
                    parse_spec()      ──► EndpointEntry[]
                    enrich_endpoints() ──► Bedrock (parallel, ≤60 s each)
                    delete_api_entries()   (idempotent)
                    write_api_meta()
                    write_endpoint_batch()
                           │
                    DynamoDB (ApiMeta + ApiCatalog)
```

### 2. API Call Planning (pre_tool)

```
Client: pre_tool(prompt, session_id)
    │
    └─► agent.py: run_agent()
            │ search_endpoints(query) ──► catalog_search.search()
            │ get_endpoint_details(op_id)
            │ lookup_field_metadata(fields) ──► DataDictionary Lambda
            │
            └─► Bedrock Converse API (≤20 tool-use iterations)
                    │
                    ├─ Missing required field? ──► status="needs_input" (ask user)
                    └─ All fields present? ──────► finalize_plan() → status="ready"
```

### 3. GET Execution (post_tool — immediate)

```
Client: post_tool(api_call_plan)  [execution_type="immediate"]
    │
    └─► executor.execute_get()
            │
            └─► httpx GET ──► Target REST API
                    │
                    └─► ImmediateExecutionResult (status_code, body, elapsed_ms)
```

### 4. Write Execution (post_tool → commit_api_call)

```
Client: post_tool(api_call_plan)  [execution_type="proposal"]
    │
    └─► observatory.propose()
            │ Risk scoring → composite_score
            │ HMAC token generation
            │ save_proposal() ──► Proposals DynamoDB (TTL 3600 s)
            │
            └─► ProposalResult (proposal_id, commit_token, composite_score)
                    │
                    └─► [if status="allowed"]
                        Client: commit_api_call(proposal_id, commit_token)
                            │
                            ├─► get_proposal_data() ──► DynamoDB
                            ├─► observatory.verify() ──► token + re-score
                            └─► executor.execute_write() ──► Target API
                                    │
                                    └─► CommitResult (status="committed")
```

---

## AWS Infrastructure

Defined in [`template.yaml`](../template.yaml) (AWS SAM / CloudFormation).

### Lambda Functions

| Function | Memory | Timeout | Trigger |
|----------|--------|---------|---------|
| `ToolWeaveFunction` | 1024 MB | 120 s | API Gateway HTTP API |
| `SwaggerProcessorFunction` | 512 MB | 300 s | EventBridge Rule |

Both functions run inside a VPC with a security group that allows only outbound HTTPS traffic.

### IAM Permissions

`ToolWeaveLambdaRole` grants:

| Service | Actions |
|---------|---------|
| Amazon Bedrock | `InvokeModel`, `InvokeModelWithResponseStream` |
| Amazon DynamoDB | `PutItem`, `GetItem`, `Query`, `Scan`, `DeleteItem`, `BatchWriteItem` |
| Amazon S3 | `GetObject`, `PutObject`, `ListBucket` on the API specs bucket |
| VPC | `AWSLambdaVPCAccessExecutionRole` (managed policy) |

### Deployment

Deployments are managed by GitHub Actions (`.github/workflows/deploy.yml`) using OIDC — no static AWS credentials. The workflow:

1. Fetches shared stack outputs (VPC, subnet IDs, observatory table ARN)
2. Runs `sam validate → sam build → sam deploy`
3. Passes `ObservatorySecretKey`, `BedrockModelId`, and other overrides as SAM parameters

---

## Data Models

```
EndpointEntry          — Normalized API endpoint (path, method, params, body fields, enrichment)
PreToolResponse        — Output of pre_tool (status, plan, extracted params/body)
ImmediateExecutionResult — GET result (status_code, response_body, elapsed_ms)
ProposalResult         — Write proposal (proposal_id, commit_token, risk score)
CommitResult           — Commit outcome (status, commit_id, response_body)
FieldMapping           — Traceability: where each extracted value came from
DataElementMeta        — DataDictionary field doc (meaning, dataType, examples)
EndpointParameter      — Single path/query/header/cookie parameter
RequestBodyField       — Single request body field (dot-notation for nested objects)
```

All models are defined in `src/toolweave/models.py` using Pydantic v2.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_REGION` | `us-east-1` | AWS region |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-haiku-20240307-v1:0` | Agent model |
| `ENRICHER_MODEL_ID` | same as above | Enrichment model |
| `CATALOG_TABLE_NAME` | `toolweave-ApiCatalogTable` | DynamoDB catalog table |
| `META_TABLE_NAME` | `toolweave-ApiMetaTable` | DynamoDB meta table |
| `PROPOSALS_TABLE_NAME` | `toolweave-ProposalsTable` | DynamoDB proposals table |
| `API_SPECS_BUCKET` | set by SAM | S3 bucket for specs |
| `OBSERVATORY_SECRET_KEY` | `change-me-in-production` | HMAC secret for commit tokens |
| `OBSERVATORY_BLOCK_THRESHOLD` | `0.45` | Risk score threshold (0–1) |
| `DATA_DICTIONARY_URL` | shared Lambda URL | DataDictionary endpoint |
| `TOOLWEAVE_LOG_CONVERSE_MESSAGES` | `false` | Log Bedrock agent prompts |
| `TOOLWEAVE_LOG_CONVERSE_RAW_RESPONSES` | `false` | Log raw Bedrock responses |

---

## Security Model

| Operation | Safety Mechanism |
|-----------|-----------------|
| **GET (read)** | Executed immediately — no gating |
| **Write (POST/PUT/PATCH/DELETE)** | Requires `post_tool` → proposal → `commit_api_call` with HMAC token |
| **High-risk write** | Blocked if `composite_score > OBSERVATORY_BLOCK_THRESHOLD` |
| **Field extraction** | Agent never invents IDs/UUIDs; asks one clarifying question if required fields are missing |
| **Proposal expiry** | DynamoDB TTL = 3600 s — proposals auto-expire |
| **IAM** | Lambda role has minimum required permissions; no wildcard resource grants |
| **Network** | Lambda runs in VPC; security group allows outbound HTTPS only |
| **Credentials** | GitHub Actions uses OIDC; no long-lived AWS keys |

---

## Dependency Graph

```
server.py
├── agent.py
│   ├── catalog_search.py
│   ├── data_dictionary_client.py
│   └── models.py
├── executor.py
│   └── models.py
├── observatory.py
│   └── dynamodb_client.py (proposals)
├── dynamodb_client.py (catalog)
└── models.py

swagger_processor.py
├── swagger_parser.py
│   └── models.py
├── endpoint_enricher.py
│   └── models.py
└── dynamodb_client.py (catalog + meta)
```

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| MCP Framework | FastMCP 2.3+ |
| AI / Planning | Amazon Bedrock — Claude 3 Haiku (Converse API) |
| HTTP Server | Uvicorn (local) / Mangum + API Gateway (AWS) |
| Data Validation | Pydantic v2 |
| HTTP Client | httpx (async) |
| AWS SDK | boto3 1.34+ |
| Safety Gating | mcp-observatory |
| API Spec Parsing | PyYAML + custom OpenAPI v2/v3 parser |
| Infrastructure | AWS SAM (CloudFormation) |
| CI/CD | GitHub Actions (OIDC) |
| Testing | pytest + pytest-asyncio + moto (DynamoDB/S3 mocking) |
