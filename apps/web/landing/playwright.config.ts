import { defineConfig } from "@playwright/test";
import { fileURLToPath } from "node:url";
const root = fileURLToPath(new URL("../../../", import.meta.url));
process.env.PLAYWRIGHT_BROWSERS_PATH = `${root}.artifacts/playwright-browsers`;
export default defineConfig({
  testDir: ".",
  testMatch: "landing.spec.ts",
  workers: 1,
  outputDir: `${root}.artifacts/v6-design/results`,
  reporter: [["list"]],
  use: { headless: true, locale: "ru-RU" },
});
