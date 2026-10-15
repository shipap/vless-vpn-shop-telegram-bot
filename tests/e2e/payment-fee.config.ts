import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("../../", import.meta.url));
const server = focused.webServer;
if (!server || Array.isArray(server))
  throw new Error("Expected one local server");
export default defineConfig({
  ...focused,
  testMatch: "v11-service.spec.ts",
  outputDir: `${root}.artifacts/payment-fee`,
  webServer: {
    ...server,
    env: { ...server.env, WEB_INSTANCE_ID: "payment-fee" },
  },
});
