import { expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { notificationTarget } from "./notification-target";
it("push worker has no fetch cache handlers and confines all clicks to same-origin safe internal targets", async () => {
  const handlers = new Map<string, (event: unknown) => void>();
  const show = vi.fn(async (_title: string, _options: unknown) => {});
  const navigate = vi.fn(async () => {});
  const focus = vi.fn(async () => {});
  const openWindow = vi.fn(async () => {});
  const self = {
    location: { origin: "https://dashboard.example.test" },
    addEventListener: (name: string, handler: (event: unknown) => void) =>
      handlers.set(name, handler),
    registration: { showNotification: show },
    clients: {
      matchAll: async () => [
        {
          url: "https://foreign.example.test/",
          navigate: vi.fn(),
          focus: vi.fn(),
        },
        { url: "https://dashboard.example.test/", navigate, focus },
      ],
      openWindow,
    },
  };
  runInNewContext(
    readFileSync(
      new URL("../../apps/web/public/sw.js", import.meta.url),
      "utf8",
    ),
    { self, URL },
  );
  expect([...handlers.keys()]).toEqual(["push", "notificationclick"]);
  let pending = Promise.resolve();
  handlers.get("push")!({
    data: {
      json: () => ({
        title: "<b>Plain text</b>".repeat(20),
        body: "x".repeat(800),
        target: "https://foreign.example.test/",
      }),
    },
    waitUntil: (p: Promise<void>) => {
      pending = p;
    },
  });
  await pending;
  expect(show.mock.calls[0]?.[0]).toHaveLength(120);
  expect(show.mock.calls[0]?.[1]).toMatchObject({
    body: "x".repeat(512),
    data: { url: "/#home" },
  });
  for (const url of [
    "https://foreign.example.test/",
    "//foreign.example.test",
    "/#account",
    "/admin#users",
    "/?token=synthetic",
    "/%2f%2fforeign",
    "/?tab=subscriptions",
    "/#history",
    "/#notifications",
    "/#subscriptions",
  ]) {
    handlers.get("notificationclick")!({
      notification: { data: { url }, close: () => {} },
      waitUntil: (p: Promise<void>) => {
        pending = p;
      },
    });
    await pending;
    expect(navigate).toHaveBeenLastCalledWith(
      `https://dashboard.example.test${notificationTarget(url) || "/#home"}`,
    );
  }
  expect(openWindow).not.toHaveBeenCalled();
  expect(focus).toHaveBeenCalledTimes(10);
});

it("maps only exact historical customer notification routes", () => {
  expect(notificationTarget("/?tab=subscriptions")).toBe("/#devices");
  expect(notificationTarget("/#history")).toBe("/#payments");
  expect(notificationTarget("/#subscriptions")).toBe("/#home");
  expect(notificationTarget("/admin#history")).toBe("/admin#history");
  expect(notificationTarget("/#notifications")).toBe("/#notifications");
  for (const target of [
    "/?tab=subscriptions&token=x",
    "/#unknown",
    "https://example.test/#history",
  ])
    expect(notificationTarget(target)).toBeUndefined();
});
