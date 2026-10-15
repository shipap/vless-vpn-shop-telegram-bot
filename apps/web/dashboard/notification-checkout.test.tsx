import { beforeEach, describe, expect, it, vi } from "vitest";
import { Customer } from "./customer";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Catalog, Dashboard, Session } from "@api/types";

const harness = vi.hoisted(() => ({
  slots: [] as unknown[],
  cursor: 0,
  effects: [] as (() => void)[],
  requests: [] as string[],
  tree: null as unknown,
  quote: {
    id: "fresh-server-quote",
    subscription_id: "own-sub",
    product_id: "published-product",
    action: "renew",
    period_days: 90,
    devices_limit: 7,
    total: "123.45",
    currency: "RUB",
    base_amount: "150.00",
    discount_amount: "26.55",
    bonus_amount: "0.00",
    credit_amount: "0.00",
    resulting_expires_at: "2027-01-01T00:00:00Z",
    expires_at: "2026-10-12T00:00:00Z",
    breakdown: [],
    selected_secondary: { source: "renewal_campaign", percent: "20" },
  },
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: unknown) => {
    const slot = harness.cursor++;
    if (!(slot in harness.slots))
      harness.slots[slot] = typeof initial === "function" ? initial() : initial;
    return [
      harness.slots[slot],
      (value: unknown) => {
        harness.slots[slot] =
          typeof value === "function" ? value(harness.slots[slot]) : value;
      },
    ];
  },
  useEffect: (run: () => void) => {
    harness.effects.push(run);
  },
  useRef: (initial: unknown) => ({ current: initial }),
}));
vi.mock("@ui/instance", () => ({
  useInstance: () => ({ terminology: {}, support_url: null }),
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  api: async (path: string) => {
    harness.requests.push(path);
    return harness.quote;
  },
}));
function node(
  value: unknown,
  name: string,
): { type: unknown; props: Record<string, unknown> } | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const result = node(item, name);
      if (result) return result;
    }
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const item = value as {
    type?: { name?: string };
    props?: Record<string, unknown>;
  };
  if (item.type?.name === name && item.props)
    return { type: item.type, props: item.props };
  return node(item.props?.children, name);
}
async function render(
  sid: string,
  state = "none",
  capacity: number | null = 7,
) {
  const resource = <T,>(data: T) => ({
    data,
    error: null,
    loading: false,
    reload: () => {},
    setData: () => {},
  });
  const account = { id: "9007199254740993123" };
  const props = {
    page: "home",
    session: { account } as Session,
    config: null,
    refreshSession: () => {},
    catalog: resource({
      products: [],
      periods: [],
      legal: { configured: true, documents: [] },
    } as unknown as Catalog),
    dashboard: resource({
      account,
      subscriptions: [
        {
          id: "own-sub",
          name: "Подписка",
          status: "active",
          devices_limit: capacity,
          panel_device_limit: null,
          devices: [],
          segments: [],
          retention_state: state,
        },
      ],
      payments: [],
    } as unknown as Dashboard),
    notificationAction: {
      label: "Продлить",
      target: "/#subscriptions",
      operation: "renew",
      metadata: { subscription_id: sid, quote_id: "saved-quote" },
    },
  } as Parameters<typeof Customer>[0];
  harness.cursor = 0;
  harness.tree = Customer(props);
  for (const effect of harness.effects.splice(0)) effect();
  await Promise.resolve();
  await Promise.resolve();
  harness.cursor = 0;
  harness.tree = Customer(props);
}
beforeEach(() => {
  harness.slots = [];
  harness.effects = [];
  harness.requests = [];
  vi.stubGlobal("location", { search: "" });
});
describe("owned renewal notification checkout", () => {
  it("keeps the selected renewal quote finite when current panel capacity is explicitly unknown", async () => {
    await render("own-sub", "none", null);
    const checkout = node(harness.tree, "Checkout")!;
    harness.slots = [];
    harness.cursor = 0;
    const html = renderToStaticMarkup(
      createElement(
        checkout.type as import("react").ComponentType,
        checkout.props,
      ),
    );
    expect(html).toMatch(/<dt>Устройства<\/dt><dd>7<\/dd>/);
    expect(html).toContain("123,45");
  });
  it("loads the exact server offer and passes it to normal checkout without price/period guesses", async () => {
    await render("own-sub");
    expect(harness.requests).toEqual([
      "/subscriptions/own-sub/renewal-offer?quote_id=saved-quote",
    ]);
    expect(node(harness.tree, "Checkout")?.props.target).toMatchObject({
      action: "renew",
      quote: harness.quote,
    });
  });
  it("displays the authoritative offer period/price and keeps payment behind explicit legal acceptance", async () => {
    await render("own-sub");
    const checkout = node(harness.tree, "Checkout")!;
    harness.slots = [];
    harness.cursor = 0;
    const html = renderToStaticMarkup(
      createElement(
        checkout.type as import("react").ComponentType,
        checkout.props,
      ),
    );
    expect(html).toContain("90 дней");
    expect(html).toContain("123,45");
    expect(html).toContain("Кампания продления");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>[\s\S]*?Подтвердить заказ/,
    );
    expect(harness.requests).toEqual([
      "/subscriptions/own-sub/renewal-offer?quote_id=saved-quote",
    ]);
  });
  for (const [sid, state] of [
    ["foreign-sub", "none"],
    ["own-sub", "review"],
    ["own-sub", "archived"],
  ]) {
    it(`does not start checkout for ${sid}/${state}`, async () => {
      await render(sid!, state!);
      expect(harness.requests).toEqual([]);
      expect(node(harness.tree, "Checkout")).toBeNull();
    });
  }
});
