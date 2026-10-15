import { syntheticInstance } from "./instance-fixture";
import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { ownerStaffFixture } from "./staff-fixture";

test("карточка пользователя: история, приглашённые и подтверждение предупреждения", async ({
  page,
}) => {
  const reads: string[] = [],
    writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/admin", async (route) =>
    route.fulfill({
      response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname.replace("/api/v1", "");
    reads.push(path + url.search);
    if (route.request().method() !== "GET") {
      writes.push({ path, body: route.request().postDataJSON() });
      return route.fulfill({ json: {} });
    }
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: { id: "1", email: "owner@example.test", email_verified: true },
        is_admin: true,
        ...ownerStaffFixture,
        mfa_verified: true,
        csrf_token: "synthetic",
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog") json = { products: [], periods: [], content: {} };
    if (path === "/admin/users")
      json = {
        items: [
          {
            id: "2",
            email: "friend@example.test",
            telegram_username: "friend",
          },
        ],
        total: 1,
      };
    if (path === "/admin/users/2")
      json = {
        account: { id: "2" },
        identities: { telegram_username: "friend" },
        subscriptions: [],
        orders: [],
        payments: [],
        ledger: [],
        devices: [],
        audit: [],
        active_subscriptions_total: 2,
        active_agreements_total: 30,
        referral: { balance: "15.00", earned_total: "30.00", invited_total: 2 },
        moderation: {
          unlink_month: 3,
          unlink_total: 7,
          current_warnings: 1,
          total_warnings: 1,
          unlink_blocked: false,
        },
        can_moderate: true,
        pagination: {
          page: Number(url.searchParams.get("page")),
          page_size: 25,
          totals: { orders: 30 },
        },
      };
    if (path === "/admin/users/2/invitees")
      json = { items: [{ id: "3", email: "invitee@example.test" }], total: 1 };
    await route.fulfill({ json });
  });
  await page.goto("/admin#users");
  await page.getByRole("button", { name: "Открыть", exact: true }).click();
  const drawer = page.getByRole("dialog");
  await drawer.locator("summary", { hasText: "Подписки и автооплата" }).click();
  await expect(drawer).toContainText("Действующих соглашений: 30");
  await drawer.getByRole("button", { name: "Далее", exact: true }).click();
  await expect
    .poll(() => reads.some((path) => path.includes("/admin/users/2?page=2")))
    .toBe(true);
  await drawer.locator("summary", { hasText: "Реферальная система" }).click();
  await drawer
    .getByRole("button", { name: "Кого пригласил", exact: true })
    .click();
  await expect(drawer).toContainText("invitee@example.test");
  await expect(drawer.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await drawer
    .getByRole("button", { name: "Предупреждение", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "VPN, покупки и продление остаются доступны",
  );
  expect(writes).toHaveLength(0);
  await expect(
    page.getByRole("dialog").getByRole("textbox", { name: /Причина/ }),
  ).toHaveCount(0);
  await page
    .getByRole("dialog")
    .locator('input[type="password"]')
    .fill("123456");
  await page.getByRole("dialog").getByRole("checkbox").check();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Подтвердить/ })
    .click();
  await expect
    .poll(() => writes.some((item) => item.path.endsWith("/device-moderation")))
    .toBe(true);
  expect(
    writes.find((item) => item.path.endsWith("/device-moderation"))?.body,
  ).toMatchObject({
    action: "warn",
    confirmed: true,
    reason:
      "Подтверждение действия warn в карточке пользователя 2 через кабинет",
  });
});

