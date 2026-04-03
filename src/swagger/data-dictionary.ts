import SwaggerParser from "@apidevtools/swagger-parser";
import { OpenAPIV2, OpenAPIV3, OpenAPIV3_1 } from "openapi-types";

export interface FieldEntry {
  name: string;
  path: string;        // dot-notation path, e.g. "order.items[].price"
  type: string;
  format?: string;
  description?: string;
  required: boolean;
  nullable?: boolean;
  enum?: unknown[];
  example?: unknown;
  default?: unknown;
}

export interface SchemaEntry {
  name: string;
  description?: string;
  fields: FieldEntry[];
}

export interface EndpointSchema {
  toolName: string;
  method: string;
  path: string;
  parameters: FieldEntry[];
  requestBody?: {
    required: boolean;
    contentType: string;
    fields: FieldEntry[];
  };
  responses: {
    statusCode: string;
    description?: string;
    fields: FieldEntry[];
  }[];
}

export interface DataDictionary {
  title: string;
  version: string;
  /** Named schemas from components/schemas (OpenAPI 3) or definitions (Swagger 2) */
  schemas: SchemaEntry[];
  /** Per-endpoint request/response field breakdown */
  endpoints: EndpointSchema[];
}

type AnySchema = Record<string, unknown>;

// ─── Public entry point ───────────────────────────────────────────────────────

export async function buildDataDictionary(source: string): Promise<DataDictionary> {
  const api = (await SwaggerParser.dereference(source)) as
    | OpenAPIV2.Document
    | OpenAPIV3.Document
    | OpenAPIV3_1.Document;

  const title = api.info?.title ?? "Unknown API";
  const version = api.info?.version ?? "1.0.0";

  const schemas = extractNamedSchemas(api);
  const endpoints = extractEndpointSchemas(api);

  return { title, version, schemas, endpoints };
}

// ─── Named schemas ────────────────────────────────────────────────────────────

function extractNamedSchemas(
  api: OpenAPIV2.Document | OpenAPIV3.Document | OpenAPIV3_1.Document
): SchemaEntry[] {
  const results: SchemaEntry[] = [];

  if ("swagger" in api) {
    // Swagger 2.x definitions
    const defs = (api as OpenAPIV2.Document).definitions ?? {};
    for (const [name, schema] of Object.entries(defs)) {
      results.push(schemaEntryFrom(name, schema as AnySchema));
    }
  } else {
    // OpenAPI 3.x components/schemas
    const v3 = api as OpenAPIV3.Document;
    const schemaMap = (v3.components as Record<string, unknown>)?.["schemas"] as
      | Record<string, AnySchema>
      | undefined;

    if (schemaMap) {
      for (const [name, schema] of Object.entries(schemaMap)) {
        results.push(schemaEntryFrom(name, schema));
      }
    }
  }

  return results.sort((a, b) => a.name.localeCompare(b.name));
}

function schemaEntryFrom(name: string, schema: AnySchema): SchemaEntry {
  const fields: FieldEntry[] = [];
  flattenSchema(schema, name, "", true, fields);
  return {
    name,
    description: schema["description"] as string | undefined,
    fields,
  };
}

// ─── Endpoint schemas ─────────────────────────────────────────────────────────

function extractEndpointSchemas(
  api: OpenAPIV2.Document | OpenAPIV3.Document | OpenAPIV3_1.Document
): EndpointSchema[] {
  const results: EndpointSchema[] = [];

  if (!api.paths) return results;

  const isV2 = "swagger" in api;
  const methods = ["get", "post", "put", "patch", "delete", "head", "options"] as const;

  for (const [path, pathItem] of Object.entries(api.paths)) {
    if (!pathItem) continue;

    for (const method of methods) {
      const op = (pathItem as Record<string, unknown>)[method] as
        | OpenAPIV2.OperationObject
        | OpenAPIV3.OperationObject
        | undefined;
      if (!op) continue;

      const toolName = buildToolName(method, path, op.operationId);
      const parameters = extractParamFields(op, pathItem as Record<string, unknown>);

      const requestBody = isV2
        ? extractV2RequestFields(op as OpenAPIV2.OperationObject)
        : extractV3RequestFields(op as OpenAPIV3.OperationObject);

      const responses = isV2
        ? extractV2ResponseFields(op as OpenAPIV2.OperationObject)
        : extractV3ResponseFields(op as OpenAPIV3.OperationObject);

      results.push({ toolName, method: method.toUpperCase(), path, parameters, requestBody, responses });
    }
  }

  return results;
}

