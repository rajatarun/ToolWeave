# ToolWeave

ToolWeave is a FastMCP server that turns natural-language requests into safe REST API executions using OpenAPI/Swagger specs.

It combines:
- **Cataloging** of API specs from S3 into DynamoDB.
- **Planning** via an AWS Bedrock Converse agent.
- **Execution controls** using proposal/commit gating through `mcp-observatory`.
- **Field understanding** through a DataDictionary MCP service.

## What ToolWeave does

ToolWeave exposes five MCP tools:

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

5. **`health`**
   - Read-only liveness: status and loaded endpoint count. No DynamoDB, Bedrock, or API call.

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

The specs ToolWeave serves are declared in `src/Swagger/` and published by the
deploy. Today that is two siblings:

| Spec | API | Server resolved from |
|------|-----|----------------------|
| `deviceweave.yaml` | DeviceWeave | `deviceweave` stack, `ApiBaseUrl` output |
| `content-orchestrator.yaml` | TaskWeave Content Orchestrator | `tarun-admin-content` stack, `ApiBaseUrl` output |

1. `scripts/publish_specs.py` uploads each to the API specs bucket.
2. EventBridge invokes `SwaggerProcessorFunction`.
3. Processor:
   - Loads spec from S3.
   - Parses endpoints and body fields.
   - Enriches endpoint metadata via Bedrock.
   - Replaces existing rows for that API in DynamoDB.

**Why a publisher rather than `aws s3 cp`.** Both specs declare their server as
an OAS3 variable, because they instruct callers to resolve the host from a
CloudFormation output rather than hardcode it:

```yaml
servers:
  - url: "{apiBaseUrl}"
    variables:
      apiBaseUrl:
        default: https://example.execute-api.us-east-1.amazonaws.com/prod
```

That default is the trap. It is not an obviously broken placeholder — it
resolves, it is a syntactically valid API Gateway URL, and it points at a host
that does not exist. A spec uploaded verbatim catalogs every one of its
endpoints against that address; the agent plans calls against them and each
one fails with a connection error naming a plausible AWS hostname.

So the publisher rewrites `servers` to the one literal URL read from that
sibling's stack, and verifies the result with the same resolver the processor
uses. A spec that still carries a server template therefore did not come
through the publisher, and `swagger_processor` **refuses to catalog it**.

A sibling whose stack does not resolve is skipped by name and left in the
bucket untouched: one API fewer, rather than an API pointing nowhere — and
never a working catalog entry deleted because one `describe-stacks` call
blipped.

**Withdrawing a spec.** Removing it from `src/Swagger/` prunes it from the
bucket on the next deploy, and the `Object Deleted` event drops its endpoints
*and* its metadata row from DynamoDB. Both halves matter: the rule used to
listen for `Object Created` only, so a deleted spec left its endpoints in the
catalog and the MCP server went on offering an API that existed nowhere else.

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
| `MCP_AUTH_TOKEN_SECRET_NAME` | Secrets Manager name of the `/mcp` bearer token. The value is a name, not the token. |

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

`samconfig.toml` provides default deploy settings (stack name `toolweave`, region `us-east-1`), including `McpAuthTokenSecretName=toolweave/mcp-auth-token`.

---

## MCP authentication

The deployed `/mcp` route requires a bearer token. API Gateway still forwards every other path to the same Lambda, and those paths are not checked.

### CipherWeave access tokens

The bearer token can be a short-lived token from the CipherWeave token issuer: an ES256 JWT with `aud` = `toolweave`, 15-minute lifetime, verified in-process against CipherWeave's JWKS (cached 10 minutes). A token for another Weave, an expired or forged one, or `alg: none` is the same 401.

```bash
TOKEN=$(curl -s -u "$CLIENT_ID:$CLIENT_SECRET" -H 'content-type: application/json' \
  -d '{"audience":"toolweave"}' "$CIPHERWEAVE_API/token" | jq -r .access_token)
```

| Setting | |
|---|---|
| `CipherWeaveJwksUrl` → `CIPHERWEAVE_JWKS_URL` | read by the deploy from the `cipherweave-prod` output `IssuerJwksUrl`. Empty: CipherWeave tokens are refused and only the static token works |
| `CipherWeaveIssuer` → `CIPHERWEAVE_ISSUER` | `IssuerId` output, default `urn:cipherweave:prod` |
| `AcceptLegacyToken` → `ACCEPT_LEGACY_TOKEN` | default `true`: the static token below keeps working. Set by the repository variable `ACCEPT_LEGACY_TOKEN` |
| `src/cipherweave_verify/` | vendored copy of the verifier, pinned by commit in `requirements-cipherweave.txt`, checksummed in `VENDORED.json`. Never edit it; upgrade with `scripts/sync_cipherweave_verify.sh` |

The decision is `cipherweave_verify.BearerAuth`, the same code the other Weaves run. Each call logs `mcp auth method=jwt` or `method=legacy`. Once no `method=legacy` appears, set `ACCEPT_LEGACY_TOKEN=false` and redeploy: the static secret is then never read and can be deleted. `health` is a read-only tool for checking that a token and the route work.

### The (legacy) static token

The token is the `SecretString` of a Secrets Manager secret. The template parameter `McpAuthTokenSecretName` is the **name** of that secret (default `toolweave/mcp-auth-token`). The token is not a CloudFormation parameter and is not stored in this repository. The Lambda reads it with `secretsmanager:GetSecretValue` on that secret only, and caches a successful read for 60 seconds. A missing name, a missing secret, an empty secret, a missing header, or a wrong token all fail closed: HTTP 401 with a JSON-RPC error (`code` `-32001`). Nothing in that response says which of those it was.

