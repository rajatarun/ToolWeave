# Launch draft (not published)

Use this as a starting point for a blog post, README announcement, or community
post. Edit before publishing; limits below are accurate to the codebase today.

---

## Draft post

### Title idea

**ToolWeave: OpenAPI specs as safe MCP tools for REST APIs**

### Problem

Agents and MCP clients are good at calling *something*, but not at calling
*your* API safely. Hand-written tools drift from the spec. Blind HTTP from a
Lambda is an SSRF risk. Writes need a human or policy gate, not instant execution.

### What ToolWeave does

ToolWeave is an MCP server that:

1. Ingests OpenAPI/Swagger into a searchable catalog.
2. Plans calls from natural language (`pre_tool`) without inventing missing IDs.
3. Executes GETs immediately and turns writes into **proposals** verified via
   mcp-observatory before commit.
4. Binds every outbound request to cataloged endpoints and public egress rules.

### Who it is for

- Teams exposing internal REST APIs to AI assistants through MCP.
- Platform engineers already on AWS (Lambda, DynamoDB, Bedrock, SAM).
- Anyone who wants spec-driven tool catalogs instead of one-off integrations.

### Quick proof

Clone the repo, `pip install -e ".[dev]"`, run `python3 -m pytest tests/ -q`
— **170+ offline tests** (synthetic/moto) exercise parser, plan guard, auth, and
contracts.

### Honest limits

- **AWS-first:** Production path is SAM deploy; local `toolweave` still expects
  configured AWS services for full functionality.
- **Bedrock:** Planning and enrichment need model access (or mocks in tests).
- **Sibling services:** DataDictionary and CipherWeave integrate when URLs and
  JWKS are configured; they are not bundled in this repo.
- **No hosted SaaS:** You operate the stack.

### Call to action

Try the quickstart in `README.md`, add your OpenAPI under `src/Swagger/`, and
wire your MCP client using `examples/mcp_client_config.example.json`.

---

## Communities where this fits (draft)

| Community | Angle |
|-----------|--------|
| **Model Context Protocol** | Reference MCP server for OpenAPI-backed tools |
| **AWS builders / serverless** | SAM, Lambda, Bedrock Converse patterns |
| **OpenAPI / API design** | Living catalog from specs, withdrawal on delete |
| **AI engineer / LLM ops** | Safe write gating and scope-separated tools |
| **mcp-observatory users** | Proposal/commit workflow for REST side effects |

## Launch checklist (maintainers)

- [ ] Confirm demo GIF or architecture diagram in README
- [ ] Tag a release version in `pyproject.toml` if publishing to PyPI (not done today)
- [ ] Sanitize `.env.example` for public clones
- [ ] Enable GitHub Security Advisories
- [ ] Post to MCP and AWS communities with limits section intact
