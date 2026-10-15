import { afterEach, expect, it, vi } from "vitest";
import {
  fetchPublicInstance,
  instanceHtml,
  instanceManifest,
  instanceMetadata,
  distributionNotices,
} from "../../apps/web/instance-meta";
import { neutralInstance } from "./instance-value";
import { webConfig } from "../../apps/web/vite.shared";
import type { UserConfig } from "vite";

type EmittedAsset = { fileName: string; source: string };
async function generateMetadata(landing: boolean, backend: string) {
  const plugin = instanceMetadata(backend, landing, "./");
  const assets: EmittedAsset[] = [];
  const generate = plugin.generateBundle as unknown as (this: {
    emitFile: (asset: EmittedAsset) => void;
  }) => void | Promise<void>;
  await generate.call({
    emitFile: (asset) => {
      assets.push(asset);
    },
  });
  return {
    plugin,
    assets,
    manifest: JSON.parse(
      assets.find((asset) => asset.fileName === "site.webmanifest")!.source,
    ),
  };
}
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it("uses the public default name without a private image", () => {
  expect(neutralInstance.app_name).toBe("VLESS VPN Shop");
  expect(neutralInstance.app_short_name).toBe("VLESS VPN Shop");
  expect(neutralInstance.sender_name).toBe("VLESS VPN Shop");
  expect(neutralInstance.visual_profile).toBe("neutral");
  expect(neutralInstance.logo_url).toBeNull();
  expect(neutralInstance.hero_url).toBeNull();
});

it("includes complete source and third-party notices in distributable web builds", async () => {
  const plugin = distributionNotices("./");
  const assets: EmittedAsset[] = [];
  const generate = plugin.generateBundle as unknown as (this: {
    emitFile: (asset: EmittedAsset) => void;
  }) => void;
  generate.call({ emitFile: (asset) => assets.push(asset) });
  expect(assets.map((asset) => asset.fileName)).toEqual([
    "licenses/AGPL-3.0.txt",
    "licenses/NOTICE.txt",
    "licenses/THIRD_PARTY_NOTICES.txt",
  ]);
  expect(assets[0].source).toContain("GNU AFFERO GENERAL PUBLIC LICENSE");
  expect(assets[1].source).toContain("shipap");
  expect(assets[2].source).toContain("MIT License");
});

it.each([true, false])(
  "brands emitted manifest before the HTML hook for landing=%s",
  async (landing) => {
    const instance = {
      ...neutralInstance,
      app_name: "Синтетический сервис",
      app_short_name: "Синтетика",
      favicon_url: "/api/v1/instance/assets/favicon",
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(
          async () => new Response(JSON.stringify({ instance })),
        ),
    );
    const { plugin, manifest } = await generateMetadata(
      landing,
      "http://localhost:8300",
    );
    expect(manifest).toMatchObject({
      name: instance.app_name,
      short_name: instance.app_short_name,
      icons: [{ src: instance.favicon_url, sizes: "any" }],
    });
    const transform = plugin.transformIndexHtml as (
      html: string,
    ) => Promise<string>;
    const html = await transform(
      "<html><head><title>VPN</title></head></html>",
    );
    expect(html).toContain(instance.app_name);
    expect(html).toContain(instance.favicon_url);
  },
);

it.each(["unavailable", "oversized"])(
  "keeps neutral manifest fallback before HTML on %s config",
  async (failure) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => {
        if (failure === "unavailable") throw new Error("synthetic offline");
        return new Response("x".repeat(65537));
      }),
    );
    const { assets, manifest } = await generateMetadata(
      true,
      "http://localhost:8300",
    );
    expect(manifest).toEqual(instanceManifest(neutralInstance));
    expect(manifest).not.toHaveProperty("icons");
    expect(
      assets.find((asset) => asset.fileName === "robots.txt")?.source,
    ).toBe("User-agent: *\nDisallow: /\n");
    expect(
      assets.find((asset) => asset.fileName === "sw.js")?.source,
    ).toContain("self.");
  },
);

