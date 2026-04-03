/**
 * MCP client for the DataDictionary server.
 *
 * The DataDictionary server (https://github.com/rajatarun/DataDictionary) is a
 * FastMCP server deployed as an AWS Lambda function. It stores API field
 * definitions (DataElements) in DynamoDB and uses AWS Bedrock to generate
 * AI-grounded meanings.
 *
 * This client uses the MCP SDK's HTTP client to call the DataDictionary tools:
 *   - propose_data_element  → Bedrock generates a DataElement from a description
 *   - commit_data_element   → Persists the approved proposal to DynamoDB
 *   - get_data_element      → Retrieve by exact field name
 *   - search_data_elements  → Free-text search
 *   - list_data_elements    → Paginated listing with optional context filter
 *   - get_elements_by_context → All fields for a given API/service
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export interface DataElement {
  dataElement: string;
  meaning: string;
  context: string;
  dataType: "string" | "number" | "boolean" | "array" | "object";
  examples: string[];
  constraints: string;
  relatedElements: string[];
  source: { apiName: string; version: string };
  status: "active" | "deprecated";
  createdAt?: string;
  updatedAt?: string;
}

export interface ProposeResult {
  proposal_id: string;
  commit_token?: string;
  proposed_element: DataElement;
  status: "approved" | "blocked";
  message?: string;
}

export interface SearchResult {
  items: DataElement[];
  count: number;
  last_evaluated_key?: string;
}

export class DataDictionaryClient {
  private url: string;

  constructor(dataDictionaryUrl: string) {
    // Normalise — strip trailing slash
    this.url = dataDictionaryUrl.replace(/\/$/, "");
  }

  /**
   * Call a DataDictionary MCP tool with the given arguments.
   * Creates a fresh stateless connection per call (matches server's stateless_http=True).
   */
  private async callTool(toolName: string, args: Record<string, unknown>): Promise<unknown> {
    const transport = new StreamableHTTPClientTransport(new URL(`${this.url}/mcp`));
    const client = new Client({ name: "toolweave", version: "1.0.0" });

    await client.connect(transport);
    try {
      const result = await client.callTool({ name: toolName, arguments: args });
      // Extract text content from MCP response
      const content = result.content as Array<{ type: string; text?: string }>;
      const text = content.find((c) => c.type === "text")?.text ?? "";
      try {
        return JSON.parse(text);
      } catch {
        return text;
      }
    } finally {
      await client.close();
    }
  }

  /**
   * Propose a new data element (uses Bedrock to generate structured definition).
   * Returns a proposal ID and commit token if Observatory approves.
   */
  async proposeDataElement(description: string): Promise<ProposeResult> {
    return this.callTool("propose_data_element", { description }) as Promise<ProposeResult>;
  }

  /**
   * Commit an approved proposal to DynamoDB.
   */
  async commitDataElement(proposalId: string, commitToken: string): Promise<DataElement> {
    return this.callTool("commit_data_element", {
      proposal_id: proposalId,
      commit_token: commitToken,
    }) as Promise<DataElement>;
  }

  /**
   * Retrieve a data element by its exact field name.
   */
  async getDataElement(dataElement: string): Promise<DataElement | null> {
    try {
      return (await this.callTool("get_data_element", { data_element: dataElement })) as DataElement;
    } catch {
      return null;
    }
  }

  /**
   * Free-text search across field names and meanings.
   */
  async searchDataElements(query: string, limit = 20): Promise<SearchResult> {
    return this.callTool("search_data_elements", { query, limit }) as Promise<SearchResult>;
  }

  /**
   * List data elements, optionally filtered by context (API name).
   */
  async listDataElements(context?: string, limit = 50): Promise<SearchResult> {
    return this.callTool("list_data_elements", { context, limit }) as Promise<SearchResult>;
  }

  /**
   * Get all data elements for a given API/service context.
   */
  async getElementsByContext(context: string): Promise<DataElement[]> {
    const result = (await this.callTool("get_elements_by_context", {
      context,
    })) as { items: DataElement[] } | DataElement[];
    return Array.isArray(result) ? result : result.items ?? [];
  }
}

/**
 * Build a natural language description for a field to send to propose_data_element.
 * The DataDictionary's Bedrock prompt works best with rich context.
 */
export function buildFieldDescription(opts: {
  fieldName: string;
  fieldType: string;
  description?: string;
  apiName: string;
  apiVersion: string;
  endpointPath: string;
  endpointMethod: string;
  required: boolean;
  example?: unknown;
  enumValues?: unknown[];
  format?: string;
}): string {
  const parts: string[] = [
    `Field name: "${opts.fieldName}"`,
    `API: "${opts.apiName}" (version ${opts.apiVersion})`,
    `Endpoint: ${opts.endpointMethod} ${opts.endpointPath}`,
    `Data type: ${opts.fieldType}${opts.format ? ` (format: ${opts.format})` : ""}`,
    `Required: ${opts.required ? "yes" : "no"}`,
  ];

  if (opts.description) parts.push(`Description from spec: ${opts.description}`);
  if (opts.example !== undefined) parts.push(`Example value: ${JSON.stringify(opts.example)}`);
  if (opts.enumValues?.length) parts.push(`Allowed values: ${opts.enumValues.join(", ")}`);

  return parts.join(". ");
}
