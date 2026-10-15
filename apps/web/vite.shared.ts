import {
  defineConfig,
  type Plugin,
  type Connect,
  type ProxyOptions,
} from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { resolve, relative, isAbsolute, dirname } from "node:path";
import { lstatSync, realpathSync } from "node:fs";
import { instanceMetadata, distributionNotices } from "./instance-meta.ts";
import { restrictedRobots } from "../../packages/web-ui/search-indexing.ts";
const project = fileURLToPath(new URL("../../", import.meta.url));
export function assertOwnedBuildOutput(root: string, output: string) {
  const base = resolve(root);
  const target = resolve(output);
  const within = relative(base, target);
  if (within.startsWith("..") || isAbsolute(within))
    throw new Error("Build output must remain inside the owned project");
  const canonicalRoot = realpathSync(base);
  for (let current = target; ; current = dirname(current)) {
    try {
      const entry = lstatSync(current);
      if (entry.isSymbolicLink())
        throw new Error("Build output links are forbidden");
      const expected = resolve(canonicalRoot, relative(base, current));
      const actual = realpathSync(current);
      const normalize = (value: string) =>
        process.platform === "win32" ? value.toLowerCase() : value;
      if (normalize(actual) !== normalize(expected))
        throw new Error("Build output reparse paths are forbidden");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    if (current === base) break;
  }
}
function guards(landing: boolean, backend: string): Plugin {
  const middleware: Connect.NextHandleFunction = async (req, res, next) => {
    res.setHeader("X-Robots-Tag", restrictedRobots);
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("X-Frame-Options", "DENY");
    res.setHeader("Referrer-Policy", "no-referrer");
    let requested = req.url || "";
    try {
      for (let i = 0; i < 4; i++) {
        const decoded = decodeURIComponent(requested);
        if (decoded === requested) break;
        requested = decoded;
      }
    } catch {
      res.statusCode = 404;
      res.end("Not found");
      return;
    }
    if (
      /(?:^|[\\/])(?:\.private|private-audit|\.git|config|brand|instance-assets(?:-[a-z0-9_-]+)?|\.env(?:\.[^/]*)?|owner-access\.json|local-resources\.json)(?:[\\/?#]|$)/i.test(
        requested,
      ) ||
      /\.(?:sql|dump|bak|pem|key)(?:[?#]|$)/i.test(requested) ||
      requested.includes("..") ||
      /^\/brand(?:\/|\?|$)/.test(requested)
    ) {
      res.statusCode = 404;
      res.end("Not found");
      return;
    }
    if (landing && req.url?.startsWith("/api/")) {
      delete req.headers.cookie;
      delete req.headers.authorization;
      delete req.headers["x-csrf-token"];
      const path = req.url.split("?")[0];
      const publicVisit =
        path === "/api/v1/campaigns/visit" && req.method === "POST";
      if (
        !publicVisit &&
        ((!["/api/v1/catalog", "/api/v1/public-config"].includes(
          req.url.split("?")[0],
        ) &&
          !/^\/api\/v1\/content\/emoji\/[0-9]{1,32}$/.test(
            req.url.split("?")[0],
          ) &&
          !/^\/api\/v1\/instance\/assets\/(?:logo|favicon|hero|og)$/.test(
            path,
          )) ||
          req.method !== "GET")
      ) {
        res.statusCode = 404;
        res.end("Not found");
        return;
      }
    }
    if (!landing && /^\/admin(?:\/|\?|$)/.test(requested)) {
      try {
        const response = await fetch(`${backend}/api/v1/admin/access`, {
          headers: { cookie: req.headers.cookie || "" },
          signal: AbortSignal.timeout(5000),
          redirect: "error",
        });
        if (response.status !== 200) {
          res.statusCode = 404;
          res.setHeader("Content-Type", "text/html; charset=utf-8");
          res.end(
            '<!doctype html><html lang="ru"><title>Страница не найдена</title><h1>Страница не найдена</h1><a href="/">На главную</a></html>',
          );
          return;
        }
      } catch {
        res.statusCode = 404;
        res.end("Not found");
        return;
      }
    }
    next();
  };
  return {
    name: "sunrise-private-routes",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}
export function webConfig(
  landing: boolean,
  e2e?: { backend: string; devPort: number; previewPort: number },
) {
  if (
    e2e &&
    (e2e.backend !== "http://127.0.0.1:8150" ||
      e2e.devPort !== (landing ? 3110 : 3210) ||
      e2e.previewPort !== (landing ? 3111 : 3211))
  )
    throw new Error("Isolated browser configuration is not allowed");
  const configuredBackend =
    e2e?.backend ?? process.env.WEB_API_BASE_URL ?? "http://127.0.0.1:8100";
  const url = new URL(configuredBackend);
  if (
    url.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(url.hostname) ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    throw new Error("Web API must use a loopback HTTP origin");
  const backend = url.origin;
  const port = (value: string | undefined, fallback: number) => {
    if (value === undefined) return fallback;
    if (!/^\d+$/.test(value) || Number(value) < 1024 || Number(value) > 65535)
      throw new Error("Invalid web port");
    return Number(value);
  };
  const id = e2e ? undefined : process.env.WEB_INSTANCE_ID;
  if (id !== undefined && !/^[a-z0-9-]{1,48}$/.test(id))
    throw new Error("Invalid web instance ID");
  const output = e2e ? undefined : process.env.WEB_OUTPUT_DIR;
  const outputBase = resolve(project, ".artifacts/web");
  const requestedOutput = output
    ? resolve(project, output)
    : resolve(outputBase, id ?? "");
  const outputRelative = relative(outputBase, requestedOutput);
  if (
    output &&
    (isAbsolute(output) ||
      outputRelative.startsWith("..") ||
      isAbsolute(outputRelative))
  )
    throw new Error("Web output must remain inside .artifacts/web");
  assertOwnedBuildOutput(
    project,
    resolve(requestedOutput, landing ? "landing" : "dashboard"),
  );
  const devPort =
    e2e?.devPort ?? port(process.env.WEB_DEV_PORT, landing ? 3100 : 3200);
  const previewPort =
    e2e?.previewPort ??
    port(process.env.WEB_PREVIEW_PORT, landing ? 3101 : 3201);
  const proxy: Record<string, ProxyOptions> = {
    "/api": {
      target: backend,
      changeOrigin: false,
      configure(proxy) {
        if (landing)
          proxy.on("proxyRes", (response) => {
            delete response.headers["set-cookie"];
          });
      },
    },
  };
  return defineConfig({
    root: fileURLToPath(
      new URL(landing ? "./landing/" : "./dashboard/", import.meta.url),
    ),
    publicDir: `${project}apps/web/public`,
    plugins: [
      guards(landing, backend),
      instanceMetadata(backend, landing, project),
      distributionNotices(project),
      react(),
    ],
    resolve: {
      alias: {
        "@ui": `${project}packages/web-ui`,
        "@api": `${project}packages/api-client`,
      },
    },
    cacheDir: `${project}.artifacts/vite-${id ? `${id}-` : ""}${landing ? "landing" : "dashboard"}`,
    server: {
      host: "127.0.0.1",
      port: devPort,
      strictPort: true,
      fs: {
        allow: [
          `${project}apps/web`,
          `${project}packages/web-ui`,
          `${project}packages/api-client`,
          `${project}node_modules`,
        ],
        deny: [
          "**/.private/**",
          "**/instance-assets*/**",
          "**/brand/**",
          "**/private-audit/**",
          "**/.env*",
          "**/.git/**",
          "**/config/**",
          "**/*.sql",
          "**/*.dump",
          "**/*.{pem,key,bak}",
          "**/owner-access.json",
          "**/local-resources.json",
        ],
      },
      proxy,
    },
    preview: {
      host: "127.0.0.1",
      port: previewPort,
      strictPort: true,
      proxy,
    },
    build: {
      outDir: `${requestedOutput.replaceAll("\\", "/")}/${landing ? "landing" : "dashboard"}`,
      copyPublicDir: false,
      emptyOutDir: true,
      sourcemap: false,
    },
  });
}
