# ToolWeave

Convert any Swagger/OpenAPI specification into an [MCP](https://modelcontextprotocol.io) server where **every API endpoint becomes an AI agent tool**. Deploy locally or on AWS Lambda + API Gateway so AI agents can call your APIs out of the box.

## How It Works

```
Swagger/OpenAPI spec
       │
       ▼
  ToolWeave parser
  (one MCP tool per endpoint)
       │
       ▼
  MCP Server (Streamable HTTP)
       │
       ├── Local: Express HTTP server
       └── Cloud: AWS Lambda + API Gateway HTTP API
```

Each tool exposes:
- **Name** — derived from `operationId` or `{method}_{path}`
- **Description** — from the spec's `summary` / `description`
- **Input schema** — JSON Schema built from path/query/header parameters and the request body
- **Execution** — makes the real HTTP call and returns the response

## Quick Start

### Install

```bash
npm install
npm run build
```

### List tools from a spec

```bash
node dist/index.js list examples/petstore.yaml
# or from a URL:
node dist/index.js list https://petstore3.swagger.io/api/v3/openapi.json
```

### Start a local MCP server

```bash
node dist/index.js serve examples/petstore.yaml --port 3000
```

The MCP endpoint is at `http://localhost:3000/mcp`.

**With overrides:**

```bash
node dist/index.js serve ./my-api.yaml \
  --base-url https://staging.api.example.com \
  --header "Authorization:Bearer $TOKEN" \
  --header "X-Tenant:acme" \
  --port 8080
```

### Connect an AI agent

Use any MCP client (e.g. Claude Desktop, Claude Code) and point it at:

```
http://localhost:3000/mcp
```

---

## AWS Deployment

### Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- AWS credentials configured

### Steps

1. **Build** the project:

   ```bash
   npm run build
   ```

2. **Copy your spec** into the project root (it will be bundled with the Lambda):

   ```bash
   cp /path/to/your-api.yaml ./swagger.yaml
   ```

3. **Deploy** with SAM:

   ```bash
   cd deploy
   sam build -t template.yaml --build-dir ../.aws-sam/build
   sam deploy -t template.yaml --config-file samconfig.toml
   ```

4. **Get the endpoint** from the CloudFormation outputs:

   ```
   McpEndpoint: https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/mcp
   ```

### Lambda Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SWAGGER_SPEC` | Yes | Path (relative to Lambda root) or URL of the spec |
| `BASE_URL` | No | Override the base URL from the spec |
| `DEFAULT_HEADERS` | No | Comma-separated `Name:Value` headers added to every request |
| `REQUEST_TIMEOUT_MS` | No | Per-request timeout (default: 30000) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        AI Agent                         │
│        (Claude, GPT, any MCP-compatible client)         │
└─────────────────┬───────────────────────────────────────┘
                  │  MCP Streamable HTTP
                  ▼
┌─────────────────────────────────────────────────────────┐
│              AWS API Gateway HTTP API                   │
│                    POST /mcp                            │
│                    GET  /mcp  (SSE)                     │
│                    DELETE /mcp                          │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────┐
│                   AWS Lambda                            │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │              ToolWeave MCP Server               │   │
│  │                                                 │   │
│  │  tools/list  ──►  [tool1, tool2, ..., toolN]   │   │
│  │                                                 │   │
│  │  tools/call  ──►  HTTP request to target API   │   │
│  └─────────────────────────────────────────────────┘   │
└─────────────────┬───────────────────────────────────────┘
                  │  HTTP/S
                  ▼
┌─────────────────────────────────────────────────────────┐
│              Your API (any HTTP API)                    │
└─────────────────────────────────────────────────────────┘
```

## Project Structure

```
ToolWeave/
├── src/
│   ├── swagger/
│   │   ├── parser.ts        # OpenAPI 2.0 & 3.0 parser → ParsedEndpoint[]
│   │   └── types.ts         # Core TypeScript interfaces
│   ├── tools/
│   │   └── executor.ts      # Executes HTTP calls for a tool invocation
│   ├── mcp/
│   │   └── server.ts        # MCP Server (tools/list + tools/call handlers)
│   ├── handlers/
│   │   ├── http.ts          # Express server (local development)
│   │   └── lambda.ts        # AWS Lambda handler for API Gateway HTTP API
│   ├── index.ts             # CLI entry point
│   └── lambda-entry.ts      # Lambda bootstrap (env-var driven)
├── examples/
│   └── petstore.yaml        # Sample OpenAPI 3.0 spec
├── deploy/
│   ├── template.yaml        # AWS SAM template
│   └── samconfig.toml       # SAM deployment defaults
├── package.json
└── tsconfig.json
```

## License

Apache 2.0
