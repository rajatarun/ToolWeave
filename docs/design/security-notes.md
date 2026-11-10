# Security notes (threat-oriented)

ToolWeave sits between an MCP client and arbitrary REST APIs described by
OpenAPI. This is a short threat model for adopters and contributors. It is not a
formal assurance document.

## Assets

- MCP bearer tokens (CipherWeave JWT or legacy static secret)
- Per-API credentials in Secrets Manager (`toolweave/api/<api_id>`)
- OpenAPI specs in S3 (integrity of the catalog)
- Proposal and commit state (mcp-observatory)
- Data returned from target APIs (may contain PII)

## Trust boundaries

| Zone | Trust level |
|------|-------------|
| MCP client | Semi-trusted — authenticated, but can supply malicious **plans** as tool arguments |
| ToolWeave Lambda | Trusted enforcement point |
| Target REST APIs | Untrusted network peers (TLS, host-bound credentials) |
| Bedrock / DataDictionary | Trusted services with their own account policies |

## Threats and mitigations

### SSRF and lateral movement

**Threat:** A client passes a crafted `api_call_plan` so the Lambda fetches
internal URLs (metadata, VPC services).

**Mitigations:**

- `plan_guard.bind` — method, host, path template, and declared params must
  match a catalog row; authority headers are not taken from plans.
- `plan_guard.resolve_egress` — public addresses only; DNS pinning at connect;
  redirects disabled. Local override: `TOOLWEAVE_ALLOW_PRIVATE_EGRESS` (dev only).

### Unauthorized writes

**Threat:** Agent or client executes destructive API calls without approval.

**Mitigations:**

- Non-GET methods become observatory **proposals**; execution requires
  `commit_api_call` with a verified commit token.
- Risk scoring can block high-risk proposals (`OBSERVATORY_BLOCK_THRESHOLD`).

### Token scope abuse

**Threat:** Stolen read-only token used to execute writes.

**Mitigations:**

- Per-tool OAuth-style scopes (`read` vs `execute`) on MCP tools.
- Write commit may require a non-delegated principal token (see `mcp_auth` and
  vendored verifier behavior).

### Credential mis-binding

**Threat:** API A's secret sent to API B.

**Mitigations:**

- Per-credential `host` must match request host (`api_credentials.py`).
- Proposals bind `api_id` under the commit token.

### Spec poisoning

**Threat:** Malicious OpenAPI uploaded to catalog internal endpoints.

**Mitigations:**

- Specs with unresolved server templates are refused at ingest.
- Egress check blocks internal `servers` URLs even if cataloged.
- Control who can write to the specs bucket and who can call `reload_catalog`.

### Authentication to `/mcp`

**Threat:** Unauthenticated MCP access.

**Mitigations:**

- Bearer (and optional DPoP) on `/mcp` in deployed configuration; legacy static
  token supported during migration (`AcceptLegacyToken`).
- Other API Gateway routes forwarded to the same Lambda are **not** authenticated
  — do not add sensitive routes there without checks.

## What ToolWeave does not provide

- Encryption of target API payloads at rest in proposals (DynamoDB TTL applies).
- WAF or rate limiting (configure on API Gateway).
- Replacement for target API authorization — ToolWeave forwards credentials you
  configure, not end-user OAuth to third parties.

## Reporting

See `SECURITY.md` for vulnerability disclosure.
