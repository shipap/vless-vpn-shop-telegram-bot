import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OperationalControls } from "./admin-operations";
import type { components } from "@api/schema";

type Routes = components["schemas"]["PanelEventRoutesResponse"];
const fixture = vi.hoisted(() => ({
  response: null as Routes | null,
  reads: [] as string[],
}));
vi.mock("@ui/staff-permissions", () => ({ useCapability: () => true }));
vi.mock("@api/client", () => ({
  api: vi.fn(),
  useApi: (path: string) => {
    fixture.reads.push(path);
    return {
      data:
        path === "/admin/panel-event-routes"
          ? fixture.response
          : { maintenance: false },
      loading: false,
      error: null,
      reload: () => {},
    };
  },
}));
const response = (): Routes => ({
  configured: false,
  policy: null,
  registration_context: {
    version: 1,
    environment: "local_test",
    panel_origin: "https://panel.example.test",
    contract: "3.4_id",
  },
  approved_node_uuids: ["11111111-1111-4111-8111-111111111111"],
  approved_product_ids: ["published-product"],
});
function render(isOwner = true) {
  return renderToStaticMarkup(
    <OperationalControls
      section="operations"
      isOwner={isOwner}
      mutate={() => {}}
      onView={() => {}}
      refreshVersion={0}
    />,
  );
}
beforeEach(() => {
  fixture.response = response();
  fixture.reads = [];
});
describe("owner panel event route registration", () => {
  it("loads the published policy and displays its read-only exact registration context", () => {
    const html = render();
    expect(fixture.reads).toContain("/admin/panel-event-routes");
    expect(html).toContain("Маршруты событий панели");
    expect(html).toContain("Связи ещё не настроены");
    expect(html).toContain("https://panel.example.test");
    expect(html).toContain("3.4_id");
    expect(html).toContain("Сохранение связей не включает вебхуки");
  });
  it("does not expose or fetch owner routes for an ordinary administrator", () => {
    expect(render(false)).not.toContain("Маршруты событий панели");
    expect(fixture.reads).not.toContain("/admin/panel-event-routes");
  });
  it("shows unsupported registration honestly without an editable form", () => {
    fixture.response = {
      ...response(),
      registration_context: null,
      approved_node_uuids: [],
      approved_product_ids: [],
    };
    const html = render();
    expect(html).toContain("В текущем окружении регистрация недоступна");
    expect(html).not.toContain("Добавить сервер");
    expect(html).not.toContain("Сохранить связи событий");
  });
  it("does not guess a node/product association when the approved inventory is empty", () => {
    fixture.response = {
      ...response(),
      approved_node_uuids: [],
      approved_product_ids: [],
    };
    const html = render();
    expect(html).toContain("Нет разрешённых серверов или доступных тарифов");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Добавить сервер/);
  });
});
