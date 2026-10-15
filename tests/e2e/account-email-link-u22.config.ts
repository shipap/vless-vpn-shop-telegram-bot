import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";

export default defineConfig({
  ...focused,
  testMatch: "account-email-link-u22.spec.ts",
  outputDir: ".artifacts/account-email-link-u22",
  expect: { timeout: 3000 },
});
