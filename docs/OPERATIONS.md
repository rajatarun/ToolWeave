# Operations and deployment

Detailed setup for running ToolWeave in AWS or locally. For a short overview,
see `README.md`.

## AWS requirements

ToolWeave is designed to run on AWS. A minimal production footprint includes:

- Lambda (ToolWeave + Swagger processor)
- DynamoDB (catalog, metadata, proposals)
- S3 (OpenAPI specs bucket)
- EventBridge (S3 object events)
- API Gateway HTTP API (`/mcp`)
- Bedrock Runtime (planner and enricher)
- Secrets Manager (MCP token, per-API credentials)

Optional or environment-specific: CipherWeave JWKS, DataDictionary URL,
ModelWeave SSM pins, shared observatory metrics table.

There is **no fully offline production mode** without replacing Bedrock and
DynamoDB with mocks (tests do this via moto).

## Deploy with SAM

```bash
sam build
sam deploy
```

Configure stack parameters per your account. See `template.yaml` and
`samconfig.toml` for defaults. Create the MCP auth secret before relying on
`/mcp`:

```bash
aws secretsmanager create-secret \
  --name toolweave/mcp-auth-token \
  --secret-string "$(openssl rand -hex 32)"
```

Use your own region and secret name as needed.

## Spec publishing

Specs live in `src/Swagger/`. The publish script rewrites `servers` URLs from
sibling stack outputs so catalogs never point at placeholder hosts. See
`scripts/publish_specs.py` and `README.md` ingestion section in the previous
long-form doc (same behavior).

## Configuration reference

Copy `.env.example` to `.env` for local runs and replace placeholder values.

| Variable | Purpose |
|----------|---------|
| `CATALOG_TABLE_NAME` | Endpoint catalog DynamoDB table |
| `META_TABLE_NAME` | API metadata table |
| `PROPOSALS_TABLE_NAME` | Proposal persistence |
| `OBSERVATORY_METRICS_TABLE` | Shared metrics table |
| `OBSERVATORY_SECRET_KEY` | HMAC for proposal/commit tokens |
| `BEDROCK_MODEL_ID` | Fallback Bedrock model for planner |
| `DATA_DICTIONARY_URL` | Optional field semantics service |
| `MCP_AUTH_TOKEN_SECRET_NAME` | Secrets Manager name for legacy bearer token |
| `TOOLWEAVE_ALLOW_PRIVATE_EGRESS` | Dev-only: allow non-public egress targets |

## MCP authentication

Deployed `/mcp` requires `Authorization: Bearer <token>`. Tokens may be
short-lived JWTs from a CipherWeave issuer (`aud=toolweave`) when JWKS URL is
configured, or the legacy static secret while `AcceptLegacyToken` is enabled.

Detailed scope matrix:

| Tool | Scope |
|------|-------|
| `pre_tool`, `health` | `read` |
| `post_tool`, `reload_catalog` | `execute` |
| `commit_api_call` | `execute` (principal; delegated tokens cannot commit) |

OAuth protected resource metadata is served at `/.well-known/oauth-protected-resource`.

## Local processes

```bash
source .venv/bin/activate
toolweave   # Uvicorn on 0.0.0.0:8080 — auth behavior depends on env
```

```bash
sam build && sam local start-api
```

## Security

See `docs/design/security-notes.md` and `SECURITY.md`.
