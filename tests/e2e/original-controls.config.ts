import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("../../", import.meta.url));
export default defineConfig({
  testDir: ".",
  testMatch: "original-controls.browser.ts",
  workers: 1,
  timeout: 30000,
  expect: { timeout: 5000 },
  outputDir: `${root}.artifacts/original-controls-results`,
  use: {
    baseURL: "http://127.0.0.1:3297",
    headless: true,
    launchOptions: { executablePath: process.env.SUNRISE_COMPONENT_CHROMIUM },
  },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 3297 --strictPort",
    url: "http://127.0.0.1:3297/tests/e2e/original-controls.html",
    cwd: root,
    reuseExistingServer: false,
  },
});
