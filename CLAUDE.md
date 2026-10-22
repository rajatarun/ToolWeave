# CLAUDE.md – ToolWeave

> Project context for Claude and other LLM-based tools working in this repository.
> The platform-wide engineering standard is `docs/succession.md` in
> [ContextWeave](https://github.com/rajatarun/ContextWeave/blob/main/docs/succession.md).
> Module-level how-tos (adding a tool, changing models, the ingestion pipeline)
> are in `docs/claude.md`. Where the two disagree, this file and `README.md`
> are current.

---

## Project identity

**Name**: ToolWeave — turns natural-language requests into safe REST API calls from OpenAPI specs
**Owner**: Tarun Raja (rajatarun)
**Account**: AWS `239571291755`, `us-east-1`, SAM via GitHub Actions OIDC

There are five MCP tools: `pre_tool` (plan), `post_tool` (execute a GET or
propose a write), `commit_api_call`, `reload_catalog` and `health`. Writes go
through mcp-observatory's propose/commit gate. Field semantics come from
DataDictionary.

The specs it serves are declared in `src/Swagger/`: today DeviceWeave and the
content orchestrator.

---

## Invariants — do not break these

1. **The agent never invents field values.** Missing required fields mean
   `status="needs_input"` with one question. Never a guessed id.
2. **Writes always go through the observatory.** `POST/PUT/PATCH/DELETE`
   become a proposal and execute only after `commit_api_call` verifies the
   token. A delegated (RFC 8693) token can plan and propose, but cannot
   commit: the principal commits.
3. **The plan is bound to the catalog** (`plan_guard.bind`). The plan is a tool
   *argument*, so a caller could otherwise name any URL and get the Lambda to
   fetch it from inside the VPC (SSRF).
   - Method, base URL and path template must match a cataloged endpoint.
   - Path parameters are single segments; encoded traversal is refused.
   - Query parameters and headers must be declared by the spec.
   - Authority headers (`Authorization`, `Host`, `Cookie`, forwarding headers)
     are never taken from a plan.
4. **Egress is public-only, and pinned** (`plan_guard.resolve_egress`).
   - Loopback, private, link-local, multicast and reserved addresses are
     refused, both as literals and by DNS. Redirects are not followed.
   - The executor connects to the address the check approved, keeping
     `Host`/SNI. A rebinding resolver gets no second lookup.
   - `TOOLWEAVE_ALLOW_PRIVATE_EGRESS` is for local development only.
5. **Per-API credentials, host-bound** (`api_credentials.py`).
   - A credential is sent only if its owner stored one at
     `toolweave/api/<api_id>`, and only to the `host` it names.
   - A secret that exists but cannot be read refuses the call; it is never sent
     unauthenticated.
   - This replaced a placeholder `Bearer 123` that went to every API.
6. **A spec with an unresolved server template is refused.**
   `scripts/publish_specs.py` rewrites `servers` from the sibling stack's
   output. The OAS3 default (`https://example.execute-api…`) looks valid,
   resolves nowhere, and would catalog every endpoint against a host that does
   not exist.
7. **Withdrawal is two halves.** Removing a spec from `src/Swagger/` prunes it
   from the bucket, and `Object Deleted` drops both its endpoints and its
   metadata row. The rule once listened for `Object Created` only, so deleted
   APIs stayed callable.
8. **The in-memory catalog is the hot path.** No DynamoDB reads in `pre_tool`;
   only `reload_catalog` rescans.
9. **Enrichment failures are non-fatal.** The catalog write succeeds with
   partially enriched entries.
10. **`api_id` hashing is fixed.** It is a 12-char SHA-256 of the S3 key.
    Changing it orphans the existing catalog.
11. **EventBridge, not direct S3 notifications.** Direct notifications create a
    CloudFormation dependency cycle.

---

## Auth

`/mcp` takes a CipherWeave token (`aud` = `toolweave`, Bearer or DPoP), or the
static token while `AcceptLegacyToken` is true.

| Tool | Scope needed |
|---|---|
| `pre_tool`, `health` | `read` |
| `post_tool`, `reload_catalog` | `execute` |
| `commit_api_call` | `execute`, not delegated |

`ToolScopeMode=audit` logs scope failures instead of refusing them, during
migration.

API Gateway forwards other paths to the same Lambda unchecked. Do not add a
route there that assumes it is authenticated.

## Shared code — never edit in place

`src/cipherweave_verify/` (CipherWeave), `modelweave_roles.py` and
`bedrock_cost.py` (ModelWeave) are vendored and checksummed. The planner's
model comes from ModelWeave's `reasoning` role pin, with `BEDROCK_MODEL_ID` as
the fallback. Spans go to the shared `OBSERVATORY_METRICS` table under contract
(`tests/test_shared_table_contract.py`).

## Working here

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q     # what CI runs
ruff check .
```

Tests use `moto`. Deploy: push to `main` → `.github/workflows/deploy.yml`.
