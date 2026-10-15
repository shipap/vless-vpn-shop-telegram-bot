import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
export default defineConfig({
  ...focused,
  testMatch: "checkout-functional.spec.ts",
  outputDir: "../../.artifacts/website-functional-resume/checkout-results",
  reporter: [
    ["list"],
    [
      "json",
      {
        outputFile: ".artifacts/website-functional-resume/checkout-report.json",
      },
    ],
  ],
});
