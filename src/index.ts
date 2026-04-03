#!/usr/bin/env node
import { Command } from "commander";
import { parseSwagger } from "./swagger/parser.js";
import { createMcpServer } from "./mcp/server.js";
import { startHttpServer } from "./handlers/http.js";
import { ExecuteOptions } from "./tools/executor.js";

const program = new Command();

program
  .name("toolweave")
  .description(
    "Convert Swagger/OpenAPI specs into MCP tools and serve them over HTTP or AWS Lambda"
  )
  .version("1.0.0");

program
  .command("serve")
  .description("Parse a Swagger/OpenAPI spec and start an MCP HTTP server")
  .argument("<spec>", "Path or URL to the Swagger/OpenAPI spec file")
  .option("-p, --port <port>", "HTTP port to listen on", "3000")
  .option(
    "-b, --base-url <url>",
    "Override the base URL from the spec (e.g. https://api.example.com)"
  )
  .option(
    "-H, --header <header...>",
    "Default headers to send with every API request (format: Name:Value)"
  )
  .option(
    "-t, --timeout <ms>",
    "Request timeout in milliseconds",
    "30000"
  )
  .action(async (spec: string, opts: Record<string, unknown>) => {
    console.log(`Parsing spec: ${spec}`);

    const swaggerResult = await parseSwagger(spec);

    console.log(
      `Loaded "${swaggerResult.title}" v${swaggerResult.version} — ${swaggerResult.endpoints.length} endpoints converted to MCP tools`
    );

    const executeOptions: ExecuteOptions = {
      baseUrlOverride: opts["baseUrl"] as string | undefined,
      timeoutMs: parseInt(String(opts["timeout"] ?? "30000"), 10),
      defaultHeaders: parseHeaders(opts["header"] as string[] | undefined),
    };

    const port = parseInt(String(opts["port"] ?? "3000"), 10);

    startHttpServer(() => createMcpServer(swaggerResult, { executeOptions }), port);
  });

program
  .command("list")
  .description("List all tools generated from a Swagger/OpenAPI spec")
  .argument("<spec>", "Path or URL to the Swagger/OpenAPI spec file")
  .action(async (spec: string) => {
    const swaggerResult = await parseSwagger(spec);

    console.log(`\n"${swaggerResult.title}" v${swaggerResult.version}`);
    console.log(`Base URL: ${swaggerResult.baseUrl}`);
    console.log(`\n${swaggerResult.endpoints.length} tools:\n`);

    for (const endpoint of swaggerResult.endpoints) {
      console.log(`  ${endpoint.toolName}`);
      console.log(`    ${endpoint.method} ${endpoint.path}`);
      console.log(`    ${endpoint.description.split("\n")[0]}`);
      console.log();
    }
  });

program.parse();

function parseHeaders(rawHeaders: string[] | undefined): Record<string, string> {
  const headers: Record<string, string> = {};
  if (!rawHeaders) return headers;

  for (const raw of rawHeaders) {
    const idx = raw.indexOf(":");
    if (idx === -1) {
      console.warn(`Ignoring malformed header (expected Name:Value): ${raw}`);
      continue;
    }
    const name = raw.slice(0, idx).trim();
    const value = raw.slice(idx + 1).trim();
    headers[name] = value;
  }

  return headers;
}
