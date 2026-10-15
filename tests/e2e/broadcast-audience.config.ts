import { defineConfig } from "@playwright/test";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const port = process.env.SUNRISE_BROADCAST_E2E_PORT
  ? Number(process.env.SUNRISE_BROADCAST_E2E_PORT)
  : await new Promise<number>((resolve, reject) => {
      const server = createServer();
      server.on("error", reject);
      server.listen(0, "127.0.0.1", () => {
        const address = server.address();
        if (!address || typeof address === "string")
          return reject(new Error("No local port"));
        server.close((error) =>
          error ? reject(error) : resolve(address.port),
        );
      });
    });
process.env.SUNRISE_BROADCAST_E2E_PORT = String(port);
process.env.PLAYWRIGHT_BROWSERS_PATH = `${root}.artifacts/playwright-browsers`;
export default defineConfig({
  testDir: ".",
  testMatch: "broadcast-audience.spec.ts",
  outputDir: `${root}.artifacts/broadcast-audience`,
  workers: 1,
  retries: 0,
  timeout: 30000,
  expect: { timeout: 10000 },
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: "chromium",
    headless: true,
    locale: "ru-RU",
  },
  webServer: {
    command: "npm.cmd run dev:dashboard",
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    cwd: root,
    env: { WEB_DEV_PORT: String(port), WEB_INSTANCE_ID: "broadcast-audience" },
  },
});
