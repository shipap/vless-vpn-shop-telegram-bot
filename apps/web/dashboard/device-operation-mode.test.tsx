import { beforeEach, expect, it, vi } from "vitest";
import { CustomerDevices } from "./customer-devices";

const state = vi.hoisted(() => ({
  rotation: null as string | null,
  slots: [] as unknown[],
  cursor: 0,
  requests: [] as { path: string; options: any }[],
}));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: unknown) => {
    const index = state.cursor++;
    if (!(index in state.slots)) state.slots[index] = initial;
    return [
      state.slots[index],
      (next: unknown) => {
        state.slots[index] = next;
      },
    ];
  },
  useEffect: () => {},
  useRef: (initial: unknown) => ({ current: initial }),
}));
vi.mock("@api/client", async (original) => ({
  ...(await original<typeof import("@api/client")>()),
  useApi: () => ({
    data: {
      source: "remnawave",
      status: "ACTIVE",
      observed_at: "2026-10-13T12:00:00Z",
      devices: [
        { id: "own-device", generation: 2, name: "Ноутбук", revoked: false },
      ],
    },
    reload: () => {},
  }),
  api: async (path: string, options: unknown) => {
    state.requests.push({ path, options });
    return { status: "revoked" };
  },
}));
function render() {
  state.cursor = 0;
  return CustomerDevices({
    subscription: {
      id: "own-sub",
      credential_rotation_state: state.rotation,
    } as never,
    blocked: false,
    onAdd: () => {},
    onConnect: () => {},
    onRefresh: () => {},
    onRotate: () => {},
  });
}
function find(tree: any, caption: string): any {
  if (Array.isArray(tree))
    return tree.map((node) => find(node, caption)).find(Boolean);
  if (!tree || typeof tree !== "object") return null;
  if (
    tree.props?.children === caption &&
    typeof tree.props?.onClick === "function"
  )
    return tree;
  return find(tree.props?.children, caption);
}
beforeEach(() => {
  state.slots = [];
  state.rotation = null;
  state.requests = [];
});
it.each([
  ["Отвязать", "single"],
  ["Отвязать все устройства", "bulk"],
])(
  "confirms %s with explicit mode even when only one device exists",
  async (caption, mode) => {
    find(render(), caption).props.onClick();
    expect(state.requests).toEqual([]);
    find(render(), "Подтвердить отвязку").props.onClick();
    await Promise.resolve();
    expect(state.requests).toHaveLength(1);
    expect(state.requests[0].path).toBe(
      "/subscriptions/own-sub/devices/revoke-bulk",
    );
    expect(state.requests[0].options.body).toMatchObject({
      device_ids: ["own-device"],
      expected_generations: { "own-device": 2 },
      operation_mode: mode,
      confirmed: true,
    });
  },
);

it("shows blocked credential rotation as manual review rather than pending", () => {
  state.rotation = "blocked";
  const tree = render();
  expect(JSON.stringify(tree)).toContain("Перевыпуск ссылки требует сверки");
  expect(find(tree, "Перевыпустить ссылку").props.disabled).toBe(true);
});
