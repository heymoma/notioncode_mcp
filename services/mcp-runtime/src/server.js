#!/usr/bin/env node

import { pathToFileURL } from "node:url";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createMcpExpressApp } from "@modelcontextprotocol/sdk/server/express.js";

import { loadConfig } from "./config.js";
import { createMcpServer } from "./tools.js";

const STARTED_AT = Date.now();

export function createApp(config) {
  const app = createMcpExpressApp();

  // Unauthenticated probe for supervisors and container health checks. It
  // deliberately reveals nothing about the secret endpoint.
  app.get("/healthz", (_request, response) => {
    response.status(200).json({
      ok: true,
      service: "notion-code-runtime",
      uptime_seconds: Math.round((Date.now() - STARTED_AT) / 1000),
    });
  });

  app.use((request, response, next) => {
    if (
      request.path === config.endpoint ||
      request.path === `${config.endpoint}/`
    ) {
      return next();
    }
    return response.status(404).end();
  });

  app.post(config.endpoint, async (request, response) => {
    const mcp = createMcpServer(config);
    try {
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined,
      });
      await mcp.connect(transport);
      await transport.handleRequest(request, response, request.body);
      response.on("close", () => {
        transport.close();
        mcp.close();
      });
    } catch (error) {
      console.error("MCP request error:", error);
      if (!response.headersSent) {
        response.status(500).json({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        });
      }
    }
  });

  app.get(config.endpoint, (_request, response) => response.status(405).end());
  app.delete(config.endpoint, (_request, response) => response.status(405).end());
  return app;
}

export function start(config = loadConfig()) {
  const server = createApp(config).listen(config.port, config.host, () => {
    console.error(
      `notion-code-runtime listening on ${config.host}:${config.port} ` +
      `with CODE_ROOT=${config.root}`,
    );
  });
  // Without this, a restart leaves in-flight tool calls to be killed by the
  // supervisor instead of finishing.
  const shutdown = (signal) => {
    console.error(`notion-code-runtime received ${signal}; closing listener`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 10_000).unref();
  };
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("SIGINT", () => shutdown("SIGINT"));
  return server;
}

const invokedDirectly = Boolean(process.argv[1]) &&
  import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  try {
    start();
  } catch (error) {
    console.error(`Configuration error: ${error.message}`);
    process.exit(2);
  }
}
