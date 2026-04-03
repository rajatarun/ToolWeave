#!/usr/bin/env node
import { Command } from "commander";
import { parseSwagger } from "./swagger/parser.js";
import { createMcpServer } from "./mcp/server.js";
import { startHttpServer } from "./handlers/http.js";
import { ExecuteOptions } from "./tools/executor.js";
import { buildDataDictionary } from "./swagger/data-dictionary.js";
import {
  DataDictionaryClient,
  buildFieldDescription,
} from "./integrations/data-dictionary-client.js";

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
  .option(
    "-d, --data-dictionary-url <url>",
    "DataDictionary MCP server URL — adds dd_get_field, dd_search_fields, dd_list_fields_by_api tools"
  )
  .action(async (spec: string, opts: Record<string, unknown>) => {
    console.log(`Parsing spec: ${spec}`);

    const swaggerResult = await parseSwagger(spec);

    const ddUrl = opts["dataDictionaryUrl"] as string | undefined;
    console.log(
      `Loaded "${swaggerResult.title}" v${swaggerResult.version} — ${swaggerResult.endpoints.length} endpoints converted to MCP tools` +
      (ddUrl ? ` + DataDictionary connected` : "")
    );

    const executeOptions: ExecuteOptions = {
      baseUrlOverride: opts["baseUrl"] as string | undefined,
      timeoutMs: parseInt(String(opts["timeout"] ?? "30000"), 10),
      defaultHeaders: parseHeaders(opts["header"] as string[] | undefined),
    };

    const port = parseInt(String(opts["port"] ?? "3000"), 10);

    startHttpServer(
      () => createMcpServer(swaggerResult, { executeOptions, dataDictionaryUrl: ddUrl }),
      port
    );
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

program
  .command("sync")
  .description(
    "Parse a Swagger/OpenAPI spec and push all field definitions to the DataDictionary MCP server"
  )
  .argument("<spec>", "Path or URL to the Swagger/OpenAPI spec file")
  .requiredOption(
    "-d, --data-dictionary-url <url>",
    "Base URL of the DataDictionary MCP server (e.g. https://abc.lambda-url.us-east-1.on.aws)"
  )
  .option(
    "--dry-run",
    "Parse and show what would be synced without calling DataDictionary",
    false
  )
  .option(
    "--concurrency <n>",
    "Number of parallel propose+commit calls",
    "3"
  )
  .action(async (spec: string, opts: Record<string, unknown>) => {
    const dryRun = Boolean(opts["dryRun"]);
    const concurrency = parseInt(String(opts["concurrency"] ?? "3"), 10);
    const ddUrl = String(opts["dataDictionaryUrl"]);

    console.log(`\nParsing spec: ${spec}`);
    const dict = await buildDataDictionary(spec);
    console.log(`Loaded "${dict.title}" v${dict.version}`);

    // Collect all unique fields across all endpoints (params + request body + responses)
    type FieldJob = {
      fieldName: string;
      fieldType: string;
      description?: string;
      endpointPath: string;
      endpointMethod: string;
      required: boolean;
      example?: unknown;
      enumValues?: unknown[];
      format?: string;
    };

    const fieldJobs: FieldJob[] = [];
    const seen = new Set<string>();

    for (const ep of dict.endpoints) {
      for (const f of ep.parameters) {
        const key = `${f.name}:${ep.path}`;
        if (seen.has(key)) continue;
        seen.add(key);
        fieldJobs.push({
          fieldName: f.name,
          fieldType: f.type,
          description: f.description,
          endpointPath: ep.path,
          endpointMethod: ep.method,
          required: f.required,
          example: f.example,
          enumValues: f.enum,
          format: f.format,
        });
      }

      if (ep.requestBody) {
        for (const f of ep.requestBody.fields) {
          const key = `body.${f.path}:${ep.path}`;
          if (seen.has(key)) continue;
          seen.add(key);
          fieldJobs.push({
            fieldName: f.name,
            fieldType: f.type,
            description: f.description,
            endpointPath: ep.path,
            endpointMethod: ep.method,
            required: f.required,
            example: f.example,
            format: f.format,
          });
        }
      }
    }

    console.log(`\nFound ${fieldJobs.length} unique fields to sync.\n`);

    if (dryRun) {
      for (const j of fieldJobs) {
        const desc = buildFieldDescription({
          ...j,
          apiName: dict.title,
          apiVersion: dict.version,
        });
        console.log(`[DRY RUN] ${j.fieldName} (${j.endpointMethod} ${j.endpointPath})`);
        console.log(`  → ${desc}\n`);
      }
      console.log("Dry run complete. No data was sent to DataDictionary.");
      return;
    }

    const client = new DataDictionaryClient(ddUrl);
    let synced = 0;
    let skipped = 0;
    let failed = 0;

    // Process in batches of `concurrency`
    for (let i = 0; i < fieldJobs.length; i += concurrency) {
      const batch = fieldJobs.slice(i, i + concurrency);

      await Promise.all(
        batch.map(async (job) => {
          const desc = buildFieldDescription({
            ...job,
            apiName: dict.title,
            apiVersion: dict.version,
          });

          try {
            const proposal = await client.proposeDataElement(desc);

            if (proposal.status === "blocked") {
              console.warn(
                `  BLOCKED  ${job.fieldName}: ${proposal.message ?? "Observatory rejected"}`
              );
              skipped++;
              return;
            }

            if (!proposal.commit_token) {
              console.warn(`  SKIPPED  ${job.fieldName}: no commit token returned`);
              skipped++;
              return;
            }

            await client.commitDataElement(proposal.proposal_id, proposal.commit_token);
            console.log(`  SYNCED   ${job.fieldName} (${job.endpointMethod} ${job.endpointPath})`);
            synced++;
          } catch (err) {
            const msg = err instanceof Error ? err.message : String(err);
            console.error(`  FAILED   ${job.fieldName}: ${msg}`);
            failed++;
          }
        })
      );
    }

    console.log(
      `\nSync complete: ${synced} synced, ${skipped} skipped, ${failed} failed.`
    );
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