function extractParamFields(
  op: OpenAPIV2.OperationObject | OpenAPIV3.OperationObject,
  pathItem: Record<string, unknown>
): FieldEntry[] {
  const pathParams = (pathItem["parameters"] as OpenAPIV3.ParameterObject[] | undefined) ?? [];
  const opParams = (op.parameters as OpenAPIV3.ParameterObject[] | undefined) ?? [];

  const seen = new Map<string, FieldEntry>();

  for (const raw of [...pathParams, ...opParams]) {
    if ("$ref" in raw) continue;
    const p = raw as OpenAPIV3.ParameterObject;
    const schema = (p.schema ?? { type: "string" }) as AnySchema;

    seen.set(`${p.name}:${p.in}`, {
      name: p.name,
      path: p.name,
      type: typeString(schema),
      format: schema["format"] as string | undefined,
      description: p.description ?? (schema["description"] as string | undefined),
      required: p.required ?? p.in === "path",
      nullable: schema["nullable"] as boolean | undefined,
      enum: schema["enum"] as unknown[] | undefined,
      example: p.example ?? schema["example"],
      default: schema["default"],
    });
  }

  return Array.from(seen.values());
}

function extractV2RequestFields(
  op: OpenAPIV2.OperationObject
): EndpointSchema["requestBody"] | undefined {
  const bodyParam = (op.parameters as OpenAPIV2.InBodyParameterObject[] | undefined)?.find(
    (p) => p.in === "body"
  );
  if (!bodyParam?.schema) return undefined;

  const fields: FieldEntry[] = [];
  flattenSchema(bodyParam.schema as AnySchema, "body", "", bodyParam.required ?? false, fields);

  const consumes = (op as { consumes?: string[] }).consumes?.[0] ?? "application/json";
  return { required: bodyParam.required ?? false, contentType: consumes, fields };
}

function extractV3RequestFields(
  op: OpenAPIV3.OperationObject
): EndpointSchema["requestBody"] | undefined {
  if (!op.requestBody) return undefined;
  const rb = op.requestBody as OpenAPIV3.RequestBodyObject;
  const contentType = Object.keys(rb.content ?? {})[0] ?? "application/json";
  const mediaSchema = (rb.content?.[contentType]?.schema ?? {}) as AnySchema;

  const fields: FieldEntry[] = [];
  flattenSchema(mediaSchema, "body", "", rb.required ?? false, fields);

  return { required: rb.required ?? false, contentType, fields };
}

function extractV2ResponseFields(
  op: OpenAPIV2.OperationObject
): EndpointSchema["responses"] {
  const results: EndpointSchema["responses"] = [];
  const responses = (op.responses as Record<string, OpenAPIV2.Response>) ?? {};

  for (const [statusCode, resp] of Object.entries(responses)) {
    if (!resp) continue;
    const r = resp as OpenAPIV2.Response & { schema?: AnySchema; description?: string };
    const fields: FieldEntry[] = [];
    if (r.schema) flattenSchema(r.schema as AnySchema, "response", "", true, fields);
    results.push({ statusCode, description: r.description, fields });
  }

  return results;
}

function extractV3ResponseFields(
  op: OpenAPIV3.OperationObject
): EndpointSchema["responses"] {
  const results: EndpointSchema["responses"] = [];
  const responses = (op.responses as Record<string, OpenAPIV3.ResponseObject>) ?? {};

  for (const [statusCode, resp] of Object.entries(responses)) {
    if (!resp) continue;
    const r = resp as OpenAPIV3.ResponseObject;
    const contentType = Object.keys(r.content ?? {})[0];
    const schema = contentType ? ((r.content?.[contentType]?.schema ?? {}) as AnySchema) : undefined;

    const fields: FieldEntry[] = [];
    if (schema) flattenSchema(schema, "response", "", true, fields);
    results.push({ statusCode, description: r.description, fields });
  }

  return results;
}

// ─── Schema flattening ────────────────────────────────────────────────────────

function flattenSchema(
  schema: AnySchema,
  name: string,
  pathPrefix: string,
  required: boolean,
  out: FieldEntry[],
  depth = 0
): void {
  if (depth > 6) return; // guard against deep recursion on circular schemas

  const currentPath = pathPrefix ? `${pathPrefix}.${name}` : name;

  // Handle allOf / anyOf / oneOf by merging properties
  const merged = mergeComposedSchema(schema);

  const type = merged["type"] as string | undefined;

  if (type === "object" || merged["properties"]) {
    const props = (merged["properties"] as Record<string, AnySchema>) ?? {};
    const requiredFields = (merged["required"] as string[]) ?? [];

    // Emit the object itself if it's not the root
    if (depth > 0) {
      out.push({
        name,
        path: currentPath,
        type: "object",
        description: merged["description"] as string | undefined,
        required,
        example: merged["example"],
      });
    }

    for (const [propName, propSchema] of Object.entries(props)) {
      flattenSchema(
        propSchema,
        propName,
        currentPath,
        requiredFields.includes(propName),
        out,
        depth + 1
      );
    }
    return;
  }

  if (type === "array") {
    const items = (merged["items"] as AnySchema) ?? {};
    out.push({
      name,
      path: currentPath,
      type: "array",
      description: merged["description"] as string | undefined,
      required,
      example: merged["example"],
    });
    flattenSchema(items, `${name}[]`, pathPrefix, false, out, depth + 1);
    return;
  }

  // Primitive or unknown
  out.push({
    name,
    path: currentPath,
    type: typeString(merged),
    format: merged["format"] as string | undefined,
    description: merged["description"] as string | undefined,
    required,
    nullable: merged["nullable"] as boolean | undefined,
    enum: merged["enum"] as unknown[] | undefined,
    example: merged["example"],
    default: merged["default"],
  });
}

