import { beforeEach, describe, expect, it, vi } from "vitest";
import { PanelEventRouteForm } from "./admin-operations";
import type { components } from "@api/schema";
const harness = vi.hoisted(() => ({ slots: [] as unknown[], cursor: 0 }));
vi.mock("react", async (original) => ({
  ...(await original<typeof import("react")>()),
  useState: (initial: unknown) => {
    const slot = harness.cursor++;
    if (!(slot in harness.slots)) harness.slots[slot] = initial;
    return [
      harness.slots[slot],
      (next: unknown) => {
        harness.slots[slot] =
          typeof next === "function" ? next(harness.slots[slot]) : next;
      },
    ];
  },
}));
type Element = { type: string | Function; props: Record<string, any> };
function find(
  value: unknown,
  match: (value: Element) => boolean,
): Element | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const result = find(item, match);
      if (result) return result;
    }
    return null;
  }
  if (!value || typeof value !== "object" || !("props" in value)) return null;
  const item = value as Element;
  return match(item) ? item : find(item.props.children, match);
}
const uuid = "11111111-1111-4111-8111-111111111111";
const context = {
  version: 1,
  environment: "sandbox",
  panel_origin: "https://panel.example.test",
  contract: "3.4_id",
} as const;
const value = (): components["schemas"]["PanelEventRoutesResponse"] => ({
  configured: false,
  policy: null,
  registration_context: context,
  approved_node_uuids: [uuid],
  approved_product_ids: ["published-product"],
});
function draw(props: Parameters<typeof PanelEventRouteForm>[0]) {
  harness.cursor = 0;
  return PanelEventRouteForm(props);
}
function button(tree: unknown, text: string) {
  return find(
    tree,
    (e) =>
      typeof e.type === "function" &&
      (Array.isArray(e.props.children)
        ? e.props.children.join("")
        : e.props.children) === text,
  )!;
}
function field(tree: unknown, name: string) {
  const label = find(
    tree,
    (e) =>
      e.type === "label" &&
      (Array.isArray(e.props.children)
        ? e.props.children[0]
        : e.props.children
      )?.trim?.() === name,
  )!;
  return find(
    label.props.children,
    (e) => e.type === "input" || e.type === "select",
  )!;
}
beforeEach(() => {
  harness.slots = [];
});
describe("explicit owner mapping draft", () => {
  it("adds blank associations and delegates exact server context/case to the audited MFA confirmation", () => {
    const mutate = vi.fn(),
      props = { value: value(), canWrite: true, mutate };
    let tree = draw(props);
    button(tree, "Добавить сервер").props.onClick();
    button(tree, "Добавить связь CRM").props.onClick();
    tree = draw(props);
    expect(field(tree, "UUID разрешённого сервера").props.value).toBe("");
    expect(
      find(tree, (e) => e.type === "input" && e.props.type === "checkbox")!
        .props.checked,
    ).toBe(false);
    field(tree, "UUID разрешённого сервера").props.onChange({
      target: { value: uuid },
    });
    field(tree, "Публичное название сервера").props.onChange({
      target: { value: "Основной сервер" },
    });
    // Каждое событие получает состояние после обычного повторного рендера React.
    tree = draw(props);
    find(
      tree,
      (e) => e.type === "input" && e.props.type === "checkbox",
    )!.props.onChange({ target: { checked: true } });
    field(tree, "Название провайдера в CRM").props.onChange({
      target: { value: "ExactProvider" },
    });
    field(tree, "Название узла в CRM").props.onChange({
      target: { value: "ExactNode" },
    });
    field(tree, "Безопасное название для владельца").props.onChange({
      target: { value: "Инфраструктура проекта" },
    });
    tree = draw(props);
    find(tree, (e) => e.type === "form")!.props.onSubmit({
      preventDefault: () => {},
    });
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        path: "/admin/panel-event-routes",
        method: "PATCH",
        freshMfa: true,
        body: {
          policy: {
            ...context,
            nodes: [
              {
                node_uuid: uuid,
                product_ids: ["published-product"],
                safe_label: "Основной сервер",
              },
            ],
            infra: [
              {
                provider_name: "ExactProvider",
                node_name: "ExactNode",
                safe_label: "Инфраструктура проекта",
              },
            ],
          },
        },
      }),
    );
    expect(mutate.mock.calls[0]![0].body).not.toHaveProperty("confirmed");
    expect(mutate.mock.calls[0]![0].body).not.toHaveProperty("reason");
  });
  it.each([
    { node_uuid: "foreign-node", product_ids: ["published-product"] },
    { node_uuid: uuid, product_ids: ["foreign-product"] },
    { node_uuid: uuid, product_ids: [] },
  ])("does not submit an unknown or missing approved association", (node) => {
    const mutate = vi.fn();
    const data = value();
    data.policy = {
      ...context,
      nodes: [{ ...node, safe_label: "Тестовый сервер" }],
      infra: [],
    };
    const tree = draw({ value: data, canWrite: true, mutate });
    find(tree, (e) => e.type === "form")!.props.onSubmit({
      preventDefault: () => {},
    });
    expect(mutate).not.toHaveBeenCalled();
  });
  it("never delegates a save without current settings_write capability", () => {
    const mutate = vi.fn(),
      tree = draw({ value: value(), canWrite: false, mutate });
    expect(button(tree, "Сохранить связи событий").props.disabled).toBe(true);
    find(tree, (e) => e.type === "form")!.props.onSubmit({
      preventDefault: () => {},
    });
    expect(mutate).not.toHaveBeenCalled();
  });
  it("loads configured rows and can explicitly clear associations without changing context", () => {
    const mutate = vi.fn(),
      data = value();
    data.configured = true;
    data.policy = {
      ...context,
      nodes: [
        {
          node_uuid: uuid,
          product_ids: ["published-product"],
          safe_label: "Сервер проекта",
        },
      ],
      infra: [
        {
          provider_name: "ExactProvider",
          node_name: "ExactNode",
          safe_label: "Инфраструктура",
        },
      ],
    };
    const props = { value: data, canWrite: true, mutate };
    let tree = draw(props);
    expect(field(tree, "Название провайдера в CRM").props.value).toBe(
      "ExactProvider",
    );
    button(tree, "Убрать связь сервера 1").props.onClick();
    button(tree, "Убрать связь CRM 1").props.onClick();
    tree = draw(props);
    find(tree, (e) => e.type === "form")!.props.onSubmit({
      preventDefault: () => {},
    });
    expect(mutate.mock.calls[0]![0].body.policy).toEqual({
      ...context,
      nodes: [],
      infra: [],
    });
  });
});
