import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";

export default defineConfig({
  ...focused,
  testMatch: "admin-layout-ui20.spec.ts",
  outputDir: ".artifacts/admin-layout-ui20",
});
