import { afterEach, describe, expect, it, vi } from "vitest";
import { api, setCsrf } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf(null);
});
describe("protected broadcast binary transport", () => {
  it.each(["image/jpeg", "image/png", "video/mp4"])(
    "sends exact %s bytes with the existing CSRF/session/signal",
    async (type) => {
      const rawBody = new Blob([new Uint8Array([0, 255, 1])], { type });
      const controller = new AbortController();
      setCsrf("synthetic-csrf");
      const fetcher = vi.fn(async (_url: string, init: RequestInit) => {
        expect(init.body).toBe(rawBody);
        expect(init.signal).toBe(controller.signal);
        expect(init.credentials).toBe("same-origin");
        expect(init.redirect).toBe("error");
        expect(new Headers(init.headers).get("Content-Type")).toBe(type);
        expect(new Headers(init.headers).get("X-CSRF-Token")).toBe(
          "synthetic-csrf",
        );
        return Response.json({ asset_id: "synthetic" });
      });
      vi.stubGlobal("fetch", fetcher);
      await api("/admin/broadcasts/media", {
        method: "POST",
        rawBody,
        signal: controller.signal,
      });
      expect(fetcher.mock.calls[0]?.[0]).toBe("/api/v1/admin/broadcasts/media");
    },
  );
  it.each([
    { method: "POST", rawBody: new Blob(["x"], { type: "text/html" }) },
    {
      method: "POST",
      rawBody: new Blob(["x"], { type: "image/png" }),
      body: {},
    },
    { method: "GET", rawBody: new Blob(["x"], { type: "image/png" }) },
    {
      method: "POST",
      rawBody: new Blob(["x"], { type: "image/png" }),
      public: true,
    },
  ])("denies invalid raw options before fetch", async (options) => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(api("/admin/broadcasts/media", options)).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("does not offer binary transport to another endpoint", async () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    await expect(
      api("/orders", {
        method: "POST",
        rawBody: new Blob(["x"], { type: "image/png" }),
      }),
    ).rejects.toThrow();
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("retains cancellation and fresh MFA error handling", async () => {
    const rawBody = new Blob(["x"], { type: "image/png" });
    const dispatchEvent = vi.fn();
    vi.stubGlobal("window", { dispatchEvent });
    vi.stubGlobal("fetch", async () =>
      Response.json(
        { error: { code: "mfa_required", message: "Подтвердите вход" } },
        { status: 403 },
      ),
    );
    await expect(
      api("/admin/broadcasts/media", { method: "POST", rawBody }),
    ).rejects.toMatchObject({ status: 403, code: "mfa_required" });
    expect(dispatchEvent.mock.calls[0]?.[0].type).toBe("sunrise:mfa-required");
    const aborted = new DOMException("cancelled", "AbortError");
    vi.stubGlobal("fetch", async () => {
      throw aborted;
    });
    await expect(
      api("/admin/broadcasts/media", { method: "POST", rawBody }),
    ).rejects.toBe(aborted);
  });
});
