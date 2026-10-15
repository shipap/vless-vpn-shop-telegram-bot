import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { runInNewContext } from "node:vm";
import { build, createServer, preview, type ViteDevServer } from "vite";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { instanceHtml, instanceMetadata } from "../../apps/web/instance-meta";
import { webConfig } from "../../apps/web/vite.shared";
import { neutralInstance, publicInstance } from "./instance-value";
import { searchRobots } from "./search-indexing";

const directive = "noindex,nofollow,noarchive";
const optIn = {
  ...neutralInstance,
  search_indexing_enabled: true,
  public_site_url: "https://vpn.operator.net",
};
const servers: ViteDevServer[] = [];

beforeEach(() => {
  vi.stubEnv("WEB_INSTANCE_ID", "indexing-tests");
});

it("keeps the test optimizer separate from both live website caches", () => {
  for (const landing of [true, false]) {
    expect(resolve(webConfig(landing).cacheDir!)).not.toBe(
      resolve(`.artifacts/vite-${landing ? "landing" : "dashboard"}`),
    );
  }
});
afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

it("renders fail-closed robots metadata before JavaScript for both website templates", () => {
  for (const landing of [true, false]) {
    const html = instanceHtml(
      "<html><head><title>VPN</title></head></html>",
      optIn,
      landing,
    );
    expect(html).toContain(`<meta name="robots" content="${directive}"`);
    const browser: Record<string, unknown> = {};
    for (const script of html.matchAll(/<script>(.*?)<\/script>/gs)) {
      runInNewContext(script[1], { window: browser });
    }
    expect(browser.__SEARCH_INDEXING_TEST_ENVIRONMENT__).toBe(true);
  }
});

it("allows only explicitly public landing metadata when local restrictions are disabled", () => {
  const template =
    '<html><head><meta name="robots" content="noindex,nofollow"/><title>VPN</title></head></html>';
  // The explicit production context is exercised without enabling any actual deployment.
  expect(instanceHtml(template, optIn, true, false)).toContain(
    'name="robots" content="index,follow"',
  );
  expect(instanceHtml(template, optIn, false, false)).toContain(
    `content="${directive}"`,
  );
  expect(
    instanceHtml(
      template,
      { ...optIn, visual_profile: "private" },
      true,
      false,
    ),
  ).toContain(`content="${directive}"`);
  expect(
    instanceHtml(
      template,
      { ...optIn, public_site_url: "http://127.0.0.1:3100" },
      true,
      false,
    ),
  ).toContain(`content="${directive}"`);
});

it("keeps malformed and missing public indexing flags disabled", () => {
  expect(
    publicInstance({ ...optIn, search_indexing_enabled: "true" })
      .search_indexing_enabled,
  ).toBe(false);
  expect(
    publicInstance({ ...optIn, search_indexing_enabled: undefined })
      .search_indexing_enabled,
  ).toBe(false);
  expect(publicInstance(optIn).search_indexing_enabled).toBe(true);
});

it("emits robots policy with built assets even when the backend cannot supply configuration", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  const plugin = instanceMetadata("http://127.0.0.1:1", true, "./");
  const transform = plugin.transformIndexHtml as (
    html: string,
  ) => Promise<string>;
  const html = await transform("<html><head><title>VPN</title></head></html>");
  expect(html).toContain(`content="${directive}"`);
  const assets: { fileName: string; source: string }[] = [];
  const generate = plugin.generateBundle as Function;
  generate.call({
    emitFile: (asset: { fileName: string; source: string }) =>
      assets.push(asset),
  });
  expect(assets.find((asset) => asset.fileName === "robots.txt")?.source).toBe(
    "User-agent: *\nDisallow: /\n",
  );
});

