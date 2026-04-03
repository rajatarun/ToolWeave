import axios, { AxiosRequestConfig, AxiosResponse } from "axios";
import { ParsedEndpoint } from "../swagger/types.js";

export interface ExecuteOptions {
  baseUrlOverride?: string;
  defaultHeaders?: Record<string, string>;
  timeoutMs?: number;
}

export interface ExecuteResult {
  status: number;
  statusText: string;
  headers: Record<string, string>;
  data: unknown;
}

export async function executeEndpoint(
  endpoint: ParsedEndpoint,
  toolInput: Record<string, unknown>,
  options: ExecuteOptions = {}
): Promise<ExecuteResult> {
  const baseUrl = options.baseUrlOverride ?? endpoint.baseUrl;
  const url = buildUrl(baseUrl, endpoint.path, toolInput, endpoint.parameters);

  const queryParams: Record<string, unknown> = {};
  const headers: Record<string, string> = { ...options.defaultHeaders };

  for (const param of endpoint.parameters) {
    const value = toolInput[param.name];
    if (value === undefined) continue;

    if (param.in === "query") {
      queryParams[param.name] = value;
    } else if (param.in === "header") {
      headers[param.name] = String(value);
    }
  }

  const requestConfig: AxiosRequestConfig = {
    method: endpoint.method,
    url,
    params: Object.keys(queryParams).length > 0 ? queryParams : undefined,
    headers,
    timeout: options.timeoutMs ?? 30000,
    validateStatus: () => true, // Don't throw on non-2xx
  };

  if (endpoint.requestBody && toolInput["body"] !== undefined) {
    requestConfig.data = toolInput["body"];
    if (!headers["Content-Type"]) {
      headers["Content-Type"] = endpoint.requestBody.contentType;
    }
  }

  const response: AxiosResponse = await axios(requestConfig);

  const responseHeaders: Record<string, string> = {};
  for (const [key, value] of Object.entries(response.headers)) {
    if (typeof value === "string") responseHeaders[key] = value;
  }

  return {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
    data: response.data,
  };
}

function buildUrl(
  baseUrl: string,
  path: string,
  toolInput: Record<string, unknown>,
  parameters: ParsedEndpoint["parameters"]
): string {
  let resolvedPath = path;

  for (const param of parameters) {
    if (param.in === "path" && toolInput[param.name] !== undefined) {
      resolvedPath = resolvedPath.replace(
        `{${param.name}}`,
        encodeURIComponent(String(toolInput[param.name]))
      );
    }
  }

  const base = baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
  return `${base}${resolvedPath}`;
}