test("личная ссылка копируется, а блокировка отключает только отвязку", async ({
  page,
  context,
}) => {
  const writes: string[] = [];
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET") writes.push(path);
    const account = {
      id: "1",
      email: "synthetic@example.test",
      email_verified: true,
      unlink_blocked: true,
    };
    let json: unknown = {};
    if (path === "/auth/me")
      json = { account, csrf_token: "synthetic", is_admin: false };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/subscriptions/synthetic-sub/inventory")
      json = {
        subscription_id: "synthetic-sub",
        source: "remnawave",
        status: "ACTIVE",
        observed_at: "2026-10-08T00:00:00Z",
        error: null,
        traffic_bytes: "0",
        traffic_limit_bytes: null,
        paid_expires_at: "2099-12-01T00:00:00Z",
        devices: [
          {
            id: "synthetic-device",
            name: "Телефон",
            platform: "Android",
            generation: 1,
            revoked: false,
            observed_at: "2026-10-08T00:00:00Z",
          },
        ],
      };
    if (path === "/legal/status")
      json = { accepted: true, configured: true, missing: [], documents: [] };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {
          help_center: {
            connection_instructions:
              "1. Установите Incy или Happ.\n2. Импортируйте ссылку.\n3. Выберите локацию. Можно делиться с друзьями и семьёй.",
          },
        },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "synthetic-sub",
            product_id: "test",
            name: "Моя подписка",
            status: "active",
            expires_at: "2099-12-01T00:00:00Z",
            devices_limit: 3,
            version: 1,
            connection_url: "https://vpn.example.test/s/synthetic",
            devices: [
              {
                id: "synthetic-device",
                name: "Телефон",
                last_seen_at: "2026-10-08T00:00:00Z",
              },
            ],
            segments: [],
            recurring: null,
          },
        ],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    await route.fulfill({ json });
  });
  await page.goto("/#home");
  await page
    .getByRole("button", { name: "Подключить VPN", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Перейти по ссылке" }),
  ).toHaveAttribute("href", "https://vpn.example.test/s/synthetic");
  await page
    .getByRole("button", { name: "Скопировать ссылку на конфигурацию" })
    .click();
  await expect(
    page.getByText("Ссылка скопирована.", { exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "https://vpn.example.test/s/synthetic",
  );
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await mkdir(".artifacts/ui/service-v9", { recursive: true });
  await page.screenshot({
    path: ".artifacts/ui/service-v9/connect-mobile.png",
  });
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await page.goto("/#devices");
  await expect(
    page.getByText("Отвязка устройств заблокирована из-за предупреждений.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Телефон", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Отвязать", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Отвязать все устройства", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Добавить устройства", exact: true }),
  ).toBeEnabled();
  expect(writes.filter((path) => path.includes("revoke"))).toEqual([]);
});

test("FAQ использует общие инструкции, документы и каналы без обращения к внешним сервисам", async ({
  page,
}) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const account = {
      id: "9007199254740993123",
      email: "synthetic@example.test",
      email_verified: true,
    };
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
      };
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        dashboard_url: "https://shop.example.test",
        support_url: "https://t.me/example",
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        bonus_reserved: "0",
        credit_reserved: "0",
      };
    const documents = [
      {
        document_type: "offer",
        version: "v1",
        url: "https://example.test/offer",
      },
      {
        document_type: "privacy",
        version: "v1",
        url: "https://example.test/privacy",
      },
    ];
    if (path === "/legal/status")
      json = { configured: true, accepted: true, missing: [], documents };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        legal: { configured: true, documents },
        content: {
          help_center: {
            support_url: "https://t.me/example",
            dashboard_url: "https://shop.example.test",
            faq: [
              {
                id: "purchase",
                title: "Где ещё можно купить подписку?",
                body: "Подписка доступна на сайте.",
              },
              {
                id: "promos",
                title: "Промокоды и скидки",
                body: "3 месяца — 10%, 6 месяцев — 15%, 12 месяцев — 20%.",
              },
            ],
            promo_channels: [
              {
                title: "Telegram-канал",
                url: "https://example.test/community",
              },
              {
                title: "YouTube-канал",
                url: "https://example.test/videos",
              },
            ],
          },
        },
      };
    await route.fulfill({ json });
  });
  await page.goto("/#help");
  await expect(
    page.getByRole("heading", { name: "FAQ — часто задаваемые вопросы" }),
  ).toBeVisible();
  await expect(
    page
      .locator(".service-faq")
      .getByRole("link", { name: "Публичная оферта" }),
  ).toHaveAttribute("href", "https://example.test/offer");
  await expect(
    page
      .locator(".service-faq")
      .getByRole("link", { name: "Политика конфиденциальности" }),
  ).toHaveAttribute("href", "https://example.test/privacy");
  await page.getByText("Промокоды и скидки", { exact: true }).click();
  await expect(
    page.getByText("3 месяца — 10%, 6 месяцев — 15%, 12 месяцев — 20%."),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Telegram-канал" }),
  ).toHaveAttribute("href", "https://example.test/community");
  await page.setViewportSize({ width: 375, height: 812 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
