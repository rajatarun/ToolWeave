/**
 * AWS Lambda handler for API Gateway HTTP API (payload format v2).
 *
 * Architecture:
 *   API Gateway HTTP API  →  Lambda  →  MCP Streamable HTTP transport
 *
 * Because Lambda is stateless, session state is stored in memory per
 * Lambda instance. For production workloads with multiple instances you
 * should either:
 *   a) Use sticky routing (API GW route key or custom header) so a session
 *      always hits the same Lambda instance, OR
 *   b) Store session state externally (DynamoDB / ElastiCache) and
 *      reconstruct the transport on every invocation.
 *
 * The handler supports:
 *   POST  /mcp  – MCP client → server requests + initialize
 *   GET   /mcp  – SSE stream (server → client notifications)
 *   DELETE /mcp – session teardown
 */

import {
  APIGatewayProxyEventV2,
  APIGatewayProxyStructuredResultV2,
  Context,
} from "aws-lambda";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { IncomingMessage, ServerResponse } from "http";
import { randomUUID } from "crypto";
import { Socket } from "net";

// Warm-instance session cache
const sessions = new Map<string, StreamableHTTPServerTransport>();

export type ServerFactory = () => Server;

export function createLambdaHandler(createServer: ServerFactory) {
  return async function handler(
    event: APIGatewayProxyEventV2,
    _context: Context
  ): Promise<APIGatewayProxyStructuredResultV2> {
    const method = event.requestContext.http.method.toUpperCase();
    const sessionId = event.headers?.["mcp-session-id"];

    if (method === "POST") {
      return handlePost(event, createServer, sessionId);
    } else if (method === "GET") {
      return handleGet(event, sessionId);
    } else if (method === "DELETE") {
      return handleDelete(sessionId);
    }

    return { statusCode: 405, body: "Method Not Allowed" };
  };
}

async function handlePost(
  event: APIGatewayProxyEventV2,
  createServer: ServerFactory,
  sessionId: string | undefined
): Promise<APIGatewayProxyStructuredResultV2> {
  let body: unknown;
  try {
    const rawBody = event.isBase64Encoded
      ? Buffer.from(event.body ?? "", "base64").toString("utf-8")
      : event.body ?? "";
    body = JSON.parse(rawBody);
  } catch {
    return { statusCode: 400, body: "Invalid JSON body" };
  }

  let transport: StreamableHTTPServerTransport;

  if (sessionId && sessions.has(sessionId)) {
    transport = sessions.get(sessionId)!;
  } else if (!sessionId && isInitializeRequest(body)) {
    const newSessionId = randomUUID();
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => newSessionId,
      onsessioninitialized: (sid) => {
        sessions.set(sid, transport);
      },
    });
    transport.onclose = () => sessions.delete(newSessionId);

    const server = createServer();
    await server.connect(transport);
  } else {
    return {
      statusCode: 400,
      body: JSON.stringify({
        error: "Bad Request",
        message: "Send an initialize request without Mcp-Session-Id to start a session.",
      }),
    };
  }

  return adaptTransportCall(transport, event, body);
}

async function handleGet(
  event: APIGatewayProxyEventV2,
  sessionId: string | undefined
): Promise<APIGatewayProxyStructuredResultV2> {
  if (!sessionId || !sessions.has(sessionId)) {
    return { statusCode: 400, body: "Unknown or missing Mcp-Session-Id" };
  }

  const transport = sessions.get(sessionId)!;
  return adaptTransportCall(transport, event, undefined);
}

async function handleDelete(
  sessionId: string | undefined
): Promise<APIGatewayProxyStructuredResultV2> {
  if (sessionId && sessions.has(sessionId)) {
    sessions.delete(sessionId);
  }
  return { statusCode: 200, body: "" };
}

/**
 * Bridge between API Gateway event and the Node.js IncomingMessage /
 * ServerResponse interface expected by StreamableHTTPServerTransport.
 */
async function adaptTransportCall(
  transport: StreamableHTTPServerTransport,
  event: APIGatewayProxyEventV2,
  body: unknown
): Promise<APIGatewayProxyStructuredResultV2> {
  return new Promise((resolve) => {
    const socket = new Socket();
    const req = new IncomingMessage(socket);

    req.method = event.requestContext.http.method.toUpperCase();
    req.url = event.rawPath + (event.rawQueryString ? `?${event.rawQueryString}` : "");

    // Merge headers
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(event.headers ?? {})) {
      if (v !== undefined) headers[k.toLowerCase()] = v;
    }
    req.headers = headers;

    const responseChunks: Buffer[] = [];
    const responseHeaders: Record<string, string> = {};
    let statusCode = 200;

    const res = new ServerResponse(req);

    (res as unknown as { writeHead: (code: number, hdrs?: Record<string, string | string[]>) => ServerResponse }).writeHead = function (code: number, hdrs?: Record<string, string | string[]>) {
      statusCode = code;
      if (hdrs) {
        for (const [k, v] of Object.entries(hdrs)) {
          responseHeaders[k] = Array.isArray(v) ? v.join(", ") : v;
        }
      }
      return res;
    };

    (res as NodeJS.WritableStream).write = function (chunk: unknown) {
      if (Buffer.isBuffer(chunk)) {
        responseChunks.push(chunk);
      } else if (typeof chunk === "string") {
        responseChunks.push(Buffer.from(chunk));
      }
      return true;
    };

    res.end = function (chunk?: unknown) {
      if (chunk) {
        if (Buffer.isBuffer(chunk)) {
          responseChunks.push(chunk);
        } else if (typeof chunk === "string") {
          responseChunks.push(Buffer.from(chunk));
        }
      }

      const bodyStr = Buffer.concat(responseChunks).toString("utf-8");
      resolve({
        statusCode,
        headers: responseHeaders,
        body: bodyStr,
      });
      return res;
    };

    // Inject body if present
    if (body !== undefined) {
      const bodyStr = JSON.stringify(body);
      req.headers["content-length"] = String(Buffer.byteLength(bodyStr));
      req.push(bodyStr);
      req.push(null);
    } else {
      req.push(null);
    }

    transport.handleRequest(req as unknown as Parameters<typeof transport.handleRequest>[0], res as unknown as Parameters<typeof transport.handleRequest>[1], body).catch((err: unknown) => {
      resolve({
        statusCode: 500,
        body: `Internal error: ${err instanceof Error ? err.message : String(err)}`,
      });
    });
  });
}
