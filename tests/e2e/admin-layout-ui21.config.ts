import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";

export default defineConfig({
  ...focused,
  testMatch: "admin-layout-ui21.spec.ts",
  outputDir: ".artifacts/admin-layout-ui21",
});
