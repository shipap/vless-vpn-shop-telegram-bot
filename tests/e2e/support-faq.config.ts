import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import focused from "./broadcast-audience.config";

const root = fileURLToPath(new URL("../../", import.meta.url));
const server = focused.webServer;
if (!server || Array.isArray(server))
  throw new Error("Expected one focused local server");
export default defineConfig({
  ...focused,
  testMatch: "support-faq.spec.ts",
  outputDir: `${root}.artifacts/support-faq`,
  webServer: {
    ...server,
    env: { ...server.env, WEB_INSTANCE_ID: "support-faq" },
  },
});
