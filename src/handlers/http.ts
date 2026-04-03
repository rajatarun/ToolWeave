import express, { Request, Response } from "express";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";
import { randomUUID } from "crypto";

/**
 * Start an Express HTTP server exposing the MCP server over Streamable HTTP.
 *
 * Each client session gets its own transport instance (stateful sessions).
 * Sessions are identified by the Mcp-Session-Id header returned on the first
 * POST /mcp request.
 */
export function startHttpServer(
  createServer: () => Server,
  port: number = 3000
): void {
  const app = express();
  app.use(express.json());

  // Map of sessionId → transport
  const sessions = new Map<string, StreamableHTTPServerTransport>();

  app.post("/mcp", async (req: Request, res: Response) => {
    const sessionId = req.headers["mcp-session-id"] as string | undefined;

    let transport: StreamableHTTPServerTransport;

    if (sessionId && sessions.has(sessionId)) {
      transport = sessions.get(sessionId)!;
    } else if (!sessionId && isInitializeRequest(req.body)) {
      // New session initialisation
      const newSessionId = randomUUID();
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => newSessionId,
        onsessioninitialized: (sid) => {
          sessions.set(sid, transport);
        },
      });

      transport.onclose = () => {
        sessions.delete(newSessionId);
      };

      const server = createServer();
      await server.connect(transport);
    } else {
      res.status(400).json({
        error: "Bad Request",
        message: "Missing or unknown Mcp-Session-Id. Send an initialize request first.",
      });
      return;
    }

    await transport.handleRequest(req, res, req.body);
  });

  // SSE endpoint for server-to-client streaming (GET /mcp)
  app.get("/mcp", async (req: Request, res: Response) => {
    const sessionId = req.headers["mcp-session-id"] as string | undefined;

    if (!sessionId || !sessions.has(sessionId)) {
      res.status(400).json({ error: "Unknown or missing Mcp-Session-Id" });
      return;
    }

    const transport = sessions.get(sessionId)!;
    await transport.handleRequest(req, res);
  });

  // Session teardown (DELETE /mcp)
  app.delete("/mcp", async (req: Request, res: Response) => {
    const sessionId = req.headers["mcp-session-id"] as string | undefined;

    if (sessionId && sessions.has(sessionId)) {
      const transport = sessions.get(sessionId)!;
      await transport.handleRequest(req, res);
      sessions.delete(sessionId);
    } else {
      res.status(404).json({ error: "Session not found" });
    }
  });

  // Health check
  app.get("/health", (_req: Request, res: Response) => {
    res.json({ status: "ok", sessions: sessions.size });
  });

  app.listen(port, () => {
    console.log(`ToolWeave MCP server listening on http://localhost:${port}/mcp`);
  });
}
