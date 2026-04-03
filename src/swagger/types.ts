export interface ParsedEndpoint {
  toolName: string;
  method: string;
  path: string;
  baseUrl: string;
  description: string;
  inputSchema: Record<string, unknown>;
  parameters: ParsedParameter[];
  requestBody?: ParsedRequestBody;
  security?: SecurityRequirement[];
}

export interface ParsedParameter {
  name: string;
  in: "query" | "header" | "path" | "cookie";
  required: boolean;
  description?: string;
  schema: Record<string, unknown>;
}

export interface ParsedRequestBody {
  required: boolean;
  description?: string;
  contentType: string;
  schema: Record<string, unknown>;
}

export interface SecurityRequirement {
  type: string;
  name: string;
  in?: string;
}

export interface SwaggerParseResult {
  title: string;
  version: string;
  baseUrl: string;
  endpoints: ParsedEndpoint[];
}
