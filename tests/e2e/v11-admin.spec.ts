import { expect, test } from "@playwright/test";

const common = [
  "statistics_read",
  "users_read",
  "catalog_read",
  "payments_read",
  "referrals_read",
  "promos_write",
  "messages_write",
  "broadcasts_write",
  "campaigns_read",
  "campaigns_write",
];
const ownerCapabilities = [
  ...common,
  "catalog_write",
  "settings_write",
  "security_manage",
  "wallet_write",
  "subscriptions_manage",
  "identity_review",
  "backups_manage",
  "operations_manage",
  "staff_manage",
  "content_write",
  "refunds_manage",
  "recurring_manage",
];

test("owner gift, identity resolution, node settings and backup require fresh MFA while direct message uses its own confirmed scoped request", async ({
  page,
}) => {
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#users"),
  );
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: {
          id: "synthetic-owner",
          email: "owner@example.test",
          email_verified: true,
          telegram_id: null,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        staff_role: "owner",
        capabilities: ownerCapabilities,
      };
    if (path === "/catalog")
      json = {
        products: [
          {
            id: "synthetic-product",
            name: "Синтетический тариф",
            available: true,
            periods: [],
          },
        ],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/users")
      json = {
        items: [
          {
            id: "synthetic-user",
            email: "user@example.test",
            email_verified: true,
            telegram_id: null,
          },
        ],
        total: 1,
      };
    if (path === "/admin/identity-requests")
      json = {
        items: [
          {
            id: "synthetic-review",
            account_id: "synthetic-user",
            reason: "Потерян доступ к Telegram",
            created_at: "2026-10-08T12:00:00Z",
            expires_at: "2099-01-01T00:00:00Z",
          },
        ],
      };
    if (path === "/admin/nodes")
      json = {
        items: [
          {
            id: "synthetic-node",
            name: "Тестовый сервер",
            visible: true,
            status: "online",
            observed_at: "2026-10-08T12:00:00Z",
            error: null,
          },
        ],
      };
    if (path === "/admin/backups")
      json = {
        items: [],
        schedule: "16:00",
        timezone: "Europe/Moscow",
        recipient: "owner",
        scope: "whole_test_database",
      };
    if (route.request().method() !== "GET") {
      writes.push({ path, body: route.request().postDataJSON() });
      json = { status: "queued" };
    }
    await route.fulfill({ json });
  });
  const confirm = async (fresh: boolean, label = "Подтвердить изменение") => {
    const dialog = page.getByRole("dialog");
    await dialog
      .getByLabel("Причина изменения")
      .fill("Проверка синтетического сценария");
    if (fresh)
      await dialog
        .getByLabel("Свежий код аутентификатора", { exact: true })
        .fill("123456");
    else
      await expect(
        dialog.getByLabel("Свежий код аутентификатора", { exact: true }),
      ).toHaveCount(0);
    await dialog.getByRole("checkbox").check();
    await dialog.getByRole("button", { name: label, exact: true }).click();
    await expect(dialog).toHaveCount(0);
  };
  await page.goto("/");
  await page
    .getByRole("button", { name: "Выдать доступ", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("combobox", { name: "Тариф", exact: true })
    .selectOption("synthetic-product");
  await page.getByRole("dialog").getByLabel("Дней доступа").fill("7");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Продолжить" })
    .click();
  await confirm(true, "Выдать доступ");
  await expect(page.locator("main")).toContainText("Запрос принят");
  await page.getByRole("button", { name: "Написать пользователю" }).click();
  await page
    .getByRole("dialog")
    .getByLabel("Текст сообщения")
    .fill("Синтетическое сообщение");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Продолжить" })
    .click();
  await confirm(false, "Отправить сообщение");
  await page
    .getByRole("link", { name: "Обращения по привязке", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Рассмотреть удаление привязки" })
    .click();
  await confirm(true);
  await page.getByRole("link", { name: "Серверы", exact: true }).click();
  await page
    .getByRole("button", { name: "Изменить название и видимость" })
    .click();
  await page
    .getByRole("dialog")
    .getByLabel("Публичное название")
    .fill("Новое публичное название");
  await page
    .getByRole("dialog")
    .getByLabel("Показывать пользователям")
    .uncheck();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Продолжить" })
    .click();
  await confirm(true);
  await page
    .getByRole("link", { name: "Резервные копии", exact: true })
    .click();
  await page.getByRole("button", { name: "Создать резервную копию" }).click();
  await confirm(true);
  expect(writes.filter((w) => w.path === "/admin/mfa/verify")).toHaveLength(4);
  expect(writes.find((w) => w.path.endsWith("/grant"))?.body).toMatchObject({
    product_id: "synthetic-product",
    days: 7,
    subscription_id: null,
    devices: null,
    confirmed: true,
    reason: "Проверка синтетического сценария",
    idempotency_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  expect(writes.find((w) => w.path.endsWith("/message"))?.body).toMatchObject({
    nodes: [
      {
        type: "paragraph",
        children: [{ type: "text", text: "Синтетическое сообщение" }],
      },
    ],
    confirmed: true,
  });
  expect(
    writes.find((w) => w.path.endsWith("/synthetic-node"))?.body,
  ).toMatchObject({
    name: "Новое публичное название",
    visible: false,
    confirmed: true,
  });
  expect(writes.filter((w) => /\/orders|\/payments/.test(w.path))).toHaveLength(
    0,
  );
});

test("ordinary admin creates a campaign with confirmed reason, sees measured statistics, and uses only promo-specific writes", async ({
  page,
}) => {
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#campaigns"),
  );
  const writes: { path: string; body: unknown }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: {
          id: "synthetic-admin",
          email: "admin@example.test",
          email_verified: true,
          telegram_id: null,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        staff_role: "admin",
        capabilities: common,
      };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/campaigns") {
      if (route.request().method() === "POST")
        writes.push({ path, body: route.request().postDataJSON() });
      json = {
        items: [
          {
            id: "synthetic-campaign",
            name: "Проверочная кампания",
            source: "partner",
            medium: "banner",
            campaign: "october",
            url: "https://t.me/synthetic_bot?start=utm_synthetic",
            website_url: "https://landing.example.test/?utm_id=synthetic",
            created_at: "2026-10-08T12:00:00Z",
          },
        ],
      };
    }
    if (path.endsWith("/stats"))
      json = {
        web_visits: 8,
        web_unique_visitors: 3,
        telegram_starts: 2,
        accounts: 1,
        paid_orders: 1,
        turnover: "100.50",
        currency: "RUB",
      };
    if (path === "/admin/promos") json = { items: [] };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Добавить кампанию" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Название кампании").fill("Новая кампания");
  await dialog.getByLabel("Источник").fill("partner");
  await dialog.getByLabel("Канал").fill("banner");
  await dialog.getByLabel("Кампания", { exact: true }).fill("october");
  await dialog.getByRole("button", { name: "Продолжить" }).click();
  await expect(page.getByRole("dialog")).not.toContainText("Код из приложения");
  await page
    .getByRole("dialog")
    .getByRole("textbox")
    .fill("Синтетическая проверка кампании");
  await page.getByRole("dialog").getByRole("checkbox").check();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Создать кампанию", exact: true })
    .click();
  expect(writes).toEqual([
    {
      path: "/admin/campaigns",
      body: {
        name: "Новая кампания",
        source: "partner",
        medium: "banner",
        campaign: "october",
        reason: "Синтетическая проверка кампании",
        confirmed: true,
      },
    },
  ]);
  await page.getByRole("button", { name: "Статистика кампании" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Уникальных посетителей",
  );
  await expect(page.getByRole("dialog")).toContainText("100,50 ₽");
  await page.keyboard.press("Escape");
  await page.getByRole("link", { name: "Промокоды", exact: true }).click();
  await expect(page.locator("main")).toContainText("Промокодов пока нет");
  await expect(page.getByRole("navigation")).not.toContainText(
    "Резервные копии",
  );
});

test("ordinary admin sees readable catalog and users without owner edits or direct forbidden sections", async ({
  page,
}) => {
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#users"),
  );
  const requests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    let json: unknown = {};
    if (path.endsWith("/auth/me"))
      json = {
        account: {
          id: "9007199254740993123",
          email: "synthetic-admin@example.test",
          email_verified: true,
          telegram_id: "9007199254740993999",
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        staff_role: "admin",
        capabilities: common,
      };
    if (path.endsWith("/users"))
      json = {
        items: [
          {
            id: "synthetic-user",
            email: "synthetic-user@example.test",
            telegram_id: null,
            email_verified: true,
          },
        ],
        total: 1,
      };
    if (path.endsWith("/catalog"))
      json = {
        items: [],
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await expect(page.getByRole("navigation")).toContainText("Пользователи");
  await expect(page.getByRole("navigation")).not.toContainText("Безопасность");
  await expect(page.getByRole("navigation")).not.toContainText("Операции");
  await expect(
    page.getByRole("button", { name: "Денежные средства" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Сеансы", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Каталог", exact: true }).click();
  await expect(page.locator("main")).toContainText("Каталог пуст");
  await expect(
    page.getByRole("button", { name: "Добавить тариф" }),
  ).toHaveCount(0);
  expect(requests).not.toContain("/api/v1/admin/panel-mapping");
  await page.evaluate(() => {
    location.hash = "security";
  });
  await expect(page.locator("main")).toContainText("Страница не найдена");
  expect(requests).not.toContain("/api/v1/admin/security");
});

test("owner navigation remains within viewport with reachable lower tools on compact and desktop screens", async ({
  page,
}) => {
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#backups"),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let json: unknown = {};
    if (path.endsWith("/auth/me"))
      json = {
        account: {
          id: "synthetic-owner",
          email: "synthetic-owner@example.test",
          email_verified: true,
          telegram_id: null,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        staff_role: "owner",
        capabilities: [
          ...common,
          "catalog_write",
          "settings_write",
          "security_manage",
          "wallet_write",
          "subscriptions_manage",
          "identity_review",
          "backups_manage",
          "operations_manage",
          "staff_manage",
          "content_write",
          "refunds_manage",
          "recurring_manage",
        ],
      };
    if (path.endsWith("/catalog"))
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path.endsWith("/notifications")) json = { items: [], unread_count: 0 };
    if (path.endsWith("/backups"))
      json = {
        items: [],
        schedule: "16:00",
        timezone: "Europe/Moscow",
        recipient: "owner",
        scope: "whole_test_database",
      };
    await route.fulfill({ json });
  });
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 700 });
    await page.goto("/");
    await expect(page.locator("main")).toContainText("16:00 МСК");
    if (width <= 700) await page.locator("#mobile-menu-toggle").click();
    const nav = page.getByRole("navigation");
    const last = nav.getByRole("link", { name: "Серверы", exact: true });
    await last.focus();
    const link = (await last.boundingBox())!;
    expect(link.y).toBeGreaterThanOrEqual(0);
    expect(link.y + link.height).toBeLessThanOrEqual(700);
    const footer = (await page.locator(".sidebar-bottom").boundingBox())!;
    expect(footer.y + footer.height).toBeLessThanOrEqual(700);
    expect(link.y + link.height).toBeLessThanOrEqual(footer.y);
    await page.screenshot({
      path: `.artifacts/ui/v11/owner-nav-${width}.png`,
      fullPage: true,
    });
    expect(
      await page.evaluate(() =>
        [...document.querySelectorAll(".workspace-top > *")]
          .map((el) => ({
            className: el.className,
            right: el.getBoundingClientRect().right,
            viewport: innerWidth,
          }))
          .filter((el) => el.right > el.viewport),
      ),
    ).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});
