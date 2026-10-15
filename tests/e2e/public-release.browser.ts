import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { neutralInstance } from "../../packages/web-ui/instance-value";
import { ownerStaffFixture } from "./staff-fixture";

const images = fileURLToPath(
  new URL("../../docs/assets/screenshots/", import.meta.url),
);
const account = {
  id: "9007199254740993123",
  email: "demo@example.test",
  email_verified: true,
  telegram_id: null,
};
const periods = [
  { days: 30, months: 1, price: "150.00", discount_percent: "0" },
  { days: 90, months: 3, price: "405.00", discount_percent: "10" },
  { days: 180, months: 6, price: "765.00", discount_percent: "15" },
  { days: 365, months: 12, price: "1440.00", discount_percent: "20" },
];
const products = [1, 2].map((rank) => ({
  id: `demo-product-${rank}`,
  code: `demo-${rank}`,
  semantic_role: `demo-${rank}`,
  name: rank === 1 ? "Повседневный" : "Расширенный",
  description: "Подключение ваших устройств. Условия задаёт владелец сервиса.",
  currency: "RUB",
  price: rank === 1 ? "150.00" : "300.00",
  available: true,
  version: 1,
  tier_level: rank,
  included_devices: rank + 2,
  content: {},
  periods,
  base_period_days: 30,
  sort_order: rank,
  highlighted: rank === 2,
  visible_landing: true,
  visible_bot: true,
  extra_device_price: "40.00",
  eligibility: { allow_upgrade: true, allow_downgrade: true },
}));
const legal = {
  configured: true,
  accepted: true,
  revision: "demo-v1",
  missing: [],
  documents: ["offer", "privacy"].map((type) => ({
    document_type: type,
    version: "demo-v1",
    url: `https://example.test/${type}`,
  })),
};
const subscription = {
  id: "demo-subscription",
  product_id: products[0].id,
  product_name: products[0].name,
  name: products[0].name,
  status: "active",
  expires_at: "2027-12-01T12:00:00Z",
  connection_url: null,
  devices: [],
  devices_limit: 3,
  segments: [],
  recurring: null,
  observed_at: "2026-10-15T12:00:00Z",
  device_inventory_state: "fresh",
  provisioning: { state: "done" },
  retention_state: "none",
};

