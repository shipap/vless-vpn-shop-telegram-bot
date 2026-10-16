import type { FullConfig } from "@playwright/test";
import { createServer } from "vite";
import { fileURLToPath } from "node:url";

export default async function setup(config: FullConfig) {
  const port = Number(new URL(config.projects[0].use.baseURL!).port);
  process.env.WEB_DEV_PORT = String(port);
  process.env.WEB_INSTANCE_ID = "action-entry-refresh";
  const server = await createServer({
    configFile: fileURLToPath(
      new URL("../../apps/web/dashboard/vite.config.ts", import.meta.url),
    ),
    server: { host: "127.0.0.1", port, strictPort: true },
  });
  await server.listen();
  return () => server.close();
}
