import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
const root = fileURLToPath(new URL("../../", import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH = `${root}.artifacts/playwright-browsers`;
process.env.SUNRISE_E2E_AUTH_RUN_ID ??= randomUUID();
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  timeout: 180000,
  expect: { timeout: 15000 },
  workers: 1,
  fullyParallel: false,
  retries: 0,
  forbidOnly: true,
  outputDir: `${root}.artifacts/playwright-results`,
  reporter: [
    ["list"],
    ["json", { outputFile: `${root}.artifacts/playwright-report.json` }],
  ],
  globalTeardown: "./local_auth_cleanup.ts",
  use: {
    locale: "ru-RU",
    baseURL: "http://127.0.0.1:3200",
    browserName: "chromium",
    headless: true,
    trace: "off",
    screenshot: "off",
  },
  webServer: [
    {
      command: ".venv\\Scripts\\python.exe -m tests.e2e.local_auth_server",
      url: "http://127.0.0.1:8150/__e2e__/status",
      reuseExistingServer: false,
      cwd: root,
      timeout: 30000,
    },
    {
      command: "npm.cmd run preview:dashboard",
      url: "http://127.0.0.1:3201",
      reuseExistingServer: true,
      cwd: root,
    },
    {
      command:
        ".\\node_modules\\.bin\\vite.cmd preview --config tests/e2e/vite-dashboard.config.ts",
      url: "http://127.0.0.1:3211",
      reuseExistingServer: false,
      cwd: root,
    },
    {
      command:
        ".\\node_modules\\.bin\\vite.cmd preview --config tests/e2e/vite-landing.config.ts",
      url: "http://127.0.0.1:3111",
      reuseExistingServer: false,
      cwd: root,
    },
    {
      command: "npm.cmd run preview:landing",
      url: "http://127.0.0.1:3101",
      reuseExistingServer: true,
      cwd: root,
    },
    {
      command: "npm.cmd run dev:dashboard",
      url: "http://127.0.0.1:3200",
      reuseExistingServer: true,
      cwd: root,
    },
    {
      command: "npm.cmd run dev:landing",
      url: "http://127.0.0.1:3100",
      reuseExistingServer: true,
      cwd: root,
    },
    {
      command:
        ".\\node_modules\\.bin\\vite.cmd --config tests/e2e/vite-dashboard.config.ts",
      url: "http://127.0.0.1:3210",
      reuseExistingServer: false,
      cwd: root,
    },
    {
      command:
        ".\\node_modules\\.bin\\vite.cmd --config tests/e2e/vite-landing.config.ts",
      url: "http://127.0.0.1:3110",
      reuseExistingServer: false,
      cwd: root,
    },
  ],
});