function mergeComposedSchema(schema: AnySchema): AnySchema {
  const composed = (schema["allOf"] ?? schema["anyOf"] ?? schema["oneOf"]) as
    | AnySchema[]
    | undefined;

  if (!composed || composed.length === 0) return schema;

  // Merge all sub-schemas' properties into a single object schema
  const merged: AnySchema = { type: "object", properties: {}, required: [] };

  for (const sub of composed) {
    const props = sub["properties"] as Record<string, AnySchema> | undefined;
    if (props) Object.assign(merged["properties"] as object, props);

    const req = sub["required"] as string[] | undefined;
    if (req) (merged["required"] as string[]).push(...req);

    if (sub["description"]) merged["description"] = sub["description"];
  }

  return merged;
}

function typeString(schema: AnySchema): string {
  const type = schema["type"];
  if (!type) {
    if (schema["properties"] || schema["allOf"]) return "object";
    if (schema["items"]) return "array";
    return "any";
  }
  const format = schema["format"] as string | undefined;
  return format ? `${type}(${format})` : String(type);
}

function buildToolName(method: string, path: string, operationId?: string): string {
  if (operationId) return operationId.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 64);
  const sanitizedPath = path
    .replace(/\//g, "_")
    .replace(/[{}]/g, "")
    .replace(/[^a-zA-Z0-9_]/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  return `${method}_${sanitizedPath}`.slice(0, 64);
}

// ─── Rendering helpers ────────────────────────────────────────────────────────

export function renderSchemaEntry(entry: SchemaEntry): string {
  const lines: string[] = [`## ${entry.name}`, ""];
  if (entry.description) lines.push(entry.description, "");

  if (entry.fields.length === 0) {
    lines.push("_No fields_");
  } else {
    lines.push("| Field | Type | Required | Description | Example |");
    lines.push("|---|---|---|---|---|");
    for (const f of entry.fields) {
      const req = f.required ? "Yes" : "No";
      const desc = [f.description, f.enum ? `Enum: ${f.enum.join(", ")}` : undefined]
        .filter(Boolean)
        .join(". ") || "-";
      const ex = f.example !== undefined ? String(f.example) : "-";
      lines.push(`| \`${f.path}\` | ${f.type} | ${req} | ${desc} | ${ex} |`);
    }
  }

  return lines.join("\n");
}

export function renderEndpointSchema(ep: EndpointSchema): string {
  const lines: string[] = [`## ${ep.method} ${ep.path}`, `**Tool:** \`${ep.toolName}\``, ""];

  if (ep.parameters.length > 0) {
    lines.push("### Parameters", "");
    lines.push("| Name | Type | In | Required | Description |");
    lines.push("|---|---|---|---|---|");
    for (const f of ep.parameters) {
      const desc = f.description ?? "-";
      lines.push(`| \`${f.name}\` | ${f.type} | param | ${f.required ? "Yes" : "No"} | ${desc} |`);
    }
    lines.push("");
  }

  if (ep.requestBody) {
    lines.push(`### Request Body (${ep.requestBody.contentType})`, "");
    if (ep.requestBody.fields.length > 0) {
      lines.push("| Field | Type | Required | Description |");
      lines.push("|---|---|---|---|");
      for (const f of ep.requestBody.fields) {
        lines.push(`| \`${f.path}\` | ${f.type} | ${f.required ? "Yes" : "No"} | ${f.description ?? "-"} |`);
      }
    }
    lines.push("");
  }

  for (const resp of ep.responses) {
    lines.push(`### Response ${resp.statusCode}${resp.description ? ` — ${resp.description}` : ""}`, "");
    if (resp.fields.length > 0) {
      lines.push("| Field | Type | Description |");
      lines.push("|---|---|---|");
      for (const f of resp.fields) {
        lines.push(`| \`${f.path}\` | ${f.type} | ${f.description ?? "-"} |`);
      }
    }
    lines.push("");
  }

  return lines.join("\n");
}