async function fixture(
  page: Page,
  { staff = false, authenticated = true, notice = false } = {},
) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  if (staff)
    await page.addInitScript(() => {
      if (location.pathname === "/")
        history.replaceState(null, "", `/admin${location.hash || "#overview"}`);
    });
  await page.route("**/*", (route) => {
    const host = new URL(route.request().url()).hostname;
    return ["127.0.0.1", "localhost"].includes(host)
      ? route.continue()
      : route.abort();
  });
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET")
      return route.fulfill({
        status: 403,
        json: {
          error: { code: "synthetic_read_only", message: "Только просмотр" },
        },
      });
    let json: unknown = { items: [], total: 0 };
    if (path === "/auth/me") {
      if (!authenticated)
        return route.fulfill({
          status: 401,
          json: {
            error: {
              code: "authentication_required",
              message: "Войдите в аккаунт",
            },
          },
        });
      json = {
        account,
        csrf_token: "synthetic-csrf",
        is_admin: staff,
        mfa_verified: staff,
        ...(staff ? ownerStaffFixture : { capabilities: [] }),
      };
    }
    if (path === "/public-config")
      json = {
        instance: {
          ...neutralInstance,
          app_name: "VLESS VPN Shop",
          app_short_name: "VLESS VPN Shop",
          app_slug: "vpn-shop",
          sender_name: "VLESS VPN Shop",
          support_url: "https://example.test/support",
        },
        local_test: true,
        support_url: "https://example.test/support",
        turnstile_site_key: null,
      };
    if (path === "/catalog")
      json = {
        products,
        periods,
        content: {
          faq: [],
          catalog_description: "Выберите подходящий тариф и срок подключения.",
        },
        legal,
        recurring_enrollment_enabled: false,
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [subscription],
        payments: [],
        bonus_balance: "120.00",
        credit_balance: "0",
        credit_reserved: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
      };
    if (path.startsWith("/legal")) json = legal;
    if (path === "/notifications")
      json = {
        items: notice
          ? [
              {
                id: "synthetic-announcement",
                kind: "broadcast",
                title: "Сервис обновлён",
                body: Array.from(
                  { length: 10 },
                  () =>
                    "Новости и сообщения сервиса доступны в личном кабинете. Если нужна помощь с подключением, обратитесь в поддержку.",
                ).join("\n\n"),
                severity: "info",
                created_at: "2026-10-15T12:00:00Z",
                read_at: null,
                target: null,
              },
            ]
          : [],
        unread_count: notice ? 1 : 0,
      };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/promos/active") json = null;
    if (path === "/admin/catalog")
      json = { items: products, total: products.length };
    if (path === "/admin/overview")
      json = {
        currency: "RUB",
        timezone: "Europe/Moscow",
        service_count: 24,
        service_value: { RUB: "3600.00" },
        credit_spent: { RUB: "120.00" },
        currencies: { RUB: "3600.00" },
        refunds: { RUB: "0.00" },
        confirmed_fees: { RUB: "0.00" },
        active_paid_subscriptions: 24,
        sales_composition: [],
        service_rows: [],
        settlement_series: [],
        recurring_cycles: [],
        provisioning: [],
        recurring_rows: [],
        series: [],
        rows: [],
      };
    if (path === "/admin/operations/health")
      json = {
        database: { state: "ok" },
        redis: { state: "ok" },
        backup: { state: "not_verified" },
        maintenance: { enabled: false },
        queue: { items: [] },
        issues: { signal_count: 0, items: [] },
      };
    return route.fulfill({ json });
  });
  return errors;
}

async function capture(page: Page, name: string, errors: string[]) {
  await page.getByRole("main").waitFor();
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("body")).not.toContainText(
    /sunrise|bububi|nnstore/i,
  );
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  await mkdir(images, { recursive: true });
  await page.screenshot({
    path: `${images}${name}.png`,
    fullPage: true,
    animations: "disabled",
  });
}

test("generic landing desktop and mobile", async ({ page }) => {
  const errors = await fixture(page);
  await page.goto(`http://127.0.0.1:${process.env.PUBLIC_QA_LANDING_PORT}`);
  await expect(
    page.getByRole("heading", { name: "Повседневный", exact: true }),
  ).toBeVisible();
  await capture(page, "landing-desktop", errors);
  await page.setViewportSize({ width: 390, height: 844 });
  await capture(page, "landing-mobile", errors);
});

test("customer subscriptions and account", async ({ page }) => {
  const errors = await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Повседневный", exact: true }),
  ).toBeVisible();
  await capture(page, "customer-subscriptions", errors);
  await page
    .getByRole("link", { name: "Аккаунт", exact: true })
    .first()
    .click();
  await capture(page, "customer-account", errors);
});

test("admin dashboard and tariff management", async ({ page }) => {
  const errors = await fixture(page, { staff: true });
  await page.goto("/#overview");
  await expect(
    page.getByText("Выданные услуги и внутренние средства"),
  ).toBeVisible();
  await capture(page, "admin-dashboard", errors);
  await page.goto("/#catalog");
  await expect(
    page.getByText("Повседневный", { exact: true }).first(),
  ).toBeVisible();
  await capture(page, "admin-catalog", errors);
});

