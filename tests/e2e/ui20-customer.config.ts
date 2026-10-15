import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
import focused from "./broadcast-audience.config";

export default defineConfig({
  ...focused,
  testMatch: [
    "ui20-customer.spec.ts",
    "v11-notifications.spec.ts",
    "support-faq.spec.ts",
  ],
  outputDir: fileURLToPath(
    new URL("../../.artifacts/ui20-customer", import.meta.url),
  ),
});
