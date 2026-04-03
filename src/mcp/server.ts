import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { ParsedEndpoint, SwaggerParseResult } from "../swagger/types.js";
import { executeEndpoint, ExecuteOptions } from "../tools/executor.js";

export interface ToolWeaveServerOptions {
  executeOptions?: ExecuteOptions;
}

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

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools,
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const toolName = request.params.name;
    const endpoint = endpointMap.get(toolName);

    if (!endpoint) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Tool not found: ${toolName}`,
          },
        ],
        isError: true,
      };
    }

    const toolInput = (request.params.arguments ?? {}) as Record<string, unknown>;
    const executeOptions = options.executeOptions ?? {};

    try {
      const result = await executeEndpoint(endpoint, toolInput, executeOptions);
      return {
        content: [
          {
            type: "text" as const,
            text: formatResponse(result),
          },
        ],
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

function formatResponse(result: {
  status: number;
  statusText: string;
  data: unknown;
}): string {
  const lines: string[] = [`HTTP ${result.status} ${result.statusText}`, ""];

  if (result.data !== undefined && result.data !== null && result.data !== "") {
    if (typeof result.data === "object") {
      lines.push(JSON.stringify(result.data, null, 2));
    } else {
      lines.push(String(result.data));
    }
  }

  return lines.join("\n");
}
