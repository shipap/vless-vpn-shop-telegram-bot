import { expect, test } from "@playwright/test";
import { ownerStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

test("web broadcasts calculate and submit the automatic consented audience without recipient input", async ({
  page,
}) => {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#broadcasts"),
  );
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
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
        ...ownerStaffFixture,
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog") json = { products: [], periods: [], content: {} };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/broadcasts") json = { items: [], total: 0 };
    if (route.request().method() !== "GET") {
      writes.push({ path, body: route.request().postDataJSON() });
      json = path.endsWith("/estimate")
        ? {
            eligible_total: 12,
            recipient_count: 3,
            denied_count: 9,
            delivery: "durable_notifications",
          }
        : { id: "synthetic-broadcast", state: "pending", recipient_count: 3 };
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", {
      name: "Предпросмотр получателей",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByLabel(/Email получателей|Тестовые email получателей/),
  ).toHaveCount(0);
  await expect(page.getByRole("main")).toContainText(
    "Все пользователи с действующим согласием",
  );
  await expect(page.getByRole("main")).toContainText("Если привязан Telegram");
  await page
    .getByLabel("Текст сообщения для предпросмотра")
    .fill("Проверка автоматической аудитории");
  await page
    .getByRole("button", { name: "Рассчитать получателей", exact: true })
    .click();
  await expect(
    page.getByText("Всего с действующим согласием: 12", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Разрешено на локальном тестовом стенде: 3", {
      exact: true,
    }),
  ).toBeVisible();
  expect(writes[0]).toMatchObject({ path: "/admin/broadcasts/estimate" });
  expect(writes[0].body).not.toHaveProperty("recipients");
  await page
    .getByRole("button", { name: "Новая рассылка", exact: true })
    .click();
  const editor = page.getByRole("dialog");
  await expect(editor.getByLabel(/email|получател/i)).toHaveCount(0);
  await editor
    .getByLabel("Текст сообщения", { exact: true })
    .fill("Сообщение общей аудитории");
  await editor
    .getByRole("button", { name: "Создать рассылку", exact: true })
    .click();
  const confirmation = page.getByRole("dialog");
  await confirmation
    .getByLabel("Причина изменения")
    .fill("Проверка автоматической рассылки");
  await confirmation
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await confirmation.getByRole("checkbox").check();
  await confirmation
    .getByRole("button", { name: "Подтвердить и создать", exact: true })
    .click();
  await expect
    .poll(
      () => writes.filter((write) => write.path === "/admin/broadcasts").length,
    )
    .toBe(1);
  const submitted = writes.find((write) => write.path === "/admin/broadcasts")!;
  expect(submitted.body).not.toHaveProperty("recipients");
  expect(submitted.body.nodes).toEqual([
    {
      type: "paragraph",
      children: [{ type: "text", text: "Сообщение общей аудитории" }],
    },
  ]);
});
