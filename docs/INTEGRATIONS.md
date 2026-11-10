# Integration opportunities

ToolWeave is a **Model Context Protocol (MCP) server** that plans and executes
REST calls from OpenAPI catalogs. Below are ecosystems where an adapter or
connector would help adoption. Effort is a rough engineering estimate for a
production-quality adapter with docs and tests.

| Integration | Fit | Implemented today | Adapter sketch | Effort |
|-------------|-----|-------------------|----------------|--------|
| **MCP clients** (Claude Desktop, VS Code MCP, `mcp-remote`, custom agents) | **High** | **Yes** — HTTP `/mcp` with five tools | Point the client at your deployed endpoint; send `Authorization: Bearer …`. See `examples/mcp_client_config.example.json`. | Low (config only) |
| **Amazon Bedrock / AgentCore** | **High** | **Partial** — planning and enrichment already call Bedrock Converse; deployment is SAM/Lambda | AgentCore Runtime could host the same FastMCP app; swap Mangum entry for AgentCore HTTP adapter; reuse `plan_guard` and observatory gate. | Medium |
| **OpenTelemetry** | **Medium** | **Partial** — invocation metrics go to shared observatory DynamoDB table per `contracts/` | Export spans from `pre_tool` / `post_tool` via OTLP alongside existing metrics rows; optional sidecar in SAM template. | Medium |
| **LangChain / LangGraph** | **Medium** | No | Wrap MCP as LangChain tools (`StructuredTool`) calling `pre_tool` → `post_tool` → optional `commit_api_call`; graph node for human approval on proposals. | Medium |
| **OpenAI / Anthropic tool-use SDKs** | **Medium** | No | Thin client that maps SDK tool schemas to MCP JSON-RPC; reuse session id for multi-turn `needs_input`. | Medium |
| **CI/CD (GitHub Actions, GitLab)** | **Medium** | **Partial** — repo workflow runs tests and deploys SAM | Action step: `publish_specs.py` + `reload_catalog` after spec change; gate on `pytest` and contract checks. | Low–medium |
| **API gateways / Kong / Apigee** | **Low** | No | ToolWeave calls **outbound** APIs; ingress is API Gateway today. A portal could publish specs into the ToolWeave bucket. | Medium–high |
| **Kubernetes / Helm** | **Low** | No | Containerize Uvicorn `toolweave` entrypoint; chart for env vars and IRSA for DynamoDB/S3/Bedrock. AWS SAM remains primary path. | High |

## High-fit details

### MCP clients (implemented)

ToolWeave exposes:

- `pre_tool`, `post_tool`, `commit_api_call`, `reload_catalog`, `health`

Writes require the observatory proposal/commit flow. Clients should treat
`needs_input` as a single follow-up question, not a batch form.

**Adapter:** configuration only unless you need stdio transport; the deployed
server uses streamable HTTP via FastMCP + Mangum.

### Bedrock and AgentCore

The planner (`src/toolweave/agent.py`) and enricher (`src/toolweave/endpoint_enricher.py`)
use Bedrock Converse. Model id can fall back to `BEDROCK_MODEL_ID`; production
deployments may resolve models via ModelWeave role pins (`modelweave_roles.py`).

**Adapter:** package the Lambda handler as an AgentCore agent with the same env
vars; ensure `plan_guard` and per-API credentials remain on the execution path.

### LangChain / LangGraph

**Adapter outline:**

1. `ToolWeaveSession` holds MCP HTTP client + auth header.
2. `plan(prompt)` → `pre_tool`; loop on `needs_input`.
3. `execute(plan)` → `post_tool`; branch on `immediate` vs `proposal`.
4. LangGraph interrupt before `commit_api_call`.

No LangChain dependency belongs in this repository core; keep adapters in
`examples/` or a sibling repo.

## Low-fit (skip unless you have a specific need)

- **Embedding OpenAPI in vector DBs** — ToolWeave already keyword-searches an
  in-memory catalog (`catalog_search.py`); RAG on raw specs duplicates ingestion.
- **GraphQL** — Out of scope unless you add a GraphQL-to-REST bridge upstream.

## Related reading

- `docs/EXTENDING.md` — add tools, specs, and credentials inside this repo.
- `docs/architecture.md` — runtime diagram and data flows.
- `examples/README.md` — offline and client examples.
