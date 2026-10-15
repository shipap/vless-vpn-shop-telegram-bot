import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
export default defineConfig({
  ...focused,
  testMatch: ["promos.spec.ts", "v10-promo-ux.spec.ts"],
  outputDir: "../../.artifacts/owner-web-parity/results",
  reporter: [["list"]],
});
