import { afterEach, describe, expect, it, vi } from "vitest";
import { api, setCsrf } from "./client";
afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf(null);
});
describe("Same-origin API transport", () => {
  it("keeps BIGINT strings and sends CSRF for mutation", async () => {
    setCsrf("synthetic-csrf");
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      expect(url).toBe("/api/v1/orders");
      expect(new Headers(init.headers).get("X-CSRF-Token")).toBe(
        "synthetic-csrf",
      );
      expect(init.credentials).toBe("same-origin");
      expect(JSON.parse(init.body as string).quote_id).toBe(
        "900719925474099999",
      );
      return new Response(JSON.stringify({ id: "900719925474099999" }), {
        status: 200,
      });
    });
    expect(
      await api("/orders", {
        method: "POST",
        body: { quote_id: "900719925474099999" },
      }),
    ).toEqual({ id: "900719925474099999" });
  });
  it("rejects external or path-traversal destinations before fetch", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(api("https://evil.example")).rejects.toThrow();
    await expect(api("/../external")).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("shows a safe fallback for non-JSON errors, never server trace", async () => {
    vi.stubGlobal(
      "fetch",
      async () => new Response("private database trace", { status: 500 }),
    );
    await expect(api("/dashboard")).rejects.toThrow("Не удалось");
  });
});
