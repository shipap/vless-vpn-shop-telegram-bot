import { beforeEach, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Checkout } from "./customer";

const state = vi.hoisted(() => ({
  slots: [] as unknown[],
  refs: [] as { current: unknown }[],
  cursor: 0,
  refCursor: 0,
  requests: [] as { path: string; body: any }[],
  failQuote: false,
  quoteOverrides: {} as Record<string, unknown>,
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
  useRef: (initial: unknown) =>
    state.refs[state.refCursor++] ??
    (state.refs[state.refCursor - 1] = { current: initial }),
  useEffect: () => {},
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  api: async (path: string, options: any) => {
    state.requests.push({ path, body: options?.body });
    if (path === "/quotes") {
      if (state.failQuote) throw new Error("Расчёт временно недоступен");
      return {
        ...quote,
        payment_method:
          methods.find((method) => method.id === options.body.payment_method) ??
          null,
        ...state.quoteOverrides,
      };
    }
    return {};
  },
}));
vi.mock("@ui/instance", () => ({
  useInstance: () => ({ terminology: {}, support_url: null }),
}));
const methods = [
  {
    id: "crypto",
    label: "Криптовалюта",
    discount_percent: "7.50",
    available: true,
    unavailable_reason: null,
  },
  {
    id: "xrocket",
    label: "xRocket",
    discount_percent: "3",
    available: false,
    unavailable_reason: "Временно недоступен <script>",
  },
  {
    id: "sbp",
    label: "СБП",
    discount_percent: "0",
    available: true,
    unavailable_reason: null,
  },
];
const quote = {
  id: "q",
  product_id: "p",
  period_days: 90,
  action: "purchase",
  total: "183.17",
  currency: "RUB",
  base_amount: "200.00",
  discount_amount: "16.83",
  bonus_amount: "0.00",
  credit_amount: "0.00",
  devices_limit: 2,
  resulting_expires_at: "2027-01-01T00:00:00Z",
  expires_at: "2026-11-01T00:00:00Z",
  breakdown: [],
  payment_method: null,
  payment_method_discount_amount: "16.83",
  minimum_external_required: false,
};
const catalog = {
  products: [
    {
      id: "p",
      name: "Тариф",
      available: true,
      currency: "RUB",
      periods: [{ days: 90 }],
    },
  ],
  periods: [{ days: 90 }],
  payment_methods: methods,
  legal: { configured: true, documents: [], revision: "1" },
};
function render(
  saved = false,
  paymentMethods: unknown = methods,
  wallet: Record<string, unknown> = {},
  products: unknown = catalog.products,
) {
  state.cursor = state.refCursor = 0;
  return Checkout({
    target: { action: "purchase", ...(saved ? { quote } : {}) },
    catalog: {
      ...catalog,
      products,
      payment_methods: paymentMethods === "legacy" ? undefined : paymentMethods,
    },
    wallet: { bonus_balance: "0.00", ...wallet },
    onClose: () => {},
    onComplete: () => {},
    onLegalStale: () => {},
  } as unknown as Parameters<typeof Checkout>[0]);
}
function find(tree: any, predicate: (node: any) => boolean): any {
  if (Array.isArray(tree))
    return tree.map((item) => find(item, predicate)).find(Boolean) ?? null;
  if (!tree || typeof tree !== "object") return null;
  if (predicate(tree)) return tree;
  return find(tree.props?.children, predicate);
}
beforeEach(() => {
  state.slots = [];
  state.refs = [];
  state.requests = [];
  state.failQuote = false;
  state.quoteOverrides = {};
  vi.stubGlobal("location", { search: "" });
});