test("mobile notification panel stays inside the visible viewport", async ({
  page,
}) => {
  const errors = await fixture(page, { notice: true });
  for (const width of [320, 390]) {
    await page.setViewportSize({ width, height: 640 });
    await page.goto("/");
    const bell = page.getByRole("button", { name: /^Уведомления, 1 / });
    await bell.click();
    const panel = page.locator(".notification-popover");
    await expect(panel).toBeVisible();
    const box = await panel.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(12);
    expect(box!.x + box!.width).toBeLessThanOrEqual(width - 12);
    expect(box!.y + box!.height).toBeLessThanOrEqual(640 - 12);
    const inbox = panel.locator(".notification-inbox");
    const headerBox = await panel
      .locator(".notification-popover-head")
      .boundingBox();
    const inboxBox = await inbox.boundingBox();
    expect(
      inboxBox!.y - (headerBox!.y + headerBox!.height),
    ).toBeGreaterThanOrEqual(12);
    expect(
      await inbox.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    ).toBe(true);
    const headingTop = (await panel
      .locator(".notification-popover-head")
      .boundingBox())!.y;
    await inbox.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    expect(
      (await panel.locator(".notification-popover-head").boundingBox())!.y,
    ).toBe(headingTop);
    expect(await panel.evaluate((element) => element.scrollTop)).toBe(0);
    const backgroundScroll = await page.evaluate(() => scrollY);
    await inbox.hover();
    await page.mouse.wheel(0, 1200);
    await expect
      .poll(() => page.evaluate(() => scrollY))
      .toBe(backgroundScroll);
    await inbox.evaluate((element) => {
      element.scrollTop = 0;
    });
    if (width === 390)
      await page.screenshot({
        path: `${images}notifications-mobile.png`,
        animations: "disabled",
      });
    await inbox.focus();
    await expect(inbox).toBeFocused();
    await page.keyboard.press("PageDown");
    await expect
      .poll(() => inbox.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(0);
    expect(
      (await panel.locator(".notification-popover-head").boundingBox())!.y,
    ).toBe(headingTop);
    await page.keyboard.press("Escape");
    await expect(panel).not.toBeVisible();
    await expect(bell).toBeFocused();
    await bell.click();
    await expect(panel).toBeVisible();
    await page
      .getByRole("button", { name: "Закрыть уведомления", exact: true })
      .click();
    await expect(panel).not.toBeVisible();
  }
  expect(errors).toEqual([]);
});

test("notification panel follows an inset and resized visual viewport", async ({
  page,
}) => {
  const errors = await fixture(page);
  await page.setViewportSize({ width: 390, height: 640 });
  await page.addInitScript(() => {
    class Viewport extends EventTarget {
      width = 330;
      height = 480;
      offsetLeft = 30;
      offsetTop = 20;
    }
    Object.defineProperty(window, "visualViewport", {
      configurable: true,
      value: new Viewport(),
    });
  });
  await page.goto("/");
  await page.locator(".notification-anchor").evaluate((anchor) => {
    anchor.style.marginRight = "48px";
  });
  await page
    .getByRole("button", { name: /Уведомления, 0 непрочитанных/ })
    .click();
  const panel = page.locator(".notification-popover");
  await expect(panel).toBeVisible();
  const bounded = async (left: number, right: number, bottom: number) => {
    await expect
      .poll(async () => {
        const box = await panel.boundingBox();
        return Boolean(
          box &&
          box.x >= left &&
          box.x + box.width <= right &&
          box.y + box.height <= bottom,
        );
      })
      .toBe(true);
  };
  await bounded(42, 348, 488);
  await page.evaluate(() => {
    const viewport = window.visualViewport as unknown as {
      width: number;
      height: number;
      offsetLeft: number;
      offsetTop: number;
      dispatchEvent: (event: Event) => boolean;
    };
    Object.assign(viewport, {
      width: 290,
      height: 260,
      offsetLeft: 50,
      offsetTop: 100,
    });
    viewport.dispatchEvent(new Event("resize"));
    viewport.dispatchEvent(new Event("scroll"));
  });
  await bounded(62, 328, 348);
  await expect(
    page.getByRole("button", { name: "Закрыть уведомления", exact: true }),
  ).toBeInViewport();
  expect(errors).toEqual([]);
});