it("serves local landing and dashboard HTML and robots with headers regardless of a true flag", async () => {
  // Only the external config transport is faked; real Vite consumers serve both templates.
  const realFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: string | URL | Request, init?: RequestInit) =>
    String(input) === "http://127.0.0.1:8100/api/v1/public-config"
      ? Promise.resolve(
          new Response(JSON.stringify({ instance: optIn }), {
            headers: { "content-type": "application/json" },
          }),
        )
      : String(input) === "http://127.0.0.1:8100/api/v1/admin/access"
        ? Promise.resolve(new Response("Not found", { status: 404 }))
        : realFetch(input, init),
  );
  for (const landing of [true, false]) {
    const server = await createServer({
      ...webConfig(landing),
      configFile: false,
      server: { ...webConfig(landing).server, port: 0, strictPort: false },
    });
    servers.push(server);
    await server.listen();
    const origin = server.resolvedUrls!.local[0];
    const html = await realFetch(origin);
    expect(html.headers.get("x-robots-tag")).toBe(directive);
    expect(await html.text()).toContain(`name="robots" content="${directive}"`);
    const robots = await realFetch(new URL("robots.txt?test=1", origin));
    expect(robots.status).toBe(200);
    expect(robots.headers.get("x-robots-tag")).toBe(directive);
    expect(await robots.text()).toBe("User-agent: *\nDisallow: /\n");
    if (!landing) {
      const admin = await realFetch(new URL("admin", origin));
      expect(admin.status).toBe(404);
      expect(admin.headers.get("x-robots-tag")).toBe(directive);
    }
  }
}, 20000);

it("keeps dynamic metadata denied on localhost, test hosts, private paths and mismatched origins", () => {
  expect(searchRobots(optIn, true, "https://vpn.operator.net/", false)).toBe(
    "index,follow",
  );
  for (const path of [
    "/admin",
    "/account",
    "/dashboard",
    "/auth/code",
    "/login",
    "/api/v1/catalog",
    "/%61dmin",
  ]) {
    expect(
      searchRobots(optIn, true, `https://vpn.operator.net${path}`, false),
    ).toBe(directive);
  }
  for (const url of [
    "http://localhost:3100",
    "http://127.0.0.1:3100",
    "https://another.operator.net",
    "https://vpn.example.test",
  ]) {
    expect(searchRobots(optIn, true, url, false)).toBe(directive);
  }
  expect(searchRobots(optIn, false, "https://vpn.operator.net/", false)).toBe(
    directive,
  );
  expect(
    searchRobots(neutralInstance, true, "https://vpn.operator.net/", false),
  ).toBe(directive);
});

it("preserves noindex and robots in actual build output and preview responses without a backend", async () => {
  vi.stubEnv("WEB_INSTANCE_ID", "indexing-verification");
  const realFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: string | URL | Request, init?: RequestInit) =>
    String(input).startsWith("http://127.0.0.1:8100")
      ? Promise.reject(new Error("offline"))
      : realFetch(input, init),
  );
  for (const landing of [true, false]) {
    const config = webConfig(landing);
    await build({ ...config, configFile: false, logLevel: "silent" });
    expect(
      readFileSync(`${config.build!.outDir}/index.html`, "utf8"),
    ).toContain(`name="robots" content="${directive}"`);
    expect(readFileSync(`${config.build!.outDir}/robots.txt`, "utf8")).toBe(
      "User-agent: *\nDisallow: /\n",
    );
    const server = await preview({
      ...config,
      configFile: false,
      preview: { ...config.preview, port: 0, strictPort: false },
    });
    try {
      const origin = server.resolvedUrls!.local[0];
      const response = await realFetch(origin);
      expect(response.headers.get("x-robots-tag")).toBe(directive);
      expect(await response.text()).toContain(
        `name="robots" content="${directive}"`,
      );
      const robots = await realFetch(new URL("robots.txt", origin));
      expect(robots.headers.get("x-robots-tag")).toBe(directive);
      expect(await robots.text()).toBe("User-agent: *\nDisallow: /\n");
    } finally {
      await server.close();
    }
  }
}, 60000);
