import { beforeEach, describe, expect, it, vi } from "vitest";

const harness = vi.hoisted(() => ({
  app: null as null | (() => unknown),
  slots: [] as unknown[],
  setters: [] as ((value: unknown) => void)[],
  cursor: 0,
  effects: [] as {
    run: () => void | (() => void);
    deps?: unknown[];
    cleanup?: () => void;
  }[],
  effectCursor: 0,
  pending: [] as (() => void)[],
  requests: [] as string[],
  reloads: [] as string[],
  setDashboard: vi.fn(),
  events: new Map<string, (() => void)[]>(),
  tree: null as unknown,
  offer: null as unknown,
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useCallback: (fn: unknown) => fn,
  useRef: (initial: unknown) => {
    const slot = harness.cursor++;
    if (!(slot in harness.slots)) harness.slots[slot] = { current: initial };
    return harness.slots[slot];
  },
  useState: (initial: unknown) => {
    const slot = harness.cursor++;
    if (!(slot in harness.slots))
      harness.slots[slot] = typeof initial === "function" ? initial() : initial;
    harness.setters[slot] ??= (value: unknown) => {
      harness.slots[slot] =
        typeof value === "function" ? value(harness.slots[slot]) : value;
    };
    return [harness.slots[slot], harness.setters[slot]];
  },
  useEffect: (run: () => void | (() => void), deps?: unknown[]) => {
    const slot = harness.effectCursor++;
    const previous = harness.effects[slot];
    if (
      !previous ||
      !deps ||
      deps.some((item, i) => item !== previous.deps?.[i])
    ) {
      harness.pending.push(() => {
        previous?.cleanup?.();
        const cleanup = run();
        harness.effects[slot] = {
          run,
          deps,
          cleanup: typeof cleanup === "function" ? cleanup : undefined,
        };
      });
    }
  },
}));
vi.mock("react-dom/client", () => ({
  createRoot: () => ({
    render: (element: { props: { children: { type: () => unknown } } }) => {
      harness.app = element.props.children.type;
    },
  }),
}));
vi.mock("@ui/instance", () => ({
  InstanceProvider: () => null,
  useInstance: () => ({
    app_name: "Тест",
    terminology: {},
    public_site_url: null,
  }),
}));
vi.mock("@ui/telegram", () => ({
  useTelegramWebApp: () => ({ activation: 0 }),
}));
vi.mock("@ui/campaign-hooks", () => ({ useAttachCampaign: () => {} }));
const resources = vi.hoisted(() => {
  const resource = (path: string, data: unknown) => ({
    data,
    error: null,
    loading: false,
    reload: () => harness.reloads.push(path),
    setData: path === "/dashboard" ? harness.setDashboard : vi.fn(),
  });
  return {
    "/auth/me": resource("/auth/me", {
      account: { id: "9007199254740993123", email_verified: true },
      csrf_token: "test",
      is_admin: false,
    }),
    "/dashboard": resource("/dashboard", {
      account: { id: "9007199254740993123" },
      subscriptions: [],
      payments: [],
    }),
    "/catalog": resource("/catalog", {
      legal: { configured: false, documents: [] },
    }),
    "/public-config": resource("/public-config", {}),
  };
});
vi.mock("@api/client", () => ({
  ApiError: class extends Error {},
  setCsrf: () => {},
  useApi: (path: keyof typeof resources) => resources[path],
  api: async (path: string) => {
    harness.requests.push(path);
    return path.includes("renewal-offer")
      ? harness.offer
      : resources["/dashboard"].data;
  },
}));

function render() {
  harness.cursor = 0;
  harness.effectCursor = 0;
  harness.tree = harness.app!();
  for (const run of harness.pending.splice(0)) run();
}
function dispatch(event: string) {
  for (const listener of harness.events.get(event) ?? []) listener();
}
beforeEach(async () => {
  for (const effect of harness.effects) effect.cleanup?.();
  harness.slots = [];
  harness.setters = [];
  harness.effects = [];
  harness.requests = [];
  harness.reloads = [];
  harness.events.clear();
  harness.tree = null;
  harness.offer = null;
  const eventTarget = {
    addEventListener: (event: string, fn: () => void) =>
      harness.events.set(event, [...(harness.events.get(event) ?? []), fn]),
    removeEventListener: (event: string, fn: () => void) =>
      harness.events.set(
        event,
        (harness.events.get(event) ?? []).filter((item) => item !== fn),
      ),
  };
  vi.stubGlobal("location", {
    hash: "#home",
    href: "https://example.test/",
    pathname: "/",
    origin: "https://example.test",
  });
  vi.stubGlobal("window", eventTarget);
  vi.stubGlobal("document", {
    ...eventTarget,
    visibilityState: "visible",
    getElementById: () => ({ focus: () => {} }),
  });
  vi.stubGlobal("requestAnimationFrame", (fn: () => void) => fn());
  await import("./main");
}, 120000);

describe("explicit panel entry refresh", () => {
  it("refreshes once on home entry and does not duplicate on an unchanged render", () => {
    render();
    render();
    expect(harness.requests).toEqual(["/dashboard?refresh_panel=true"]);
  });
  it("refreshes on profile/subscription transitions, not unrelated tabs or focus", () => {
    render();
    for (const page of ["payments", "help", "home", "account", "account"]) {
      location.hash = `#${page}`;
      dispatch("hashchange");
      render();
    }
    dispatch("focus");
    dispatch("visibilitychange");
    expect(
      harness.requests.filter((path) => path.includes("refresh_panel")),
    ).toEqual(Array(3).fill("/dashboard?refresh_panel=true"));
    render();
    expect(harness.requests.at(-1)).toBe("/dashboard");
  });
  it("an unrelated initial page only reads the local projection", () => {
    location.hash = "#payments";
    render();
    expect(harness.requests).toEqual(["/dashboard"]);
  });
});

function element(
  value: unknown,
  name: string,
): { props: Record<string, unknown> } | null {
  if (Array.isArray(value)) {
    for (const child of value) {
      const found = element(child, name);
      if (found) return found;
    }
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const node = value as {
    type?: { name?: string };
    props?: Record<string, unknown>;
  };
  if (node.type?.name === name && node.props) return { props: node.props };
  return element(node.props?.children, name);
}
describe("notification target context", () => {
  it("passes the exact bounded action to the customer without background quote fetching", () => {
    render();
    const center = element(harness.tree, "NotificationCenter");
    expect(center?.props.onAction).toBeTypeOf("function");
    const action = {
      label: "Продлить",
      target: "/#subscriptions",
      operation: "renew",
      metadata: { subscription_id: "owned-local", quote_id: "saved-quote" },
    };
    (center!.props.onAction as (value: unknown) => void)(action);
    render();
    expect(element(harness.tree, "Customer")?.props.notificationAction).toEqual(
      action,
    );
    expect(
      harness.requests.filter((path) => path.includes("renewal-offer")),
    ).toEqual([]);
  });
});
