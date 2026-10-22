import { beforeEach, expect, it, vi } from "vitest";
import { Customer } from "./customer";
import type { Dashboard, Catalog, Session } from "@api/types";
const h = vi.hoisted(() => ({
  effects: [] as (() => unknown)[],
  page: "home",
  slots: [] as unknown[],
  cursor: 0,
  requests: [] as string[],
  fresh: null as unknown,
  local: null as unknown,
  values: {} as Record<string, unknown>,
  bodies: [] as unknown[],
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useEffect: (run: () => unknown) => {
    h.effects.push(run);
  },
  useRef: (initial: unknown) => ({ current: initial }),
  useState: (initial: unknown) => {
    const i = h.cursor++;
    if (!(i in h.slots))
      h.slots[i] = typeof initial === "function" ? initial() : initial;
    return [
      h.slots[i],
      (v: unknown) => {
        h.slots[i] = v;
      },
    ];
  },
}));
vi.mock("@ui/instance", () => ({ useInstance: () => ({ terminology: {} }) }));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  api: async (path: string, options: unknown) => {
    h.requests.push(path);
    h.bodies.push(options);
    return h.fresh;
  },
}));
const account = { id: "9007199254740993123" };
const sub = {
  id: "own-sub",
  name: "Подписка",
  product_id: "standard",
  status: "active",
  devices_limit: 4,
  devices: [],
  segments: [],
  retention_state: "none",
};
const resource = <T,>(data: T) => ({
  data,
  error: null,
  loading: false,
  reload: () => {},
  setData: () => {},
});
function render() {
  h.cursor = 0;
  return Customer({
    page: h.page,
    session: { account } as Session,
    config: null,
    refreshSession: () => {},
    dashboard: {
      ...resource(
        (h.local || {
          account,
          subscriptions: [{ ...sub, ...h.values }],
          payments: [],
        }) as Dashboard,
      ),
      setData: (value: unknown) => {
        h.local = value;
      },
    },
    catalog: resource({
      products: [],
      periods: [],
      legal: { configured: false, documents: [] },
    } as unknown as Catalog),
  });
}
function find(tree: any, predicate: (node: any) => boolean): any {
  if (Array.isArray(tree))
    return tree.map((n) => find(n, predicate)).find(Boolean);
  if (!tree || typeof tree !== "object") return null;
  return predicate(tree) ? tree : find(tree.props?.children, predicate);
}
beforeEach(() => {
  h.slots = [];
  h.effects = [];
  h.page = "home";
  h.requests = [];
  h.bodies = [];
  h.local = null;
  h.values = {};
  h.fresh = { account, subscriptions: [sub] };
  vi.stubGlobal("location", { search: "" });
});
it.each([
  ["panel_capacity_unpriced", "Лимит устройств требует проверки"],
  ["panel_product_unverified", "Тариф панели требует проверки"],
  ["panel_access_restricted", "Доступ к подписке ограничен"],
])(
  "refreshes owned target and keeps precise %s without stale checkout",
  async (code, message) => {
    h.fresh = {
      account,
      subscriptions: [
        {
          ...sub,
          renewal_pricing_state: "blocked",
          renewal_pricing_error: code,
        },
      ],
    };
    const entry = find(render(), (n) => n.props?.className === "quick-actions");
    await entry.props.children[0].props.onClick();
    expect(h.requests).toEqual([
      "/dashboard?refresh_panel=true&refresh_subscription_id=own-sub",
    ]);
    const tree = render();
    expect(find(tree, (n) => n.type?.name === "Checkout")).toBeFalsy();
    expect(
      find(tree, (n) => n.type?.name === "ErrorState").props.error.message,
    ).toContain(message);
    expect(
      find(tree, (n) => n.props?.className === "quick-actions"),
    ).toBeTruthy();
  },
);
it("permits only server-authorized finite renewal despite damaged historical capacity", async () => {
  h.fresh = {
    account,
    subscriptions: [
      {
        ...sub,
        status: "expired",
        devices_limit: null,
        renewal_pricing_state: "blocked",
        renewal_pricing_error: "panel_capacity_unpriced",
        renewal_device_limit_required: true,
      },
    ],
  };
  await find(
    render(),
    (n) => n.props?.className === "quick-actions",
  ).props.children[0].props.onClick();
  expect(
    find(render(), (n) => n.type?.name === "Checkout").props.target.subscription
      .renewal_device_limit_required,
  ).toBe(true);
});

it("cancels only the visible exact never-started rotation after confirmation", async () => {
  h.values = {
    credential_rotation_cancelable: true,
    credential_rotation_job_id: "own-job",
  };
  const cancel = find(
    render(),
    (n) => n.props?.children === "Отменить обновление ссылки",
  );
  expect(cancel).toBeTruthy();
  cancel.props.onClick();
  expect(h.requests).toEqual([]);
  const confirm = find(render(), (n) => n.props?.children === "Подтвердить");
  await confirm.props.onClick();
  expect(h.requests).toEqual([
    "/subscriptions/own-sub/credentials/rotation/cancel",
  ]);
  expect(h.bodies[0]).toMatchObject({
    method: "POST",
    body: {
      job_id: "own-job",
      confirmed: true,
      idempotency_key: expect.any(String),
    },
  });
});

it("refreshes selected own subscription on devices menu entry", async () => {
  h.page = "devices";
  render();
  for (const effect of h.effects.splice(0)) effect();
  await Promise.resolve();
  expect(h.requests).toEqual([
    "/dashboard?refresh_panel=true&refresh_subscription_id=own-sub",
  ]);
});

it("does not submit cancellation when the displayed rotation job has changed", async () => {
  h.values = {
    credential_rotation_cancelable: true,
    credential_rotation_job_id: "own-job",
  };
  find(
    render(),
    (n) => n.props?.children === "Отменить обновление ссылки",
  ).props.onClick();
  h.values = {
    credential_rotation_cancelable: true,
    credential_rotation_job_id: "new-job",
  };
  await find(
    render(),
    (n) => n.props?.children === "Подтвердить",
  ).props.onClick();
  expect(h.requests).toEqual([]);
  expect(
    find(render(), (n) => n.type?.name === "ErrorState").props.error.message,
  ).toContain("Состояние обновления ссылки изменилось");
});
it("does not offer cancellation without positive server eligibility", () => {
  h.values = {
    credential_rotation_state: "review",
    credential_rotation_job_id: "unknown-job",
    credential_rotation_cancelable: false,
  };
  expect(
    find(render(), (n) => n.props?.children === "Отменить обновление ссылки"),
  ).toBeFalsy();
});
