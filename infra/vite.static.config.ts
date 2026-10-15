// Только сборка статических файлов. HTTP-границы обеспечивает сгенерированный Nginx.
import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";
import { webConfig, assertOwnedBuildOutput } from "../apps/web/vite.shared.ts";

export default defineConfig(({ mode }) => {
  if (!["landing", "dashboard"].includes(mode))
    throw new Error("Unknown static profile");
  const config = webConfig(mode === "landing");
  const root = fileURLToPath(new URL("../", import.meta.url));
  const output = `${root}.artifacts/web/production-static/${mode}`;
  assertOwnedBuildOutput(root, output);
  return {
    ...config,
    envPrefix: [],
    plugins: config.plugins?.filter(
      (plugin) =>
        !(
          plugin &&
          typeof plugin === "object" &&
          "name" in plugin &&
          plugin.name === "instance-public-metadata"
        ),
    ),
    build: { ...config.build, outDir: output },
  };
});
