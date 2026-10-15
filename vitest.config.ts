import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
export default defineConfig({
  resolve: {
    alias: {
      "@api": fileURLToPath(new URL("./packages/api-client", import.meta.url)),
      "@ui": fileURLToPath(new URL("./packages/web-ui", import.meta.url)),
    },
  },
  test: {
    include: [
      "packages/web-ui/**/*.test.ts",
      "packages/web-ui/**/*.test.tsx",
      "packages/api-client/**/*.test.ts",
    ],
  },
});
