import { expect, test, type Page } from "@playwright/test";
import { ownerStaffFixture, adminStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

type Write = { path: string; body: Record<string, unknown> };
async function syntheticOperations(
  page: Page,
  options: { owner?: boolean; section?: string; state?: string } = {},
) {
  const writes: Write[] = [];
  const role = options.owner === false ? adminStaffFixture : ownerStaffFixture;
  const state = options.state ?? "pending";
  await page.addInitScript(
    (section) => history.replaceState(null, "", `/admin#${section}`),
    options.section ?? "operations",
  );
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return ["127.0.0.1", "localhost"].includes(url.hostname)
      ? route.continue()
      : route.fulfill({ body: "", contentType: "application/javascript" });
  });
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
        ...role,
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog") json = { products: [], periods: [], content: {} };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (["/admin/operations", "/admin/orders"].includes(path))
      json = { items: [], total: 0 };
    if (path === "/admin/operations/health")
      json = {
        database: { state: "ok" },
        redis: { state: "ok" },
        backup: { state: "not_verified" },
        maintenance: { enabled: false },
        queue: { items: [] },
        checked_at: "2026-10-09T12:00:00Z",
        issues: {
          signal_count: 2,
          truncated: false,
          items: [
            {
              key: "job:provision-safe",
              kind: "job",
              reference: "provision-safe",
              order_id: "order-safe",
              state: "review",
              age_seconds: 1200,
              reason: "Выдача доступа ожидает восстановления.",
              retry_kind: "provisioning",
              target: "/admin#orders",
            },
            {
              key: "provider_operation:operation-unknown",
              kind: "provider_operation",
              reference: "operation-unknown",
              state: "outcome_unknown",
              age_seconds: 1800,
              reason:
                "Неизвестен результат платежа: требуется сверка, новое списание запрещено.",
              retry_kind: null,
              target: "/admin#orders",
            },
          ],
        },
      };
    const report = {
      id: "broadcast-safe",
      state,
      recipient_count: 8,
      delivered_count: 2,
      blocked_count: 1,
      provider_rejected_count: 1,
      unknown_count: 1,
      waiting_count: 2,
      cancelled_count: 1,
    };
    if (path === "/admin/broadcasts") json = { items: [report], total: 1 };
    if (path === "/admin/broadcasts/broadcast-safe") json = report;
    if (route.request().method() !== "GET") {
      writes.push({ path, body: route.request().postDataJSON() });
      if (
        ![
          "/admin/mfa/verify",
          "/admin/orders/order-safe/retry-provisioning",
        ].includes(path)
      ) {
        await route.fulfill({
          status: 500,
          json: {
            detail: "Unexpected mutation in isolated browser regression",
          },
        });
        return;
      }
      json = { status: "pending" };
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  return writes;
}

test("owner restores provisioning through existing reason, fresh MFA and explicit confirmation only", async ({
  page,
}) => {
  const writes = await syntheticOperations(page);
  await expect(
    page.getByRole("heading", { name: "Проблемные операции", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Сигналов: 2", { exact: true })).toBeVisible();
  const unknown = page
    .getByRole("row")
    .filter({ hasText: "operation-unknown" });
  await expect(unknown).toContainText("новое списание запрещено");
  await expect(unknown.getByRole("button")).toHaveCount(0);
  await expect(
    unknown.getByRole("link", { name: "Открыть раздел", exact: true }),
  ).toHaveAttribute("href", "/admin#orders");
  await expect(
    page.getByRole("button", { name: /Повторить оплату|Повторить списание/ }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Повторить выдачу", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Повторить выдачу доступа?",
    exact: true,
  });
  await expect(dialog).toContainText("Нового списания не будет");
  expect(writes).toEqual([]);
  await expect(
    dialog.getByRole("button", { name: "Подтвердить изменение", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByLabel("Причина изменения", { exact: true })
    .fill("Проверка восстановления оплаченного доступа");
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toEqual([
    { path: "/admin/mfa/verify", body: { code: "123456" } },
    {
      path: "/admin/orders/order-safe/retry-provisioning",
      body: {
        reason: "Проверка восстановления оплаченного доступа",
        confirmed: true,
      },
    },
  ]);
});

test("ordinary admin cannot open owner operations or offer provisioning retry", async ({
  page,
}) => {
  const writes = await syntheticOperations(page, { owner: false });
  await expect(page.getByRole("main")).toContainText("Страница не найдена");
  await expect(
    page.getByRole("heading", { name: "Проблемные операции", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Повторить выдачу", exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});

for (const state of ["pending", "done"]) {
  test(`broadcast ${state} report distinguishes preparation, delivery and unknown outcomes`, async ({
    page,
  }) => {
    const writes = await syntheticOperations(page, {
      section: "broadcasts",
      state,
    });
    await page
      .getByRole("row")
      .filter({ hasText: "broadcast-safe" })
      .getByRole("button", { name: "Открыть", exact: true })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "Результаты рассылки",
      exact: true,
    });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("term")).toHaveText([
      "Получателей",
      "Доставлено",
      "Заблокировано правилами или согласием",
      "Отклонено Telegram или почтой",
      "Неизвестный результат",
      "Ожидают доставки",
      "Отменено",
    ]);
    await expect(dialog.getByRole("definition")).toHaveText([
      "8",
      "2",
      "1",
      "1",
      "1",
      "2",
      "1",
    ]);
    await expect(dialog).toContainText(
      "Автоматический повтор может создать дубликат",
    );
    await expect(dialog).toContainText("Отправка продолжается");
    if (state === "pending") {
      await expect(dialog).toContainText(
        "Подготовка очереди не означает доставку",
      );
      await expect(dialog).not.toContainText("Подготовка очереди завершена");
    } else
      await expect(dialog).toContainText(
        "Подготовка очереди завершена — это не означает доставку",
      );
    expect(writes).toEqual([]);
  });
}
