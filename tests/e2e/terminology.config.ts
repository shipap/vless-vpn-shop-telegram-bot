import { defineConfig } from "@playwright/test";
import focused from "./broadcast-audience.config";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const server = focused.webServer;
if (!server || Array.isArray(server))
  throw new Error("Expected one local server");
const landing = process.env.SUNRISE_TERMINOLOGY_SURFACE === "landing";
export default defineConfig({
  ...focused,
  testMatch: "terminology.spec.ts",
  outputDir: `${root}.artifacts/terminology-${landing ? "landing" : "dashboard"}`,
  webServer: {
    ...server,
    command: landing ? "npm.cmd run dev:landing" : "npm.cmd run dev:dashboard",
    env: {
      ...server.env,
      WEB_INSTANCE_ID: `terminology-${landing ? "landing" : "dashboard"}`,
    },
  },
});
