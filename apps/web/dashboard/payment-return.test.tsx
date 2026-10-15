import { beforeEach, expect, it, vi } from "vitest";
import { Customer, Checkout } from "./customer";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const state = vi.hoisted(() => ({
  slots: [] as unknown[],
  cursor: 0,
  effects: [] as (() => void)[],
  requests: [] as string[],
  failure: false,
  order: {
    id: "own-order",
    status: "awaiting_payment",
    total: "12.34",
    currency: "RUB",
    subscription_id: null,
    checkout_url: null,
  },
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
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
  useEffect: (run: () => void) => state.effects.push(run),
  useRef: (initial: unknown) => ({ current: initial }),
}));
vi.mock("@ui/instance", () => ({
  useInstance: () => ({ terminology: {}, support_url: null }),
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  api: async (path: string) => {
    state.requests.push(path);
    if (state.failure) throw new Error("Заказ не найден.");
    return state.order;
  },
}));
const resource = (data: unknown) => ({
  data,
  error: null,
  loading: false,
  reload: () => {},
  setData: () => {},
});
function props() {
  const account = { id: "9007199254740993" };
  return {
    page: "payments",
    session: { account },
    config: null,
    refreshSession: () => {},
    dashboard: resource({ account, subscriptions: [], payments: [] }),
    catalog: resource({
      products: [],
      periods: [],
      legal: { configured: true, documents: [] },
    }),
  } as unknown as Parameters<typeof Customer>[0];
}
function node(tree: any, name: string): any {
  if (Array.isArray(tree))
    return tree.map((item) => node(item, name)).find(Boolean);
  if (!tree || typeof tree !== "object") return null;
  return tree.type?.name === name ? tree : node(tree.props?.children, name);
}
async function recover() {
  state.cursor = 0;
  Customer(props());
  for (const effect of state.effects.splice(0)) effect();
  await Promise.resolve();
  await Promise.resolve();
  state.cursor = 0;
  return Customer(props());
}
beforeEach(() => {
  state.slots = [];
  state.effects = [];
  state.requests = [];
  state.failure = false;
  vi.stubGlobal("location", { search: "?payment_order=own-order&status=paid" });
});
it("recovers the owned pending order and ignores a forged paid query", async () => {
  const tree = await recover();
  expect(state.requests).toEqual(["/orders/own-order"]);
  const checkout = node(tree, "Checkout");
  expect(checkout?.props.target.order).toEqual(state.order);
  state.slots = [];
  state.cursor = 0;
  const html = renderToStaticMarkup(createElement(Checkout, checkout.props));
  expect(html).toContain("Статус заказа");
  expect(html).toContain("12,34");
  expect(html).toContain("сам по себе не подтверждает платёж");
  expect(html).not.toContain("Подтвердить заказ");
});
it("shows an ownership/read failure without reopening checkout or creating an order", async () => {
  state.failure = true;
  const tree = await recover();
  expect(state.requests).toEqual(["/orders/own-order"]);
  expect(node(tree, "Checkout")).toBeFalsy();
  expect(node(tree, "ErrorState")?.props.error.message).toBe(
    "Заказ не найден.",
  );
});
