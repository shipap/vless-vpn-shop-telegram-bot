import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      "@ui": fileURLToPath(
        new URL("../../../packages/web-ui", import.meta.url),
      ),
      "@api": fileURLToPath(
        new URL("../../../packages/api-client", import.meta.url),
      ),
    },
  },
  test: { include: ["apps/web/dashboard/*.test.tsx"] },
});
