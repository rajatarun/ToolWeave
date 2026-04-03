import SwaggerParser from "@apidevtools/swagger-parser";
import { OpenAPI, OpenAPIV2, OpenAPIV3, OpenAPIV3_1 } from "openapi-types";
import {
  ParsedEndpoint,
  ParsedParameter,
  ParsedRequestBody,
  SwaggerParseResult,
} from "./types.js";

type OpenAPIDocument = OpenAPIV2.Document | OpenAPIV3.Document | OpenAPIV3_1.Document;

export async function parseSwagger(source: string): Promise<SwaggerParseResult> {
  const api = await SwaggerParser.dereference(source) as OpenAPIDocument;

  const isV2 = "swagger" in api;
  const isV3 = "openapi" in api;

  const title = api.info?.title ?? "Unknown API";
  const version = api.info?.version ?? "1.0.0";
  const baseUrl = resolveBaseUrl(api, source);

  const endpoints: ParsedEndpoint[] = [];

  if (!api.paths) return { title, version, baseUrl, endpoints };

  for (const [path, pathItem] of Object.entries(api.paths)) {
    if (!pathItem) continue;

    const methods = ["get", "post", "put", "patch", "delete", "head", "options"] as const;

    for (const method of methods) {
      const operation = (pathItem as Record<string, unknown>)[method] as
        | OpenAPIV2.OperationObject
        | OpenAPIV3.OperationObject
        | undefined;

      if (!operation) continue;

      const toolName = buildToolName(method, path, operation.operationId);
      const description = buildDescription(operation);
      const parameters = extractParameters(operation, pathItem as Record<string, unknown>);
      const requestBody = isV2
        ? extractV2RequestBody(operation as OpenAPIV2.OperationObject)
        : extractV3RequestBody(operation as OpenAPIV3.OperationObject);
      const inputSchema = buildInputSchema(parameters, requestBody);

      endpoints.push({
        toolName,
        method: method.toUpperCase(),
        path,
        baseUrl,
        description,
        inputSchema,
        parameters,
        requestBody,
      });
    }
  }

  return { title, version, baseUrl, endpoints };
}

function resolveBaseUrl(api: OpenAPIDocument, source: string): string {
  if ("swagger" in api) {
    const v2 = api as OpenAPIV2.Document;
    const scheme = v2.schemes?.[0] ?? "https";
    const host = v2.host ?? "localhost";
    const basePath = v2.basePath ?? "/";
    return `${scheme}://${host}${basePath}`;
  }

  const v3 = api as OpenAPIV3.Document;
  if (v3.servers?.[0]?.url) {
    return v3.servers[0].url;
  }

  // Derive from source URL if it's a remote spec
  if (source.startsWith("http")) {
    const url = new URL(source);
    return `${url.protocol}//${url.host}`;
  }

  return "http://localhost";
}

function buildToolName(method: string, path: string, operationId?: string): string {
  if (operationId) {
    return operationId.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
  }

  const sanitizedPath = path
    .replace(/\//g, "_")
    .replace(/[{}]/g, "")
    .replace(/[^a-zA-Z0-9_]/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");

  return `${method}_${sanitizedPath}`.slice(0, 64);
}

function buildDescription(
  operation: OpenAPIV2.OperationObject | OpenAPIV3.OperationObject
): string {
  const parts: string[] = [];

  if (operation.summary) parts.push(operation.summary);
  if (operation.description && operation.description !== operation.summary) {
    parts.push(operation.description);
  }
  if (operation.operationId) parts.push(`Operation: ${operation.operationId}`);

  return parts.join("\n") || "No description available";
}

function extractParameters(
  operation: OpenAPIV2.OperationObject | OpenAPIV3.OperationObject,
  pathItem: Record<string, unknown>
): ParsedParameter[] {
  const pathParams = (pathItem["parameters"] as OpenAPIV3.ParameterObject[] | undefined) ?? [];
  const opParams = (operation.parameters as OpenAPIV3.ParameterObject[] | undefined) ?? [];

  // Operation-level params override path-level params with the same name+in
  const paramMap = new Map<string, ParsedParameter>();

  for (const param of [...pathParams, ...opParams]) {
    if ("$ref" in param) continue; // already dereferenced, skip $ref leftovers

    const p = param as OpenAPIV3.ParameterObject;
    const schema = normalizeSchema(p.schema ?? { type: "string" });
    const key = `${p.name}:${p.in}`;

    paramMap.set(key, {
      name: p.name,
      in: p.in as ParsedParameter["in"],
      required: p.required ?? p.in === "path",
      description: p.description,
      schema,
    });
  }

  return Array.from(paramMap.values());
}

function extractV2RequestBody(
  operation: OpenAPIV2.OperationObject
): ParsedRequestBody | undefined {
  const bodyParam = (operation.parameters as OpenAPIV2.InBodyParameterObject[] | undefined)?.find(
    (p) => p.in === "body"
  );

  if (!bodyParam) return undefined;

  const consumes =
    (operation as { consumes?: string[] }).consumes?.[0] ?? "application/json";

  return {
    required: bodyParam.required ?? false,
    description: bodyParam.description,
    contentType: consumes,
    schema: normalizeSchema(bodyParam.schema ?? {}),
  };
}

function extractV3RequestBody(
  operation: OpenAPIV3.OperationObject
): ParsedRequestBody | undefined {
  if (!operation.requestBody) return undefined;

  const rb = operation.requestBody as OpenAPIV3.RequestBodyObject;
  const contentType =
    Object.keys(rb.content ?? {})[0] ?? "application/json";
  const mediaType = rb.content?.[contentType];

  return {
    required: rb.required ?? false,
    description: rb.description,
    contentType,
    schema: normalizeSchema(mediaType?.schema ?? {}),
  };
}

function buildInputSchema(
  parameters: ParsedParameter[],
  requestBody?: ParsedRequestBody
): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];

  for (const param of parameters) {
    properties[param.name] = {
      ...param.schema,
      description: param.description ?? (param.schema as Record<string, unknown>)?.description,
    };
    if (param.required) required.push(param.name);
  }

  if (requestBody) {
    properties["body"] = {
      ...requestBody.schema,
      description: requestBody.description ?? "Request body",
    };
    if (requestBody.required) required.push("body");
  }

  return {
    type: "object",
    properties,
    required: required.length > 0 ? required : undefined,
  };
}

function normalizeSchema(schema: unknown): Record<string, unknown> {
  if (!schema || typeof schema !== "object") return { type: "string" };
  return schema as Record<string, unknown>;
}
