# ToolWeave — Architecture

> Current as of `template.yaml` and `src/toolweave/`. `CLAUDE.md` is the short
> authoritative summary; this document is the long form. The Bedrock model is
> not named here: it comes from ModelWeave's role pins.

## Overview

ToolWeave is a **FastMCP server** that turns natural-language requests into REST
API calls, using OpenAPI specs as its catalog. Reads run immediately. Writes are
never executed on the first call: they become a **proposal** in mcp-observatory,
and run only after `commit_api_call` verifies the commit token.

```
MCP client (natural-language prompt)
        │  Bearer / DPoP (CipherWeave token) or legacy static token
        ▼
  API Gateway HTTP API ──► ToolWeave Lambda (FastMCP via Mangum)
                          ┌──────────────────────────────────────────────┐
                          │ pre_tool          plan (read)                │
                          │ post_tool         GET now / write → proposal │
                          │ commit_api_call   verify token, execute write│
                          │ reload_catalog    rescan DynamoDB            │
                          │ health            liveness and catalog size  │
                          └──────────────────────────────────────────────┘
                              │             │                 │
                              ▼             ▼                 ▼
                     DynamoDB catalog   Target REST APIs   mcp-observatory
                     + proposals        (per-API creds)   (risk, tokens, metrics)
```

Every spec in the S3 bucket becomes a set of callable endpoints. A caller never
supplies a URL. The plan must match a cataloged endpoint (see invariant 3 in
`CLAUDE.md`).

---

## System context

| Actor / system | Role |
|---|---|
| MCP client | sends natural-language requests through the five tools |
| API owner | uploads OpenAPI/Swagger specs to `toolweave-api-specs-<acct>-<region>` |
| Target REST APIs | the services ToolWeave calls on the caller's behalf |
| Amazon Bedrock | planning agent (`pre_tool`) and parallel endpoint enrichment |
| ModelWeave | resolves the `reasoning` and `cheap-fast` role pins from SSM |
| DataDictionary | field-level semantics, fetched through `DATA_DICTIONARY_URL` |
| mcp-observatory | risk scoring, HMAC proposal and commit tokens, metrics |
| CipherWeave | issues the access tokens `/mcp` verifies (vendored `cipherweave_verify`) |

---

## Containers

### ToolWeave Lambda (`src/toolweave/server.py`)

FastMCP wrapped by Mangum. 512 MB, 60 s timeout. Five tools:

| Tool | Scope | Behaviour |
|---|---|---|
| `pre_tool` | `read` | Plans a call from a prompt with a Bedrock Converse loop (up to 20 tool-use iterations). Returns `status="needs_input"` with one question when a required field is missing. Never invents a value. |
| `post_tool` | `execute` | Checks the plan against the catalog (`plan_guard.bind`). A GET executes now. Any write becomes a proposal in observatory and returns a proposal id and a commit token. |
| `commit_api_call` | `execute`, not delegated | Verifies the commit token against the proposal and executes the stored write. Delegated tokens cannot commit; the principal does. |
| `reload_catalog` | `execute` | Rescans `ApiCatalog` and `ApiMeta` into memory. |
| `health` | `read` | Liveness and catalog size. |

`ToolScopeMode=audit` logs tool-scope failures instead of refusing them, during
migration.

**State.** The catalog is an in-memory list populated at cold start. `pre_tool`
never reads DynamoDB. Only `reload_catalog` rescans.

### SwaggerProcessor Lambda (`src/toolweave/swagger_processor.py`)

Runs when an object is created or deleted in the specs bucket:

1. Fetch the spec (`.yaml`, `.yml` or `.json`).
2. Parse with `swagger_parser.parse_spec()` into `EndpointEntry` objects.
   A spec whose `servers` URL is still an unresolved template is refused.
   `scripts/publish_specs.py` rewrites it from the sibling stack's output first.
3. Enrich each endpoint with `endpoint_enricher.enrich_endpoints()`, in parallel
   Bedrock calls (60 s per endpoint, 220 s overall). Enrichment failures do not
   fail the write.
4. Delete the existing entries for that `api_id`, so re-ingestion is idempotent.
5. Write the `ApiMeta` row and the endpoint batch to `ApiCatalog`.

On **Object Deleted** it removes both the endpoints and the metadata row. A spec
removed from `src/Swagger/` is therefore withdrawn, not left callable.

### Storage

| Resource | Key | Purpose |
|---|---|---|
| `<stack>-ApiCatalog` | `api_id` / `operation_id` | normalized endpoints with enrichment |
| `<stack>-ApiMeta` | `api_id` | title, base URL, context name, endpoint count |
| `<stack>-Proposals` | `proposal_id` | write proposals; `_expires_at` is set one hour out (3600 s) |
| `toolweave-api-specs-<acct>-<region>` (S3, versioned) | object key | OpenAPI specs; `ObjectCreated` and `ObjectDeleted` both reach the processor |
| Shared `OBSERVATORY_METRICS` table | — | spans, under contract (`tests/test_shared_table_contract.py`) |
| Secrets Manager `toolweave/api/<api_id>` | — | per-API credentials, host-bound |

