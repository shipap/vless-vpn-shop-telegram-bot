// Однократная персонализация публичной статики без сети, секретов и перезаписи output.
import { createHash } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import {
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  extname,
} from "node:path";
import { fileURLToPath } from "node:url";
import { instanceHtml, instanceManifest } from "../apps/web/instance-meta.ts";
import { publicInstance } from "../packages/web-ui/instance-value.ts";
import { searchRobots } from "../packages/web-ui/search-indexing.ts";

const sha = (body: string | Buffer) =>
  createHash("sha256").update(body).digest("hex");
function noLinks(path: string) {
  for (let current = resolve(path); ; current = dirname(current)) {
    if (existsSync(current)) {
      const stat = lstatSync(current);
      if (
        stat.isSymbolicLink() ||
        realpathSync(current).toLowerCase() !== current.toLowerCase()
      )
        throw new Error("Linked path");
    }
    if (dirname(current) === current) break;
  }
}
function filesUnder(root: string): string[] {
  noLinks(root);
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    noLinks(path);
    if (entry.isDirectory()) return filesUnder(path);
    if (!entry.isFile()) throw new Error("Special file");
    return [path];
  });
}
function csp(html: string) {
  const hashes = [
    ...html.matchAll(/<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g),
  ].map(
    (match) =>
      `'sha256-${createHash("sha256").update(match[1]).digest("base64")}'`,
  );
  const policy = `default-src 'self'; script-src 'self' ${hashes.join(" ")} https://telegram.org https://oauth.telegram.org https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self' https://challenges.cloudflare.com; frame-src https://oauth.telegram.org https://challenges.cloudflare.com; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'`;
  return `add_header Content-Security-Policy "${policy}" always;\n`;
}
function main() {
  const [inputArg, outputArg, configArg] = process.argv.slice(2);
  if (!inputArg || !outputArg || !configArg || process.argv.length !== 5)
    throw new Error("Three fixed paths required");
  const input = resolve(inputArg),
    output = resolve(outputArg),
    config = resolve(configArg);
  noLinks(input);
  noLinks(output);
  noLinks(config);
  const nested = (base: string, path: string) => {
    const part = relative(base, path);
    return (
      !isAbsolute(part) &&
      part !== ".." &&
      !part.startsWith("../") &&
      !part.startsWith("..\\")
    );
  };
  if (nested(input, output) || nested(output, input))
    throw new Error("Overlapping paths");
  if (lstatSync(config).size > 65536)
    throw new Error("Oversized public config");
  const instance = publicInstance(JSON.parse(readFileSync(config, "utf8")));
  const files = new Map<string, Buffer>();
  for (const profile of ["landing", "dashboard"]) {
    const source = join(input, profile);
    for (const path of filesUnder(source)) {
      if (
        ![
          ".html",
          ".js",
          ".css",
          ".woff",
          ".woff2",
          ".ttf",
          ".svg",
          ".png",
          ".webp",
          ".jpg",
          ".ico",
        ].includes(extname(path)) &&
        ![
          "licenses/AGPL-3.0.txt",
          "licenses/NOTICE.txt",
          "licenses/THIRD_PARTY_NOTICES.txt",
        ].includes(relative(source, path).replaceAll("\\", "/"))
      )
        throw new Error("Unexpected static file");
      const name = relative(input, path).replaceAll("\\", "/");
      files.set(name, readFileSync(path));
    }
    const index = `${profile}/index.html`;
    const html = instanceHtml(
      files.get(index)!.toString("utf8"),
      instance,
      profile === "landing",
      false,
    );
    if ((html.match(/window\.__PUBLIC_INSTANCE__=/g) || []).length !== 1)
      throw new Error("Already personalized HTML");
    files.set(index, Buffer.from(html));
    files.set(`${profile}/csp.conf`, Buffer.from(csp(html)));
    files.set(
      `${profile}/site.webmanifest`,
      Buffer.from(JSON.stringify(instanceManifest(instance))),
    );
    const indexed =
      searchRobots(
        instance,
        profile === "landing",
        instance.public_site_url,
        false,
      ) === "index,follow";
    files.set(
      `${profile}/robots.txt`,
      Buffer.from(`User-agent: *\n${indexed ? "Allow" : "Disallow"}: /\n`),
    );
    files.set(
      `${profile}/sw.js`,
      readFileSync(
        fileURLToPath(new URL("../apps/web/public/sw.js", import.meta.url)),
      ),
    );
    files.set(
      `${profile}/404.html`,
      Buffer.from(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Страница не найдена</title><h1>Страница не найдена</h1><a href="/">На главную</a></html>',
      ),
    );
  }
  const receipt =
    JSON.stringify(
      {
        version: 1,
        files: Object.fromEntries(
          [...files].map(([name, body]) => [name, sha(body)]),
        ),
      },
      null,
      2,
    ) + "\n";
  files.set("receipt.json", Buffer.from(receipt));
  if (existsSync(output)) {
    for (const path of filesUnder(output)) {
      if (!files.has(relative(output, path).replaceAll("\\", "/")))
        throw new Error("Unowned output file");
    }
  }
  // Сначала сверяем все существующие файлы; несовпадение не меняет старый output.
  for (const [name, body] of files) {
    const target = join(output, name);
    if (isAbsolute(name) || relative(output, target).startsWith(".."))
      throw new Error("Output escape");
    noLinks(target);
    if (existsSync(target) && !readFileSync(target).equals(body))
      throw new Error("Output drift");
  }
  for (const [name, body] of files) {
    const target = join(output, name);
    mkdirSync(dirname(target), { recursive: true });
    noLinks(target);
    if (!existsSync(target))
      writeFileSync(target, body, { flag: "wx", mode: 0o644 });
  }
  console.log(
    JSON.stringify({
      static_receipt_sha256: sha(receipt),
      network_requests: "none",
    }),
  );
}
try {
  main();
} catch {
  console.error("Статическая конфигурация или output не прошли проверку.");
  process.exitCode = 1;
}
