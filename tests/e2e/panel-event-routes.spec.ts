import { expect, test, type Page } from "@playwright/test";
import { ownerStaffFixture, adminStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

const uuid = "11111111-1111-4111-8111-111111111111";
const context = {
  version: 1,
  environment: "local_test",
  panel_origin: "https://panel.example.test",
  contract: "3.4_id",
};
type Write = { path: string; body: Record<string, unknown> };
async function setup(page: Page, owner = true, supported = true) {
  const writes: Write[] = [],
    reads: string[] = [];
  let policy: unknown = null;
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#operations"),
  );
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (request.method() === "GET") reads.push(path);
    if (path === "/auth/me")
      json = {
        account: {
          id: "9007199254740993123",
          email: "owner@example.test",
          email_verified: true,
          telegram_id: null,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        ...(owner ? ownerStaffFixture : adminStaffFixture),
        ...(!owner
          ? {
              capabilities: [
                ...adminStaffFixture.capabilities,
                "operations_manage",
                "settings_write",
              ],
            }
          : {}),
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog") json = { products: [], periods: [], content: {} };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/operations/health")
      json = {
        maintenance: false,
        queue: { items: [] },
        subscription_retention: { enabled: false, days_after_expiry: 7 },
      };
    if (path === "/admin/panel-event-routes")
      json = {
        configured: policy !== null,
        policy,
        registration_context: supported ? context : null,
        approved_node_uuids: supported ? [uuid] : [],
        approved_product_ids: supported ? ["published-product"] : [],
      };
    if (request.method() !== "GET") {
      const body = request.postDataJSON() as Record<string, unknown>;
      writes.push({ path, body });
      if (path === "/admin/panel-event-routes") {
        policy = body.policy;
        json = policy;
      }
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await page.locator("summary", { hasText: "Состояние операций" }).click();
  if (owner)
    await page
      .locator("summary", { hasText: "Маршруты событий панели" })
      .click();
  return { writes, reads };
}

test("owner explicitly maps approved nodes/products and exact CRM names through fresh MFA and audit confirmation", async ({
  page,
}) => {
  const { writes } = await setup(page);
  await expect(
    page.locator("summary", { hasText: "Маршруты событий панели" }),
  ).toBeVisible();
  await expect(page.getByText(/Окружение: local_test/)).toContainText(
    context.panel_origin,
  );
  await expect(
    page.getByText(/Сохранение связей не включает вебхуки/),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Добавить сервер", exact: true })
    .click();
  const server = page.getByRole("group", {
    name: "Связь сервера 1",
    exact: true,
  });
  const node = server.getByRole("combobox", {
    name: "UUID разрешённого сервера",
    exact: true,
  });
  await expect(node).toHaveValue("");
  await expect(node.locator("option")).toHaveCount(2);
  await node.selectOption(uuid);
  await server
    .getByLabel("Публичное название сервера", { exact: true })
    .fill("Сервер проекта");
  await expect(
    server.getByRole("checkbox", {
      name: "Тариф published-product",
      exact: true,
    }),
  ).not.toBeChecked();
  await page
    .getByRole("button", { name: "Сохранить связи событий", exact: true })
    .click();
  await expect(
    page.getByText(
      "Выберите разрешённый сервер и хотя бы один доступный тариф.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(writes).toEqual([]);
  await server
    .getByRole("checkbox", { name: "Тариф published-product", exact: true })
    .check();
  await page
    .getByRole("button", { name: "Добавить связь CRM", exact: true })
    .click();
  await page
    .getByLabel("Название провайдера в CRM", { exact: true })
    .fill("ExactProvider");
  await page
    .getByLabel("Название узла в CRM", { exact: true })
    .fill("ExactNode");
  await page
    .getByLabel("Безопасное название для владельца", { exact: true })
    .fill("Инфраструктура проекта");
  await page
    .getByRole("button", { name: "Сохранить связи событий", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  expect(writes).toEqual([]);
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes.map((write) => write.path)).toEqual([
    "/admin/mfa/verify",
    "/admin/panel-event-routes",
  ]);
  expect(writes[1]!.body).toEqual({
    confirmed: true,
    policy: {
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
          safe_label: "Инфраструктура проекта",
        },
      ],
    },
  });
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText("Связи настроены.", { exact: true })).toHaveCount(
    1,
  );
  for (const title of ["Состояние операций", "Маршруты событий панели"]) {
    const summary = page.locator("summary", { hasText: title });
    if (
      !(await summary.evaluate((node) =>
        node.parentElement!.hasAttribute("open"),
      ))
    )
      await summary.click();
  }
  await expect(
    page.getByText("Связи настроены.", { exact: true }),
  ).toBeVisible();
});

test("unsupported owner registration does not offer an editable map", async ({
  page,
}) => {
  const { writes } = await setup(page, true, false);
  await expect(
    page.locator("summary", { hasText: "Маршруты событий панели" }),
  ).toBeVisible();
  await expect(
    page.getByText(/В текущем окружении регистрация недоступна/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Добавить сервер", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Сохранить связи событий", exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("an administrator never fetches or displays the owner route editor", async ({
  page,
}) => {
  const { reads, writes } = await setup(page, false);
  // Даже operations/settings capabilities не подменяют роль владельца.
  await expect(page.getByText("Администратор", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Состояние сервисов", exact: true }),
  ).toBeVisible();
  await expect(
    page.locator("summary", { hasText: "Маршруты событий панели" }),
  ).toHaveCount(0);
  expect(reads).not.toContain("/admin/panel-event-routes");
  expect(writes).toEqual([]);
});
