import { defineConfig } from "@playwright/test";
import { createServer } from "node:net";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
async function unusedPort() {
  return new Promise<number>((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string")
        return reject(new Error("No local port"));
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}
const dashboardPort =
  process.env.PUBLIC_QA_DASHBOARD_PORT ?? String(await unusedPort());
const landingPort =
  process.env.PUBLIC_QA_LANDING_PORT ?? String(await unusedPort());
process.env.PUBLIC_QA_DASHBOARD_PORT = dashboardPort;
process.env.PUBLIC_QA_LANDING_PORT = landingPort;

export default defineConfig({
  testDir: ".",
  testMatch: "public-release.browser.ts",
  workers: 1,
  retries: 0,
  timeout: 30000,
  outputDir: `${root}.artifacts/public-browser-results`,
  use: {
    baseURL: `http://127.0.0.1:${dashboardPort}`,
    locale: "ru-RU",
    headless: true,
    launchOptions: { executablePath: process.env.PUBLIC_QA_CHROMIUM },
    viewport: { width: 1365, height: 900 },
  },
  webServer: [
    {
      command:
        "node node_modules/vite/bin/vite.js --config apps/web/dashboard/vite.config.ts",
      url: `http://127.0.0.1:${dashboardPort}`,
      cwd: root,
      env: {
        WEB_DEV_PORT: dashboardPort,
        WEB_INSTANCE_ID: "public-release-dashboard",
      },
      reuseExistingServer: false,
    },
    {
      command:
        "node node_modules/vite/bin/vite.js --config apps/web/landing/vite.config.ts",
      url: `http://127.0.0.1:${landingPort}`,
      cwd: root,
      env: {
        WEB_DEV_PORT: landingPort,
        WEB_INSTANCE_ID: "public-release-landing",
      },
      reuseExistingServer: false,
    },
  ],
});