it("shows selected catalog description and configured months, days and discounts without fixed periods", () => {
  const products = [
    {
      ...catalog.products[0],
      description: "Первая строка\nУстройства для всей семьи",
      content: { description: [] },
      periods: [
        { days: 31, months: 1, discount_percent: "0", price: "123.45" },
        { days: 93, months: 3, discount_percent: "10", price: "321.09" },
        { days: 186, months: 6, discount_percent: "15", price: "654.32" },
        { days: 365, months: 12, discount_percent: "20", price: "987.65" },
        { days: 210, months: 7, discount_percent: "12.5" },
      ],
    },
    {
      ...catalog.products[0],
      id: "other",
      name: "Другой тариф",
      description: "Резервное описание",
      content: {
        description: [
          {
            type: "strong",
            children: [{ type: "text", text: "Описание другого тарифа" }],
          },
        ],
      },
      currency: "EUR",
      periods: [
        { days: 45, months: 2, discount_percent: "8.75", price: "47.89" },
      ],
    },
  ];
  let tree = render(false, methods, {}, products);
  const html = renderToStaticMarkup(tree);
  expect(html).toContain("Первая строка\nУстройства для всей семьи");
  for (const label of [
    "1 месяц (31 дн.)",
    "3 месяца (93 дн.) −10%",
    "6 месяцев (186 дн.) −15%",
    "12 месяцев (365 дн.) −20%",
    "7 месяцев (210 дн.) −12,5%",
  ])
    expect(html).toContain(label);
  for (const price of ["123,45 ₽", "321,09 ₽", "654,32 ₽", "987,65 ₽"])
    expect(html).toContain(price);
  find(
    tree,
    (node) => node.type === "select" && node.props.value === "p",
  ).props.onChange({ target: { value: "other" } });
  tree = render(false, methods, {}, products);
  const other = renderToStaticMarkup(tree);
  expect(other).toContain("<strong>Описание другого тарифа</strong>");
  expect(other).toContain("2 месяца (45 дн.) −8,75%");
  expect(other).toContain("47,89 EUR");
  expect(other).not.toContain("123,45 ₽");
  expect(other).not.toContain("Устройства для всей семьи");
  expect(other).not.toContain("12 месяцев (365 дн.) −20%");
});

