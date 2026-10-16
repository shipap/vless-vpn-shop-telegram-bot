import { beforeEach, expect, it, vi } from "vitest";
import { Customer } from "../../apps/web/dashboard/customer";
import type { Catalog, Dashboard, Session } from "@api/types";

const state = vi.hoisted(() => ({
  slots: [] as unknown[],
  cursor: 0,
  refs: [] as { current: unknown }[],
  refCursor: 0,
  requests: [] as string[],
  response: null as unknown,
  resolve: null as null | ((value: unknown) => void),
  fail: false,
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useEffect: () => {},
  useRef: (initial: unknown) =>
    (state.refs[state.refCursor++] ??= { current: initial }),
  useState: (initial: unknown) => {
    const index = state.cursor++;
    if (!(index in state.slots))
      state.slots[index] = typeof initial === "function" ? initial() : initial;
    return [
      state.slots[index],
      (value: unknown) => {
        state.slots[index] =
          typeof value === "function" ? value(state.slots[index]) : value;
      },
    ];
  },
}));
vi.mock("@ui/instance", () => ({
  useInstance: () => ({ terminology: {}, support_url: null }),
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  api: async (path: string) => {
    state.requests.push(path);
    if (state.fail) throw new Error("Панель недоступна");
    return new Promise((resolve) => {
      state.resolve = resolve;
    });
  },
}));
const account = { id: "9007199254740993123" };
const subscription = {
  id: "own-sub",
  product_id: "middle",
  name: "Подписка",
  status: "active",
  devices_limit: 4,
  devices: [],
  segments: [],
  retention_state: "none",
  expires_at: "2027-01-01T00:00:00Z",
};
const catalog = {
  products: [
    { id: "low", tier_level: 1, available: true },
    { id: "middle", tier_level: 2, available: true },
    { id: "high", tier_level: 3, available: true },
  ],
  periods: [],
  legal: { configured: true, documents: [] },
} as unknown as Catalog;
let data: Dashboard;
const resource = <T,>(value: T) => ({
  data: value,
  loading: false,
  error: null,
  reload: () => {},
  setData: (next: import("react").SetStateAction<T | null>) => {
    data = (
      typeof next === "function" ? (next as (previous: T) => T)(value) : next
    ) as Dashboard;
  },
});
function render(page = "home") {
  state.cursor = state.refCursor = 0;
  return Customer({
    page,
    session: { account } as Session,
    dashboard: resource(data),
    catalog: resource(catalog),
    config: null,
    refreshSession: () => {},
  });
}
function nodes(value: any): any[] {
  if (Array.isArray(value)) return value.flatMap(nodes);
  return value && typeof value === "object"
    ? [value, ...nodes(value.props?.children)]
    : [];
}
function text(value: any): string {
  if (Array.isArray(value)) return value.map(text).join("");
  if (value && typeof value === "object") return text(value.props?.children);
  return typeof value === "string" ? value : "";
}
function click(tree: unknown, action: string) {
  if (action === "devices")
    return nodes(tree)
      .find((node) => node.type?.name === "CustomerDevices")
      .props.onAdd();
  const label = {
    renew: "Продлить",
    upgrade: "Повысить тариф",
    downgrade: "Понизить тариф",
  }[action];
  return nodes(tree)
    .find((node) => node.type === "button" && text(node).includes(label!))
    .props.onClick();
}
function checkout(tree: unknown) {
  return nodes(tree).find((node) => node.type?.name === "Checkout");
}
beforeEach(() => {
  state.slots = [];
  state.refs = [];
  state.requests = [];
  state.resolve = null;
  state.fail = false;
  data = {
    account,
    subscriptions: [subscription],
    payments: [],
  } as unknown as Dashboard;
  state.response = {
    ...data,
    subscriptions: [
      {
        ...subscription,
        product_id: "high",
        devices_limit: 9,
        expires_at: "2027-03-01T00:00:00Z",
        panel_sync_state: "fresh",
        renewal_pricing_state: "ready",
      },
    ],
  };
});
it.each(["renew", "upgrade", "downgrade", "devices"])(
  "refreshes only the selected subscription before %s choices and waits for current data",
  async (action) => {
    const page = action === "devices" ? "devices" : "home";
    const pending = click(render(page), action);
    expect(state.requests).toEqual([
      "/dashboard?refresh_panel=true&refresh_subscription_id=own-sub",
    ]);
    expect(checkout(render(page))).toBeUndefined();
    expect(
      nodes(render(page)).some((node) => node.props?.role === "status"),
    ).toBe(true);
    await click(render(page), action);
    expect(state.requests).toHaveLength(1);
    state.resolve!(state.response);
    await pending;
    expect(checkout(render(page))?.props.target.subscription).toMatchObject({
      product_id: "high",
      devices_limit: 9,
      expires_at: "2027-03-01T00:00:00Z",
    });
    expect(state.requests).toHaveLength(1);
  },
);
it.each(["network", "missing", "foreign", "stale"])(
  "keeps checkout closed after %s refresh failure",
  async (failure) => {
    state.fail = failure === "network";
    const pending = click(render(), "renew");
    expect(state.requests).toEqual([
      "/dashboard?refresh_panel=true&refresh_subscription_id=own-sub",
    ]);
    if (!state.fail)
      state.resolve!(
        failure === "missing"
          ? { ...data, subscriptions: [] }
          : failure === "foreign"
            ? { ...(state.response as object), account: { id: "other" } }
            : {
                ...data,
                subscriptions: [
                  {
                    ...subscription,
                    renewal_pricing_state: "blocked",
                    panel_sync_state: "stale",
                  },
                ],
              },
      );
    await pending;
    expect(checkout(render())).toBeUndefined();
    expect(
      nodes(render()).some((node) => node.type?.name === "ErrorState"),
    ).toBe(true);
  },
);
