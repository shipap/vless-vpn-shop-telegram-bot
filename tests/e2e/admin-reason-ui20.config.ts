import { defineConfig } from "@playwright/test";

// Старый синтетический HTML fixture использует этот отдельный loopback-порт.
process.env.SUNRISE_BROADCAST_E2E_PORT = "3200";
const { default: focused } = await import("./broadcast-audience.config");

export default defineConfig({
  ...focused,
  testMatch: "v6-admin.spec.ts",
  grep: /документы сохраняют версии с MFA, автоматическим аудитом и CSRF; список сохраняет BIGINT/,
  outputDir: ".artifacts/admin-reason-ui20",
});
