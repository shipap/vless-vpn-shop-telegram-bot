import { expect, it } from "vitest";
import { webConfig } from "../../apps/web/vite.shared";
import type { Plugin, UserConfig } from "vite";

it("marketing middleware permits only exact public campaign visit POST and strips all auth headers", async () => {
  type Request = {
    url: string;
    method: string;
    headers: Record<string, string>;
  };
  type Response = {
    statusCode: number;
    setHeader: () => void;
    end: (value: string) => void;
  };
  type Middleware = (
    req: Request,
    res: Response,
    next: () => void,
  ) => Promise<void>;
  const config = webConfig(true) as UserConfig;
  const plugin = config.plugins?.find(
    (p) =>
      p && !Array.isArray(p) && (p as Plugin).name === "sunrise-private-routes",
  ) as Plugin;
  let middleware: Middleware;
  (
    plugin.configureServer as unknown as (server: {
      middlewares: { use: (fn: Middleware) => void };
    }) => void
  )({
    middlewares: {
      use: (fn) => {
        middleware = fn;
      },
    },
  });
  for (const [path, method, allowed] of [
    ["/api/v1/campaigns/visit", "POST", true],
    ["/api/v1/campaigns/visit", "GET", false],
    ["/api/v1/campaigns/attach", "POST", false],
    ["/api/v1/auth/telegram/miniapp", "POST", false],
    ["/api/v1/campaigns/visit/extra", "POST", false],
    ["/api/v1/catalog", "GET", true],
    ["/api/v1/instance/assets/hero", "GET", true],
    ["/api/v1/instance/assets/hero", "POST", false],
    ["/api/v1/instance/assets/unknown", "GET", false],
    ["/api/v1/instance/assets/hero/extra", "GET", false],
    ["/instance-assets/private/hero.png", "GET", false],
    ["/%69nstance-assets/private/hero.png", "GET", false],
    ["/@fs/C:/workspace/instance-assets/private/hero.png", "GET", false],
    ["/@fs/C:/workspace/instance-assets-private/hero.png", "GET", false],
    ["/@fs/C:/workspace/assets/brand/source/mascot.png", "GET", false],
    ["/@fs/C:/workspace/apps/web/dashboard/main.tsx", "GET", true],
  ] as const) {
    const req = {
      url: path,
      method,
      headers: {
        cookie: "synthetic-cookie",
        authorization: "synthetic-authorization",
        "x-csrf-token": "synthetic-csrf",
      },
    };
    let forwarded = false;
    const res = { statusCode: 200, setHeader: () => {}, end: () => {} };
    await middleware!(req, res, () => {
      forwarded = true;
    });
    expect(forwarded, `${method} ${path}`).toBe(allowed);
    if (path.startsWith("/api/")) expect(req.headers).toEqual({});
    if (!allowed) expect(res.statusCode).toBe(404);
  }
});
