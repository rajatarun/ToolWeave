# Examples

Runnable snippets for adopters. None of these require a deployed ToolWeave stack
unless noted.

## Parse OpenAPI offline

Install the package (or rely on `sys.path` hack in the script):

```bash
pip install -e .
python3 examples/parse_spec.py
```

Expected: prints four operations from `minimal-openapi.yaml` and base URL
`https://api.example.com/v1`.

## MCP client configuration

`mcp_client_config.example.json` shows how to connect an MCP client through
`mcp-remote` to your deployed `/mcp` endpoint. Replace the URL and token with
your values; keep tokens out of git.

## Core MCP workflow (deployed server)

After deploy and auth setup:

```json
{"tool": "health", "arguments": {}}
```

```json
{"tool": "pre_tool", "arguments": {"prompt": "List all pets"}}
```

When `pre_tool` returns `status: "ready"`, pass the plan object to:

```json
{"tool": "post_tool", "arguments": {"api_call_plan": "<plan from pre_tool>"}}
```

For write operations, complete with `commit_api_call` when a proposal is allowed.

See `docs/OPERATIONS.md` for authentication and scopes.
