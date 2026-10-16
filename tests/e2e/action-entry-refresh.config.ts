import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
import { fileURLToPath } from "node:url";
export default defineConfig({
  ...focused,
  testMatch: "action-entry-refresh.spec.ts",
  outputDir: fileURLToPath(
    new URL("../../.artifacts/action-entry-refresh", import.meta.url),
  ),
  webServer: undefined,
  globalSetup: "./action-entry-server.ts",
});
