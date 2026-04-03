import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { ParsedEndpoint, SwaggerParseResult } from "../swagger/types.js";
import { executeEndpoint, ExecuteOptions } from "../tools/executor.js";
import { DataDictionaryClient } from "../integrations/data-dictionary-client.js";

export interface ToolWeaveServerOptions {
  executeOptions?: ExecuteOptions;
  /**
   * Base URL of the DataDictionary MCP server.
   * When provided, ToolWeave registers three additional lookup tools
   * that proxy to the DataDictionary so agents can query field meanings
   * without connecting to a second MCP server.
   */
  dataDictionaryUrl?: string;
}

// ─── Tool definitions for DataDictionary proxied lookups ─────────────────────

const DD_TOOLS: Tool[] = [
  {
    name: "dd_get_field",
    description:
      "Look up the DataDictionary definition for an API field by its exact name. " +
      "Returns type, meaning, constraints, examples, and related fields.",
    inputSchema: {
      type: "object",
      properties: {
        field_name: {
          type: "string",
          description: "The exact API field/parameter name to look up (case-sensitive)",
        },
      },
      required: ["field_name"],
    },
  },
  {
    name: "dd_search_fields",
    description:
      "Search the DataDictionary for fields by keyword. " +
      "Searches across field names and their AI-generated meanings.",
    inputSchema: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "Free-text search query (e.g. 'pet identifier', 'order status')",
        },
        limit: {
          type: "number",
          description: "Maximum number of results to return (default: 20)",
        },
      },
      required: ["query"],
    },
  },
  {
    name: "dd_list_fields_by_api",
    description:
      "List all fields defined in the DataDictionary for a specific API or service context.",
    inputSchema: {
      type: "object",
      properties: {
        context: {
          type: "string",
          description: "API/service name to filter by (e.g. 'Petstore API')",
        },
      },
      required: ["context"],
    },
  },
];

// ─── Server factory ───────────────────────────────────────────────────────────

export function createMcpServer(
  swaggerResult: SwaggerParseResult,
  options: ToolWeaveServerOptions = {}
): Server {
  const server = new Server(
    { name: "toolweave", version: "1.0.0" },
    { capabilities: { tools: {} } }
  );

  const endpointMap = new Map<string, ParsedEndpoint>();
  const tools: Tool[] = [];

  for (const endpoint of swaggerResult.endpoints) {
    endpointMap.set(endpoint.toolName, endpoint);
    tools.push({
      name: endpoint.toolName,
      description: endpoint.description,
      inputSchema: endpoint.inputSchema as Tool["inputSchema"],
    });
  }

  // Optionally include DataDictionary lookup tools
  const ddClient = options.dataDictionaryUrl
    ? new DataDictionaryClient(options.dataDictionaryUrl)
    : null;

  if (ddClient) {
    tools.push(...DD_TOOLS);
  }

  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const toolName = request.params.name;
    const args = (request.params.arguments ?? {}) as Record<string, unknown>;

    // ── DataDictionary proxy tools ──────────────────────────────────────────
    if (ddClient && toolName.startsWith("dd_")) {
      return handleDdTool(ddClient, toolName, args);
    }

    // ── API endpoint tools ──────────────────────────────────────────────────
    const endpoint = endpointMap.get(toolName);
    if (!endpoint) {
      return {
        content: [{ type: "text" as const, text: `Tool not found: ${toolName}` }],
        isError: true,
      };
    }

    try {
      const result = await executeEndpoint(endpoint, args, options.executeOptions ?? {});
      return {
        content: [{ type: "text" as const, text: formatResponse(result) }],
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return {
        content: [
          {
            type: "text" as const,
            text: `Error executing ${endpoint.method} ${endpoint.path}: ${message}`,
          },
        ],
        isError: true,
      };
    }
  });

  return server;
}

// ─── DataDictionary tool handler ─────────────────────────────────────────────

async function handleDdTool(
  client: DataDictionaryClient,
  toolName: string,
  args: Record<string, unknown>
): Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }> {
  try {
    let result: unknown;

    if (toolName === "dd_get_field") {
      result = await client.getDataElement(String(args["field_name"]));
      if (!result) {
        return {
          content: [
            {
              type: "text",
              text: `No DataDictionary entry found for field: "${args["field_name"]}"`,
            },
          ],
        };
      }
    } else if (toolName === "dd_search_fields") {
      result = await client.searchDataElements(
        String(args["query"]),
        args["limit"] !== undefined ? Number(args["limit"]) : 20
      );
    } else if (toolName === "dd_list_fields_by_api") {
      result = await client.getElementsByContext(String(args["context"]));
    } else {
      return {
        content: [{ type: "text", text: `Unknown DataDictionary tool: ${toolName}` }],
        isError: true,
      };
    }

    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      content: [{ type: "text", text: `DataDictionary error: ${message}` }],
      isError: true,
    };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatResponse(result: {
  status: number;
  statusText: string;
  data: unknown;
}): string {
  const lines: string[] = [`HTTP ${result.status} ${result.statusText}`, ""];

  if (result.data !== undefined && result.data !== null && result.data !== "") {
    lines.push(
      typeof result.data === "object"
        ? JSON.stringify(result.data, null, 2)
        : String(result.data)
    );
  }

  return lines.join("\n");
}
