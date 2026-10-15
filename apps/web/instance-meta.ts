import { readFileSync } from "node:fs";
import type { Plugin } from "vite";
import {
  neutralInstance,
  publicInstance,
} from "../../packages/web-ui/instance-value.ts";
import type { PublicInstance } from "../../packages/api-client/types.ts";
import { instanceTerminology } from "../../packages/web-ui/terminology.ts";
import {
  restrictedRobots,
  searchRobots,
} from "../../packages/web-ui/search-indexing.ts";

export async function fetchPublicInstance(
  backend: string,
): Promise<PublicInstance> {
  try {
    const response = await fetch(`${backend}/api/v1/public-config`, {
      redirect: "error",
      signal: AbortSignal.timeout(2000),
      headers: { accept: "application/json" },
    });
    if (!response.ok || !response.body) return neutralInstance;
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let bytes = 0;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.length;
        if (bytes > 65536) throw new Error("Public configuration is too large");
        chunks.push(chunk.value);
      }
    } finally {
      await reader.cancel();
    }
    const body = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.length;
    }
    return publicInstance(JSON.parse(new TextDecoder().decode(body)).instance);
  } catch {
    return neutralInstance;
  }
}
export function instanceManifest(instance: PublicInstance) {
  return {
    name: instance.app_name,
    short_name: instance.app_short_name,
    id: "/",
    start_url: "/",
    display: "standalone",
    background_color: "#101316",
    theme_color: "#101316",
    lang: "ru",
    ...(instance.favicon_url
      ? { icons: [{ src: instance.favicon_url, sizes: "any" }] }
      : {}),
  };
}
const escape = (value: string) =>
  value.replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ]!,
  );
export function instanceHtml(
  html: string,
  instance: PublicInstance,
  landing: boolean,
  testEnvironment = true,
) {
  const title = `${landing ? instanceTerminology(instance).service_summary : "Личный кабинет"} — ${instance.app_name}`;
  const json = JSON.stringify(instance).replace(
    /[<>&\u2028\u2029]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  const tags = [
    `<meta name="robots" content="${searchRobots(instance, landing, instance.public_site_url, testEnvironment)}" />`,
    `<meta property="og:title" content="${escape(title)}" />`,
    `<meta property="og:description" content="${escape(instance.app_description)}" />`,
    ...(instance.favicon_url
      ? [`<link rel="icon" href="${instance.favicon_url}" />`]
      : []),
    ...(instance.og_image_url
      ? [`<meta property="og:image" content="${instance.og_image_url}" />`]
      : []),
    `<script>window.__PUBLIC_INSTANCE__=${json};</script>`,
    `<script>window.__SEARCH_INDEXING_TEST_ENVIRONMENT__=${testEnvironment};</script>`,
  ].join("\n");
  return html
    .replace(
      /<meta\b(?=[^>]*\bname\s*=\s*["'](?:robots|googlebot|bingbot)["'])[^>]*>/gi,
      "",
    )
    .replace(/<title>[^<]*<\/title>/, () => `<title>${escape(title)}</title>`)
    .replace(
      /data-visual-profile="[^"]*"/,
      () => `data-visual-profile="${instance.visual_profile}"`,
    )
    .replace(
      /<meta\s+name="description"\s+content="[^"]*"\s*\/>/,
      () =>
        `<meta name="description" content="${escape(instance.app_description)}" />`,
    )
    .replace("</head>", () => `${tags}\n</head>`);
}
export function instanceMetadata(
  backend: string,
  landing: boolean,
  project: string,
): Plugin {
  const middleware = (server: {
    middlewares: {
      use: (
        fn: (
          req: { url?: string },
          res: {
            setHeader: (key: string, value: string) => void;
            end: (body: string) => void;
          },
          next: () => void,
        ) => void,
      ) => void;
    };
  }) => {
    server.middlewares.use((req, res, next) => {
      res.setHeader("X-Robots-Tag", restrictedRobots);
      if (req.url?.split("?")[0] === "/robots.txt") {
        res.setHeader("Content-Type", "text/plain; charset=utf-8");
        res.setHeader("Cache-Control", "no-store");
        res.end("User-agent: *\nDisallow: /\n");
        return;
      }
      if (req.url !== "/site.webmanifest") return next();
      void fetchPublicInstance(backend).then((instance) => {
        res.setHeader(
          "Content-Type",
          "application/manifest+json; charset=utf-8",
        );
        res.setHeader("Cache-Control", "no-store");
        res.end(JSON.stringify(instanceManifest(instance)));
      });
    });
  };
  return {
    name: "instance-public-metadata",
    configureServer: middleware,
    configurePreviewServer: middleware,
    transformIndexHtml: async (html) => {
      const instance = await fetchPublicInstance(backend);
      return instanceHtml(html, instance, landing);
    },
    async generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "robots.txt",
        source: "User-agent: *\nDisallow: /\n",
      });
      const instance = await fetchPublicInstance(backend);
      this.emitFile({
        type: "asset",
        fileName: "site.webmanifest",
        source: JSON.stringify(instanceManifest(instance)),
      });
      this.emitFile({
        type: "asset",
        fileName: "sw.js",
        source: readFileSync(`${project}apps/web/public/sw.js`, "utf8"),
      });
    },
  };
}

export function distributionNotices(project: string): Plugin {
  return {
    name: "distribution-license-notices",
    generateBundle() {
      for (const [fileName, path] of [
        ["licenses/AGPL-3.0.txt", "LICENSE"],
        ["licenses/NOTICE.txt", "NOTICE"],
        ["licenses/THIRD_PARTY_NOTICES.txt", "docs/THIRD_PARTY_NOTICES.md"],
      ]) {
        this.emitFile({
          type: "asset",
          fileName,
          source: readFileSync(`${project}${path}`, "utf8"),
        });
      }
    },
  };
}
