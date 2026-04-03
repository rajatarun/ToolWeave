/**
 * AWS Lambda entry point.
 *
 * Set the SWAGGER_SPEC environment variable to the path (relative to the
 * Lambda package root) or a public URL of your Swagger/OpenAPI spec.
 *
 * Example environment variables:
 *   SWAGGER_SPEC=./swagger.yaml
 *   BASE_URL=https://api.example.com
 *   DEFAULT_HEADERS=Authorization:Bearer ${TOKEN},X-Api-Key:secret
 *   REQUEST_TIMEOUT_MS=10000
 *   DATA_DICTIONARY_URL=https://abc.lambda-url.us-east-1.on.aws  (optional)
 */

import { APIGatewayProxyEventV2, Context } from "aws-lambda";
import { parseSwagger } from "./swagger/parser.js";
import { createMcpServer } from "./mcp/server.js";
// createMcpServer returns a Server instance, which ServerFactory accepts
import { createLambdaHandler } from "./handlers/lambda.js";
import { ExecuteOptions } from "./tools/executor.js";

type LambdaHandlerFn = (
  event: APIGatewayProxyEventV2,
  context: Context
) => Promise<unknown>;

let cachedHandler: LambdaHandlerFn | null = null;
let initPromise: Promise<LambdaHandlerFn> | null = null;

async function initHandler(): Promise<LambdaHandlerFn> {
  const spec = process.env["SWAGGER_SPEC"];
  if (!spec) {
    throw new Error("SWAGGER_SPEC environment variable is required");
  }

  const executeOptions: ExecuteOptions = {
    baseUrlOverride: process.env["BASE_URL"],
    timeoutMs: process.env["REQUEST_TIMEOUT_MS"]
      ? parseInt(process.env["REQUEST_TIMEOUT_MS"], 10)
      : 30000,
    defaultHeaders: parseEnvHeaders(process.env["DEFAULT_HEADERS"]),
  };

  console.log(`Parsing spec: ${spec}`);
  const swaggerResult = await parseSwagger(spec);
  console.log(
    `Loaded "${swaggerResult.title}" — ${swaggerResult.endpoints.length} endpoints`
  );

  const dataDictionaryUrl = process.env["DATA_DICTIONARY_URL"] || undefined;

  return createLambdaHandler(() =>
    createMcpServer(swaggerResult, { executeOptions, dataDictionaryUrl })
  );
}

export const handler = async (
  event: APIGatewayProxyEventV2,
  context: Context
): Promise<unknown> => {
  // Lazy init — parse spec once per warm Lambda instance
  if (!cachedHandler) {
    if (!initPromise) {
      initPromise = initHandler();
    }
    cachedHandler = await initPromise;
  }

  return cachedHandler(event, context);
};

function parseEnvHeaders(raw: string | undefined): Record<string, string> {
  const headers: Record<string, string> = {};
  if (!raw) return headers;

  for (const part of raw.split(",")) {
    const idx = part.indexOf(":");
    if (idx === -1) continue;
    headers[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }

  return headers;
}