it("does not mix concurrent metadata plugin instances", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (url: string) => {
      const app_name = url.startsWith("http://localhost:8300/")
        ? "Первый сервис"
        : "Второй сервис";
      return new Response(
        JSON.stringify({
          instance: { ...neutralInstance, app_name, app_short_name: app_name },
        }),
      );
    }),
  );
  const [first, second] = await Promise.all([
    generateMetadata(true, "http://localhost:8300"),
    generateMetadata(false, "http://localhost:8301"),
  ]);
  expect(first.manifest.name).toBe("Первый сервис");
  expect(second.manifest.name).toBe("Второй сервис");
});
it("preserves dollar replacement patterns as literal instance identity in metadata and boot JSON", () => {
  const instance = {
    ...neutralInstance,
    app_name: "Сервис $& $` $' $$ <название>",
    app_description: "Описание $& $` $' $$ <текст>",
  };
  const html = instanceHtml(
    '<html data-visual-profile="neutral"><head><title>VPN</title><meta name="description" content="VPN" /></head><body>Контент</body></html>',
    instance,
    true,
  );
  const json = html.match(
    /<script>window\.__PUBLIC_INSTANCE__=(.*?);<\/script>/s,
  )?.[1];
  expect(json).toBeDefined();
  expect(JSON.parse(json!)).toEqual(instance);
  expect(html).toContain("Сервис $&amp; $` $&#39; $$ &lt;название&gt;</title>");
  expect(html).toContain(
    'content="Описание $&amp; $` $&#39; $$ &lt;текст&gt;"',
  );
});
it("separates two frontend instance build outputs and refuses unsafe deployment targets", () => {
  vi.stubEnv("WEB_INSTANCE_ID", "demo-vpn");
  vi.stubEnv("WEB_DEV_PORT", "3300");
  vi.stubEnv("WEB_PREVIEW_PORT", "3301");
  vi.stubEnv("WEB_API_BASE_URL", "http://localhost:8300");
  const config = webConfig(true) as UserConfig;
  expect(config.server?.port).toBe(3300);
  expect(config.preview?.port).toBe(3301);
  expect(config.build?.outDir).toMatch(/\.artifacts\/web\/demo-vpn\/landing$/);
  vi.stubEnv("WEB_API_BASE_URL", "http://localhost:8300/");
  expect((webConfig(true) as UserConfig).server?.proxy?.["/api"]).toMatchObject(
    { target: "http://localhost:8300" },
  );
  for (const value of [
    "https://127.0.0.1:8300",
    "http://external.test:8300",
    "http://user:secret@localhost:8300",
    "http://localhost:8300/api",
    "http://localhost:8300/?x=1",
  ]) {
    vi.stubEnv("WEB_API_BASE_URL", value);
    expect(() => webConfig(true), value).toThrow();
  }
  vi.stubEnv("WEB_API_BASE_URL", "http://localhost:8300");
  vi.stubEnv("WEB_OUTPUT_DIR", "../../private");
  expect(() => webConfig(true)).toThrow();
});
it("injects only escaped public identity into HTML and builds a neutral manifest on unavailable or excessive config", async () => {
  const instance = {
    ...neutralInstance,
    app_name: 'Другой </script><script>alert("x")</script>',
    app_description: 'Описание < & "',
    favicon_url: "/api/v1/instance/assets/favicon",
  };
  const html = instanceHtml(
    '<html data-visual-profile="neutral"><head><title>VPN</title><meta name="description" content="VPN" /></head></html>',
    instance,
    false,
  );
  expect(html).not.toContain("</script><script>alert");
  expect(html).toContain("\\u003c/script\\u003e");
  expect(html).toContain("Описание &lt; &amp; &quot;");
  expect(instanceManifest(instance).name).toBe(instance.app_name);
  const fetchMock = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({ instance, private_secret: "NEVER_EXPORT" }),
        { headers: { "content-type": "application/json" } },
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  const config = await fetchPublicInstance("http://localhost:8300");
  expect(config.app_name).toBe(instance.app_name);
  expect(JSON.stringify(config)).not.toContain("NEVER_EXPORT");
  expect(fetchMock.mock.calls[0][1].headers).toEqual({
    accept: "application/json",
  });
  expect(fetchMock.mock.calls[0][1].redirect).toBe("error");
  fetchMock.mockResolvedValueOnce(new Response("x".repeat(65537)));
  expect(await fetchPublicInstance("http://localhost:8300")).toEqual(
    neutralInstance,
  );
  fetchMock.mockRejectedValueOnce(new Error("unavailable"));
  expect(await fetchPublicInstance("http://localhost:8300")).toEqual(
    neutralInstance,
  );
});
it("keeps isolated browser ports and backend independent of runtime instance overrides", () => {
  vi.stubEnv("WEB_API_BASE_URL", "http://external.test");
  vi.stubEnv("WEB_DEV_PORT", "9000");
  vi.stubEnv("WEB_OUTPUT_DIR", "../../private");
  const config = webConfig(false, {
    backend: "http://127.0.0.1:8150",
    devPort: 3210,
    previewPort: 3211,
  }) as UserConfig;
  expect(config.server?.port).toBe(3210);
  expect(config.build?.outDir).toMatch(/\.artifacts\/web\/dashboard$/);
});

it("refuses existing build-output junctions before Vite can empty directories", async () => {
  const {
    mkdtempSync,
    mkdirSync,
    symlinkSync,
    unlinkSync,
    rmSync,
    realpathSync,
  } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const module = await import("../../apps/web/vite.shared");
  const guard = (
    module as unknown as {
      assertOwnedBuildOutput?: (root: string, output: string) => void;
    }
  ).assertOwnedBuildOutput;
  expect(guard).toBeTypeOf("function");
  const temp = mkdtempSync(join(tmpdir(), "v7-build-guard-"));
  const root = join(temp, "project");
  const outside = join(temp, "outside");
  mkdirSync(join(root, ".artifacts"), { recursive: true });
  mkdirSync(outside);
  const link = join(root, ".artifacts", "web");
  try {
    symlinkSync(outside, link, "junction");
    expect(() => guard!(root, join(link, "demo", "landing"))).toThrow();
    unlinkSync(link);
    expect(() => guard!(root, join(link, "demo", "landing"))).not.toThrow();
  } finally {
    try {
      unlinkSync(link);
    } catch {}
    expect(realpathSync(temp)).toBe(temp);
    rmSync(temp, { recursive: true, force: true });
  }
});