**`api_id` is a fixed hash.** It is the first 12 hex characters of the SHA-256
of the S3 key. Changing the hash would orphan the existing catalog.

### Event wiring

An **EventBridge** rule matches S3 object events and invokes SwaggerProcessor.
Direct S3 notifications create a CloudFormation dependency cycle between the
bucket, the function and its role, so EventBridge is used on purpose.

---

## Key flows

### 1. Spec ingestion

```
publish_specs.py (resolves servers) → S3 object created/deleted
   → EventBridge → SwaggerProcessor → ApiCatalog / ApiMeta
   → reload_catalog (or next cold start) → in-memory catalog
```

### 2. Planning (`pre_tool`)

1. Authenticate the token (`read`).
2. Run the Bedrock Converse loop against the in-memory catalog.
3. Return a plan bound to a cataloged endpoint, or `needs_input` with one question.

### 3. GET execution (`post_tool`, immediate)

1. Bind the plan to the catalog. Method, base URL and path template must match.
   Path parameters are single segments; encoded traversal is refused. Query
   parameters and headers must be declared by the spec.
2. Egress check (`plan_guard.resolve_egress`). Loopback, private, link-local,
   multicast and reserved addresses are refused, literal or by DNS. Redirects are
   not followed. The connection goes to the address that was checked, with
   `Host` and SNI preserved.
3. Attach the API's credential only if its owner stored one, and only to its host.
   A credential that exists but cannot be read refuses the call.

### 4. Write execution (`post_tool` → `commit_api_call`)

1. Steps 1–3 of §3 run for the plan.
2. The write is sent to observatory as a proposal, with its risk score. Nothing executes.
3. The caller gets a proposal id and a commit token.
4. `commit_api_call` verifies the token, runs the stored write, and returns the response.
   A delegated (RFC 8693) token can plan and propose, but cannot commit.

---

## AWS infrastructure

| Resource | Notes |
|---|---|
| API Gateway HTTP API | `/mcp` takes the token check. Other paths are forwarded to the same Lambda without a check, so do not add a route there that assumes authentication. |
| ToolWeave Lambda | Python 3.12, 512 MB, 60 s |
| SwaggerProcessor Lambda | triggered by EventBridge |
| `SharedStackLookup` custom resource | reads outputs from the shared stack (VPC, observatory table) |
| DynamoDB | `ApiCatalog`, `ApiMeta`, `Proposals` |
| S3 | specs bucket, versioned |
| CloudWatch | metric filters on `[ERROR]`, `Task timed out` and `Runtime.ExitError` in each function's log group |

**Model selection.** `pre_tool` uses the `reasoning` role pin, with
`BEDROCK_MODEL_ID` as the fallback. Enrichment uses `ENRICHER_MODEL_ID`. Both
go through `bedrock_cost.py`, so spend is tagged `repo=ToolWeave`. The
`BEDROCK_PROFILE_ARNS` map covers the application inference profiles.

**IAM.** The Lambda role's grants cover the `toolweave/api/*` secrets, the
`modelweave/roles/*` SSM parameters, the observatory table and the catalog
tables. The `/mcp` bearer-secret grant is separate, and only while the legacy token is accepted.

---

## Auth

`/mcp` takes a CipherWeave token (`aud = toolweave`, Bearer or DPoP), or the
static token while `AcceptLegacyToken` is true.

| Tool | Scope |
|---|---|
| `pre_tool`, `health` | `read` |
| `post_tool`, `reload_catalog` | `execute` |
| `commit_api_call` | `execute`, not delegated |

---

## Environment variables

| Variable | Meaning |
|---|---|
| `CATALOG_TABLE_NAME`, `META_TABLE_NAME`, `PROPOSALS_TABLE_NAME` | DynamoDB table names |
| `API_SPECS_BUCKET` | specs bucket |
| `OBSERVATORY_METRICS_TABLE`, `OBSERVATORY_SECRET_KEY`, `OBSERVATORY_BLOCK_THRESHOLD` | observatory contract and risk threshold |
| `BEDROCK_MODEL_ID`, `ENRICHER_MODEL_ID` | fallback planner and enricher models |
| `BEDROCK_PROFILE_ARNS`, `BEDROCK_COST_ATTRIBUTION` | cost attribution |
| `DATA_DICTIONARY_URL` | DataDictionary MCP endpoint |
| `ACCEPT_LEGACY_TOKEN` | accept the static token |

---

## Security model

- **The agent never invents field values.** Missing required fields mean `needs_input`.
- **Writes go through the observatory.** No write executes without a verified commit token.
- **Plans are bound to the catalog.** Plans are tool arguments, so without this a caller could name any URL and make the Lambda fetch it from inside the VPC (SSRF).
- **Egress is public-only and pinned.** See §3.
- **Credentials are per API and host-bound.** The old placeholder `Bearer 123` that went to every API is gone.
- **Withdrawal is two-sided.** Removing a spec removes its endpoints and metadata.

---

## Known limits

- The catalog is in memory and is loaded at cold start. A change reaches a warm
  container only after `reload_catalog`.
- Enrichment depends on Bedrock. A failed enrichment leaves the endpoint
  partially enriched, and it stays callable.
- `api_id` is fixed by the S3 key. Renaming a spec file creates a new API id.