const cashWallet = {
  credit_currency: "RUB",
  credit_balance: "99.00",
  credit_available: "99.00",
  credit_reserved: "0.00",
};
async function requestCashQuote(paymentMethods: unknown) {
  let tree = render(false, paymentMethods, cashWallet);
  find(
    tree,
    (node) => node.type === "input" && node.props.type === "checkbox",
  ).props.onChange({ target: { checked: true } });
  tree = render(false, paymentMethods, cashWallet);
  find(
    tree,
    (node) => node.type === "input" && node.props.inputMode === "decimal",
  ).props.onChange({ target: { value: "99.00" } });
  find(tree, (node) => node.type?.name === "LegalCheckbox").props.onChange(
    true,
  );
  tree = render(false, paymentMethods, cashWallet);
  await find(tree, (node) => node.type === "form").props.onSubmit({
    preventDefault() {},
  });
}
for (const unavailable of [
  [],
  methods.map((method) => ({ ...method, available: false })),
]) {
  it("permits an explicit internal cash request when external methods are unavailable, confirms only server zero total", async () => {
    state.quoteOverrides = {
      base_amount: "99.00",
      discount_amount: "0.00",
      payment_method_discount_amount: "0.00",
      total: "0.00",
      credit_amount: "99.00",
      minimum_external_required: false,
      payment_method: null,
    };
    await requestCashQuote(unavailable);
    const request = state.requests.find(
      (request) => request.path === "/quotes",
    );
    expect(request?.body.payment_method).toBeNull();
    expect(request?.body.credit_amount).toBe("99.00");
    const tree = render(false, unavailable, cashWallet);
    const confirm = find(
      tree,
      (node) => node.props.onClick?.name === "createOrder",
    );
    expect(confirm.props.disabled).toBe(false);
    await confirm.props.onClick();
    expect(state.requests.at(-1)?.path).toBe("/orders");
    expect(state.requests.at(-1)?.body).toEqual({
      quote_id: "q",
      idempotency_key: expect.any(String),
    });
  });
}
for (const response of [
  { total: "1.00", credit_amount: "98.00", minimum_external_required: false },
  { total: "0.00", credit_amount: "99.00", minimum_external_required: true },
  { total: "0.00", credit_amount: "0.00", minimum_external_required: false },
]) {
  it("keeps positive/mixed or externally required quotes behind an available selected payment method", async () => {
    state.quoteOverrides = { ...response, payment_method: null };
    await requestCashQuote([]);
    expect(
      state.requests.find((request) => request.path === "/quotes"),
    ).toBeDefined();
    const tree = render(false, [], cashWallet);
    const confirm = find(
      tree,
      (node) => node.props.onClick?.name === "createOrder",
    );
    expect(confirm.props.disabled).toBe(true);
    await confirm.props.onClick();
    expect(state.requests.some((request) => request.path === "/orders")).toBe(
      false,
    );
  });
}
it("does not infer full cash coverage after a quote error or without an explicit positive request", async () => {
  state.failQuote = true;
  await requestCashQuote([]);
  const tree = render(false, [], cashWallet);
  expect(
    state.requests.find((request) => request.path === "/quotes"),
  ).toBeDefined();
  expect(
    find(tree, (node) => node.props.onClick?.name === "createOrder"),
  ).toBeNull();
});
it("renders DB labels/percentages and safely explains disabled methods", () => {
  const html = renderToStaticMarkup(render());
  expect(html).toContain("Криптовалюта (−7,50%)");
  expect(html).toContain("Временно недоступен &lt;script&gt;");
  expect(html).toMatch(
    /disabled=""[^>]*value="xrocket"|value="xrocket"[^>]*disabled=""/,
  );
});
it("requires explicit method, requests fresh backend total and binds order to quote only", async () => {
  let tree = render();
  const chooser = find(
    tree,
    (node) => node.type?.name === "PaymentMethodChooser",
  );
  expect(chooser).not.toBeNull();
  find(tree, (node) => node.type?.name === "LegalCheckbox").props.onChange(
    true,
  );
  tree = render();
  await find(tree, (node) => node.type === "form").props.onSubmit({
    preventDefault() {},
  });
  expect(state.requests).toEqual([]);
  chooser.props.onChange("crypto");
  tree = render();
  await find(tree, (node) => node.type === "form").props.onSubmit({
    preventDefault() {},
  });
  const request = state.requests.find((request) => request.path === "/quotes")!;
  expect(request.body.payment_method).toBe("crypto");
  expect(request.body).not.toHaveProperty("discount_percent");
  expect(request.body).not.toHaveProperty("total");
  tree = render();
  expect(renderToStaticMarkup(tree)).toContain("183,17");
  const confirm = find(
    tree,
    (node) =>
      node.type?.name === "Button" &&
      node.props.onClick?.name === "createOrder",
  );
  await confirm.props.onClick();
  expect(state.requests.at(-1)?.body).toEqual({
    quote_id: "q",
    idempotency_key: expect.any(String),
  });
});
it("blocks a saved offer without method until a fresh selected-method quote", () => {
  const tree = render(true);
  expect(
    find(tree, (node) => node.type?.name === "PaymentMethodChooser"),
  ).not.toBeNull();
  expect(
    find(
      tree,
      (node) =>
        node.type?.name === "Button" &&
        node.props.onClick?.name === "createOrder",
    ).props.disabled,
  ).toBe(true);
});
it("selecting a saved offer method re-quotes server terms without implicitly accepting legal documents", async () => {
  const tree = render(true);
  await find(
    tree,
    (node) => node.type?.name === "PaymentMethodChooser",
  ).props.onChange("sbp");
  expect(state.requests).toEqual([
    {
      path: "/quotes",
      body: {
        product_id: "p",
        period_days: 90,
        action: "purchase",
        subscription_id: undefined,
        bonus_amount: "0.00",
        credit_amount: "0.00",
        extra_devices: 0,
        payment_method: "sbp",
      },
    },
  ]);
  const fresh = render(true);
  expect(
    find(fresh, (node) => node.type?.name === "PaymentMethodChooser").props
      .value,
  ).toBe("sbp");
  expect(
    find(
      fresh,
      (node) =>
        node.type?.name === "Button" &&
        node.props.onClick?.name === "createOrder",
    ).props.disabled,
  ).toBe(true);
});
it("discards old quote when changing method fails and rejects disabled method", async () => {
  let tree = render(true);
  const chooser = find(
    tree,
    (node) => node.type?.name === "PaymentMethodChooser",
  );
  await chooser.props.onChange("xrocket");
  expect(state.requests).toEqual([]);
  state.failQuote = true;
  await chooser.props.onChange("crypto");
  tree = render(true);
  expect(
    find(tree, (node) => node.props.onClick?.name === "createOrder"),
  ).toBeNull();
  expect(find(tree, (node) => node.type?.name === "ErrorState")).not.toBeNull();
});
it("empty admitted catalog blocks checkout while absent legacy field preserves isolated behavior", async () => {
  let tree = render(false, []);
  find(tree, (node) => node.type?.name === "LegalCheckbox").props.onChange(
    true,
  );
  tree = render(false, []);
  await find(tree, (node) => node.type === "form").props.onSubmit({
    preventDefault() {},
  });
  expect(state.requests).toEqual([]);
  expect(renderToStaticMarkup(tree)).toContain(
    "Способы оплаты временно недоступны",
  );
  state.slots = [];
  state.refs = [];
  tree = render(false, "legacy");
  find(tree, (node) => node.type?.name === "LegalCheckbox").props.onChange(
    true,
  );
  tree = render(false, "legacy");
  await find(tree, (node) => node.type === "form").props.onSubmit({
    preventDefault() {},
  });
  expect(
    state.requests.find((request) => request.path === "/quotes")?.body,
  ).not.toHaveProperty("payment_method");
});