Create the secret before the deploy that turns this check on. The workflow does not create it. If the secret is not there yet, the stack can still deploy, and every `/mcp` call returns 401 until the secret exists. Creating or rotating it does not need another deploy. After a rotation, wait for the 60 second cache or start a new execution environment.

```bash
aws secretsmanager create-secret \
  --name toolweave/mcp-auth-token \
  --region us-east-1 \
  --secret-string "$(openssl rand -hex 32)"
```

The secret string is the raw token, not JSON. To rotate it later:

```bash
aws secretsmanager put-secret-value \
  --secret-id toolweave/mcp-auth-token \
  --region us-east-1 \
  --secret-string "$(openssl rand -hex 32)"
```

A repository variable `MCP_AUTH_TOKEN_SECRET_NAME` can rename the secret. An unset or empty variable keeps `toolweave/mcp-auth-token`. The deploy refuses to ship a blank name: an empty override would replace the template default and leave `/mcp` rejecting every caller.

Send the token on every `/mcp` request:

```http
Authorization: Bearer <token>
```

`mcp-remote` (the Grok connector transport) forwards that header. Put the token in the environment of the connector, not in git:

```json
{
  "mcpServers": {
    "toolweave": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://<api-id>.execute-api.us-east-1.amazonaws.com/mcp",
        "--header",
        "Authorization:${TOOLWEAVE_AUTHORIZATION}"
      ],
      "env": {
        "TOOLWEAVE_AUTHORIZATION": "Bearer <token>"
      }
    }
  }
}
```

The URL is the `ToolWeaveApiEndpoint` stack output plus `/mcp`. The local `toolweave` process does not read the secret; the check runs in the Lambda entrypoint.

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

- **`/mcp` requires `Authorization: Bearer <token>`.** A CipherWeave access token for `toolweave`, or — while `AcceptLegacyToken` is true — the static token loaded from Secrets Manager. A missing or empty secret fails closed.
- **Read operations (`GET`)** execute directly.
- **Write operations** are gated through proposal/commit workflow.
- Proposals are scored by `mcp-observatory` and can be blocked before execution.
- Commits require token verification.
- Proposals are persisted in DynamoDB with TTL for cross-invocation durability.
- **Per-tool scopes** (`mcp_auth.TOOL_POLICIES`, `cipherweave_verify.ToolAuthorizer`): `pre_tool` and `health` need `read`; `post_tool` and `reload_catalog` need `execute`; `commit_api_call` needs `execute` and **cannot be called with a delegated (RFC 8693) token** — an agent acting for someone can plan and propose, the principal commits. A refusal is `403` with an `insufficient_scope` challenge. `ToolScopeMode=audit` (repository variable `TOOL_SCOPE_MODE`) logs scope failures instead of refusing them while clients are granted scopes; the delegation rule holds in both modes.
- **The plan is bound to the catalog** (`plan_guard.bind`). `post_tool`'s plan is a tool argument, so before this a caller could name any base URL, path and headers and the Lambda would fetch it from inside the VPC and return the body (SSRF). Now method, base URL and path template must match a cataloged endpoint, path parameters are single segments (encoded traversal refused), query parameters and headers must be declared by the spec, and `Authorization`/`Host`/`Cookie`/forwarding headers are never taken from a plan. The HTTP method — not the plan's `execution_type` — decides whether a call runs now (GET) or becomes a proposal.
- **Egress is public-only, and pinned** (`plan_guard.resolve_egress`, in the executor, for GETs and commits alike): loopback, private, link-local (Lambda runtime API, instance metadata), multicast and reserved addresses are refused, as literals and by DNS; redirects are not followed. The executor then connects to the address the check approved, with `Host` and the TLS SNI (and so the certificate check) still naming the host, so a rebinding resolver gets no second lookup. `TOOLWEAVE_ALLOW_PRIVATE_EGRESS=true` opts out for local development.
- **Per-API credentials** (`api_credentials.py`). The executor used to send `Authorization: Bearer 123` to every cataloged API. Now an API gets a credential only if its owner stores one in Secrets Manager at `toolweave/api/<api_id>`: `{"host": "api.example.com", "type": "bearer", "token": "…"}`, or `"type": "header"` with `name`/`value`, or `"type": "basic"` with `username`/`password`. `host` is required and must match the request, so a credential never leaves for another host. No secret, no credential header; a secret that exists but cannot be read or parsed refuses the call. Cached five minutes; the Lambda can read only `toolweave/api/*`. A proposal binds its `api_id` under the commit token, so the commit uses the proposed API's credential.
- **DPoP and discovery.** `Authorization: DPoP <token>` with a `DPoP` proof is accepted for sender-constrained CipherWeave tokens; `GET /.well-known/oauth-protected-resource` serves RFC 9728 metadata, and every `401` points at it.

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

## Cost attribution

The stack is deployed with `repo=ToolWeave`, which CloudFormation copies onto
every resource. Bedrock calls go through this stack's own application inference
profiles (Claude Haiku 4.5, Nova Pro and Llama 3.3 70B: the `reasoning`
candidates), tagged the same way. Model spend is therefore billed to ToolWeave
too. `src/toolweave/bedrock_cost.py` is vendored from ModelWeave and checksummed
in `BEDROCK_COST_VENDORED.json`. If a profile is refused, the call falls back to
the model id and logs it, so attribution never costs a call. `BedrockCostAttribution=off`
disables it. Read the costs back with ModelWeave's `scripts/weave_costs.py --repo ToolWeave [--llm]`.
