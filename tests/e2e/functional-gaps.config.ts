import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
export default defineConfig({
  ...focused,
  testMatch: "functional-gaps.spec.ts",
  outputDir: "../../.artifacts/website-functional-gaps/results",
  reporter: [
    ["list"],
    ["json", { outputFile: ".artifacts/website-functional-gaps/report.json" }],
  ],
  use: { ...focused.use, trace: "retain-on-failure" },
});
