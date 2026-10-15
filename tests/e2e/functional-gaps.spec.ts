import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";
import { ownerStaffFixture } from "./staff-fixture";
import { readFile } from "node:fs/promises";

type Request = { path: string; method: string; body: any; query: string };
const legal = {
  configured: true,
  accepted: true,
  missing: [],
  revision: "synthetic",
  documents: [
    { document_type: "offer", version: "1", url: "https://example.test/offer" },
    {
      document_type: "privacy",
      version: "1",
      url: "https://example.test/privacy",
    },
  ],
};
const account = {
  id: "9007199254740993123",
  email: "synthetic@example.test",
  email_verified: true,
  telegram_id: null,
};
const products = [1, 2, 3].map((rank) => ({
  id: `tier-${rank}`,
  code: `tier-${rank}`,
  name: `Тариф ${rank}`,
  description: `Описание тарифа ${rank}`,
  currency: "RUB",
  price: "127.89",
  available: true,
  tier_level: rank,
  version: 1,
  included_devices: 4,
  content: {},
  periods: [
    { days: 31, months: 1, discount_percent: "0", price: "127.89" },
    { days: 93, months: 3, discount_percent: "9.5", price: "287.65" },
  ],
}));
const baseSubscription = {
  id: "subscription-synthetic",
  product_id: "tier-2",
  name: "Синтетическая подписка",
  status: "active",
  expires_at: "2027-02-01T00:00:00Z",
  devices_limit: 4,
  version: 1,
  connection_url: null,
  devices: [],
  segments: [],
  recurring: null,
};
function failure(message: string, code = "temporarily_unavailable") {
  return { status: 503, json: { error: { code, message } } };
}

async function setup(
  page: Page,
  options: {
    admin?: boolean;
    section?: string;
    recurring?: boolean;
    mfa?: boolean;
    enrolled?: boolean;
    mfaStatusError?: boolean;
    newsError?: boolean;
    compactNews?: boolean;
  } = {},
) {
  const requests: Request[] = [];
  const controls = {
    quoteError: false,
    orderError: false,
    cancelError: false,
    cancelled: false,
    newsError: options.newsError === true,
    analyticsError: false,
    securityError: false,
    mfaError: false,
    verified: options.mfa !== true,
    enrollError: false,
    historyError: false,
    revokeError: false,
    mfaStatusError: options.mfaStatusError === true,
  };
  const allowedWrites = [
    "/legal/accept",
    "/quotes",
    "/orders",
    "/subscriptions/subscription-synthetic/recurring/cancel",
    "/admin/mfa/enroll",
    "/admin/mfa/enroll/confirm",
    "/admin/mfa/verify",
    "/admin/security/block-synthetic/revoke",
  ];
  if (options.admin)
    await page.addInitScript((section) => {
      if (!sessionStorage.getItem("functional-gap-admin-entry")) {
        sessionStorage.setItem("functional-gap-admin-entry", "1");
        history.replaceState(null, "", `/admin#${section}`);
      }
    }, options.section ?? "overview");
  await page.route("**/*", (route) =>
    ["127.0.0.1", "localhost"].includes(new URL(route.request().url()).hostname)
      ? route.continue()
      : route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const method = route.request().method();
    const body = method !== "GET" ? route.request().postDataJSON() : null;
    requests.push({ path, method, body, query: url.search });
    if (method !== "GET")
      expect(allowedWrites, `Forbidden mutation ${path}`).toContain(path);
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: !!options.admin,
        mfa_verified: controls.verified,
        ...(options.admin
          ? ownerStaffFixture
          : { staff_role: null, capabilities: [] }),
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products,
        periods: products[0].periods,
        content: {},
        legal,
        payment_methods: [
          {
            id: "crypto",
            label: "Криптовалюта",
            discount_percent: "0",
            available: true,
          },
        ],
        capabilities: { recurring: false },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            ...baseSubscription,
            recurring:
              options.recurring && !controls.cancelled
                ? {
                    id: "agreement-synthetic",
                    state: "active",
                    amount: "127.89",
                    currency: "RUB",
                    next_charge_at: "2027-02-01T00:00:00Z",
                  }
                : null,
          },
        ],
        payments: [],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
        recurring_enrollment_enabled: false,
      };
    if (path.startsWith("/legal")) json = legal;
    if (path === "/promos/active") json = null;
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/subscriptions/subscription-synthetic/panel")
      json = {
        subscription_id: baseSubscription.id,
        source: "local",
        status: "ACTIVE",
        observed_at: null,
        error: null,
        traffic_bytes: null,
        traffic_limit_bytes: null,
        devices: [],
        paid_expires_at: baseSubscription.expires_at,
      };
    if (path === "/quotes") {
      if (controls.quoteError)
        return route.fulfill(failure("Расчёт недоступен. Повторите попытку."));
      json = {
        id: `quote-${requests.length}`,
        action: body.action,
        subscription_id: body.subscription_id,
        product_id: body.product_id,
        period_days: body.period_days,
        payment_method: {
          id: "crypto",
          label: "Криптовалюта",
          discount_percent: "0",
        },
        total: "17.43",
        currency: "RUB",
        base_amount: "22.71",
        discount_amount: "5.28",
        bonus_amount: "0",
        credit_amount: "0",
        devices_limit: body.action === "devices" ? 6 : 4,
        device_pricing:
          body.action === "devices"
            ? {
                extra_devices: body.extra_devices,
                amount: "17.43",
                mode: "fixed_remaining_term",
                billing_months: 1,
              }
            : null,
        resulting_expires_at: baseSubscription.expires_at,
        expires_at: "2099-01-01T00:00:00Z",
        breakdown: [],
      };
    }
    if (path === "/orders") {
      if (controls.orderError)
        return route.fulfill(failure("Заказ проверяется. Повторите запрос."));
      json = {
        id: "order-synthetic",
        status: "paid",
        provisioning_status: "ready",
        subscription_id: baseSubscription.id,
        total: "17.43",
        currency: "RUB",
        environment: "sandbox",
        checkout_url: null,
      };
    }
    if (path === "/subscriptions/subscription-synthetic/recurring/status")
      json = {
        loyalty: {
          percent: "0",
          next_milestone_at: null,
          applied_to_external_amount: false,
        },
        dynamic_amount_supported: false,
      };
    if (path === "/subscriptions/subscription-synthetic/recurring/cancel") {
      if (controls.cancelError)
        return route.fulfill(
          failure("Отмена не подтверждена. Повторите запрос."),
        );
      controls.cancelled = true;
      json = { state: "cancelled" };
    }
    if (path === "/node-news") {
      if (controls.newsError)
        return route.fulfill(failure("Новости недоступны. Повторите запрос."));
      const old = url.searchParams.get("before");
      json = {
        items:
          !old && options.compactNews
            ? Array.from({ length: 20 }, (_, index) => ({
                id: `synthetic-event-${index}`,
                title: `🇳🇱 Сервер #${(index % 4) + 1}`,
                state: index < 4 ? "recovered" : "outage",
                occurred_at: `2026-10-12T${String(20 - index).padStart(2, "0")}:00:00Z`,
              }))
            : [
                {
                  id: old ? "news-old" : "news-new",
                  title: old
                    ? "Синтетическое событие раньше"
                    : "Синтетическое восстановление",
                  state: old ? "outage" : "recovered",
                  occurred_at: "2026-10-12T10:00:00Z",
                },
              ],
        next_cursor: old ? null : "safe-cursor:+/",
      };
    }
    if (path === "/admin/mfa/status" && controls.mfaStatusError)
      return route.fulfill(
        failure("Проверка защиты недоступна. Повторите запрос."),
      );
    if (path === "/admin/mfa/status")
      json = {
        enrolled: options.enrolled === true,
        enrollment_required: options.enrolled !== true,
        trusted: controls.verified,
        trust_expires_at: null,
      };
    if (path === "/admin/mfa/enroll") {
      if (controls.enrollError)
        return route.fulfill(
          failure("Настройка недоступна. Повторите запрос."),
        );
      json = {
        secret: "SYNTHETICKEY",
        provisioning_uri:
          "otpauth://totp/example:synthetic?secret=JBSWY3DPEHPK3PXP&issuer=example",
        expires_at: new Date(Date.now() + 600000).toISOString(),
      };
    }
    if (path === "/admin/mfa/enroll/confirm" || path === "/admin/mfa/verify") {
      if (controls.mfaError)
        return route.fulfill({
          status: 400,
          json: {
            error: {
              code: "mfa_invalid",
              message: "Код дополнительной защиты неверен.",
            },
          },
        });
      controls.verified = true;
      json = { trusted: true };
    }
    if (path === "/admin/overview") {
      if (controls.analyticsError)
        return route.fulfill(failure("Отчёт недоступен. Повторите запрос."));
      const currency = url.searchParams.get("currency") ?? "RUB";
      json = {
        currency,
        timezone: url.searchParams.get("timezone") ?? "Europe/Moscow",
        mode: url.searchParams.get("mode") ?? "cash",
        paid_count: 1,
        currencies: { [currency]: "87.65" },
        refunds: { [currency]: "3.21" },
        confirmed_fees: {},
        net_after_refunds: { [currency]: "84.44" },
        previous: { [currency]: "12.34" },
        net_usdt: "0.91",
        active_paid_subscriptions: 2,
        series: [
          {
            date: "2026-10-12",
            currency,
            payments: "87.65",
            refunds: "3.21",
            fees: null,
          },
        ],
        rows: [
          {
            id: "fact-synthetic",
            date: "2026-10-12T10:00:00Z",
            amount: "87.65",
            currency,
            fee: null,
            fee_currency: null,
            source: "web",
            product_id: "tier-2",
            canonical_account_id: account.id,
            provenance: "synthetic",
          },
        ],
      };
    }
    if (path === "/admin/security") {
      if (controls.securityError)
        return route.fulfill(
          failure("Ограничения недоступны. Повторите запрос."),
        );
      json = {
        items: [
          {
            id: "block-synthetic",
            scope: "challenge",
            rule: "wrong_code",
            count: 10,
            threshold: 10,
            started_at: "2026-10-13T10:00:00Z",
            ends_at: "2026-10-13T11:00:00Z",
          },
        ],
        total: 2,
        page_size: 1,
      };
    }
    if (path === "/admin/security/logins") {
      if (controls.historyError)
        return route.fulfill(failure("История недоступна. Повторите запрос."));
      json = {
        items: [
          {
            id: `login-${url.searchParams.get("page")}`,
            created_at: "2026-10-13T10:00:00Z",
            email: "synthetic@example.test",
            account_id: account.id,
            result: "failed",
            reason: "wrong_code",
            ip: "192.0.2.1",
            ip_provenance: "synthetic",
          },
        ],
        total: 15,
      };
    }
    if (
      path === "/admin/security/block-synthetic/revoke" &&
      controls.revokeError
    )
      return route.fulfill(
        failure("Снятие ограничения не подтверждено. Повторите запрос."),
      );
    await route.fulfill({ json });
  });
  await page.goto(options.admin ? "/" : `/#${options.section ?? "home"}`);
  return {
    requests,
    controls,
    writes: () => requests.filter((item) => item.method !== "GET"),
  };
}

for (const action of ["upgrade", "downgrade", "devices"] as const)
  test(`${action}: quote and order next/back/cancel/error/retry are scoped to the existing subscription`, async ({
    page,
  }) => {
    const state = await setup(page, {
      section: action === "devices" ? "devices" : "home",
    });
    const open = page.getByRole("button", {
      name:
        action === "devices"
          ? "Добавить устройства"
          : action === "upgrade"
            ? "Повысить тариф"
            : "Понизить тариф",
      exact: action === "devices",
    });
    await open.click();
    let dialog = page.getByRole("dialog");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(state.writes()).toEqual([]);
    await open.click();
    dialog = page.getByRole("dialog");
    await expect(
      dialog.getByRole("combobox", { name: "Тариф", exact: true }),
    ).toHaveValue(
      action === "upgrade"
        ? "tier-3"
        : action === "downgrade"
          ? "tier-1"
          : "tier-2",
    );
    if (action === "devices") await dialog.getByRole("spinbutton").fill("2");
    else
      await dialog
        .getByRole("combobox", { name: "Период", exact: true })
        .selectOption("93");
    await dialog
      .getByRole("radio", { name: "Криптовалюта", exact: true })
      .check();
    await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
    state.controls.quoteError = true;
    await dialog
      .getByRole("button", { name: "Рассчитать стоимость", exact: true })
      .click();
    await expect(
      dialog.getByText("Расчёт недоступен. Повторите попытку."),
    ).toBeVisible();
    state.controls.quoteError = false;
    await dialog
      .getByRole("button", { name: "Рассчитать стоимость", exact: true })
      .click();
    await expect(dialog.locator(".quote-total strong")).toHaveText("17,43 ₽");
    const expected = {
      action,
      subscription_id: baseSubscription.id,
      product_id:
        action === "upgrade"
          ? "tier-3"
          : action === "downgrade"
            ? "tier-1"
            : "tier-2",
      extra_devices: action === "devices" ? 2 : 0,
      period_days: action === "devices" ? 31 : 93,
    };
    expect(
      state
        .writes()
        .filter((r) => r.path === "/quotes")
        .at(-1)?.body,
    ).toMatchObject(expected);
    expect(state.writes().filter((r) => r.path === "/orders")).toEqual([]);
    await dialog.getByRole("button", { name: "Изменить", exact: true }).click();
    if (action === "devices")
      await expect(dialog.getByRole("spinbutton")).toHaveValue("2");
    else
      await expect(
        dialog.getByRole("combobox", { name: "Период", exact: true }),
      ).toHaveValue("93");
    await dialog
      .getByRole("button", { name: "Рассчитать стоимость", exact: true })
      .click();
    state.controls.orderError = true;
    await dialog
      .getByRole("button", { name: "Подтвердить заказ", exact: true })
      .click();
    await expect(
      dialog.getByText("Заказ проверяется. Повторите запрос."),
    ).toBeVisible();
    state.controls.orderError = false;
    await dialog
      .getByRole("button", { name: "Подтвердить заказ", exact: true })
      .click();
    await expect(
      dialog.getByText("Оплата подтверждена. Доступ готов. Откройте подписку."),
    ).toBeVisible();
    const orders = state.writes().filter((r) => r.path === "/orders");
    expect(orders).toHaveLength(2);
    expect(orders[1].body).toEqual(orders[0].body);
  });

test("recurring enrollment OFF preserves individual cancellation, cancel/back and error retry", async ({
  page,
}) => {
  const state = await setup(page, { recurring: true });
  const button = page.getByRole("button", {
    name: "Отменить автопродление",
    exact: true,
  });
  await button.click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Оставить как есть", exact: true })
    .click();
  expect(state.writes()).toEqual([]);
  await expect(button).toBeVisible();
  await button.click();
  state.controls.cancelError = true;
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByText("Отмена не подтверждена. Повторите запрос."),
  ).toBeVisible();
  state.controls.cancelError = false;
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(button).toHaveCount(0);
  expect(state.writes().map((r) => r.path)).toEqual([
    "/subscriptions/subscription-synthetic/recurring/cancel",
    "/subscriptions/subscription-synthetic/recurring/cancel",
  ]);
});

test("server news renders scoped populated pages, retry, earlier events and back without alerts", async ({
  page,
}) => {
  const state = await setup(page, { section: "news", newsError: true });
  await expect(
    page.getByRole("button", { name: "Обновить", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Новости недоступны. Повторите запрос."),
  ).toBeVisible();
  state.controls.newsError = false;
  await page
    .getByRole("button", { name: "Попробовать снова", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Синтетическое восстановление",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page
      .locator(".node-news-latest")
      .getByText("Работа восстановлена", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Более ранние события", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Синтетическое событие раньше",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page
      .locator(".node-news-latest")
      .getByText("Перебои в работе", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/Состояние указано на момент последнего события/),
  ).toBeVisible();
  expect(
    new URLSearchParams(
      state.requests.filter((r) => r.path === "/node-news").at(-1)!.query,
    ).get("before"),
  ).toBe("safe-cursor:+/");
  await page.getByRole("button", { name: "Назад", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Синтетическое восстановление",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.writes()).toEqual([]);
  await expect(
    page.getByRole("status").filter({ hasText: "Синтетическое" }),
  ).toHaveCount(0);
});

for (const width of [390, 1280]) {
  test(`server news compacts twenty events into four keyboard-expandable server groups at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await setup(page, { section: "news", compactNews: true });
    const groups = page.locator(".node-news-server");
    await expect(groups).toHaveCount(4);
    await expect(page.locator(".node-news-history[open]")).toHaveCount(0);
    await expect(groups.first().getByRole("heading")).toHaveText(
      "🇳🇱 Сервер #1",
    );
    await expect(groups.first().locator(".node-news-latest .badge")).toHaveText(
      "Работа восстановлена",
    );
    const summary = groups.first().locator("summary");
    await summary.focus();
    await page.keyboard.press("Enter");
    await expect(groups.first().locator("details")).toHaveAttribute("open", "");
    const events = groups.first().locator("ol > li");
    await expect(events).toHaveCount(5);
    await expect(
      events.first().getByText("Работа восстановлена", { exact: true }),
    ).toBeVisible();
    await expect(
      events.last().getByText("Перебои в работе", { exact: true }),
    ).toBeVisible();
    await expect(events.first().locator("time")).toHaveAttribute(
      "datetime",
      "2026-10-12T20:00:00Z",
    );
    await expect(events.last().locator("time")).toHaveAttribute(
      "datetime",
      "2026-10-12T04:00:00Z",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `.artifacts/w16-news-${width}.png`,
      fullPage: true,
    });
    await page.keyboard.press("Enter");
    await expect(page.locator(".node-news-history[open]")).toHaveCount(0);
    expect(state.writes()).toEqual([]);
  });
}

test("MFA enrollment error and invalid code retry preserve setup then return to authenticated admin", async ({
  page,
}) => {
  const state = await setup(page, { admin: true, mfa: true });
  state.controls.enrollError = true;
  await page
    .getByRole("button", { name: "Настроить аутентификатор", exact: true })
    .click();
  await expect(
    page.getByText("Настройка недоступна. Повторите запрос."),
  ).toBeVisible();
  state.controls.enrollError = false;
  await page
    .getByRole("button", { name: "Настроить аутентификатор", exact: true })
    .click();
  await expect(
    page.getByRole("img", { name: "QR для приложения-аутентификатора" }),
  ).toBeVisible();
  await page.getByLabel("Код аутентификатора", { exact: true }).fill("123456");
  state.controls.mfaError = true;
  await page
    .getByRole("button", { name: "Включить защиту", exact: true })
    .click();
  await expect(
    page.getByText("Код дополнительной защиты неверен."),
  ).toBeVisible();
  state.controls.mfaError = false;
  await page.getByLabel("Код аутентификатора", { exact: true }).fill("654321");
  await page
    .getByRole("button", { name: "Включить защиту", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "CSV", exact: true }),
  ).toBeVisible();
  expect(state.writes().map((r) => r.path)).toEqual([
    "/admin/mfa/enroll",
    "/admin/mfa/enroll",
    "/admin/mfa/enroll/confirm",
    "/admin/mfa/enroll/confirm",
  ]);
});

test("MFA back to cabinet cancels setup without confirming enrollment", async ({
  page,
}) => {
  const state = await setup(page, { admin: true, mfa: true });
  await page
    .getByRole("button", { name: "Настроить аутентификатор", exact: true })
    .click();
  await expect(
    page.getByRole("img", { name: "QR для приложения-аутентификатора" }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Вернуться в кабинет", exact: true })
    .click();
  await expect(
    page
      .getByRole("button", { name: "Добавить подписку", exact: true })
      .first(),
  ).toBeVisible();
  expect(state.writes().filter((r) => r.path.includes("confirm"))).toEqual([]);
});

test("admin analytics filters retry and CSV preserve applied currency and server facts", async ({
  page,
}) => {
  const state = await setup(page, { admin: true });
  await expect(
    page.getByRole("button", { name: "CSV", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Валюта", exact: true })
    .selectOption("EUR");
  await page
    .getByRole("combobox", { name: "Часовой пояс", exact: true })
    .selectOption("UTC");
  await page
    .getByRole("combobox", { name: "Группировка", exact: true })
    .selectOption("week");
  await page
    .getByRole("combobox", { name: "Источник", exact: true })
    .selectOption("web");
  await page
    .getByLabel("ID тарифа · необязательно", { exact: true })
    .fill("tier-2");
  await page.getByLabel("С даты", { exact: true }).fill("2026-10-01");
  await page
    .getByLabel("До даты · не включая", { exact: true })
    .fill("2026-10-13");
  state.controls.analyticsError = true;
  await page.getByRole("button", { name: "Применить", exact: true }).click();
  await expect(
    page.getByText("Отчёт недоступен. Повторите запрос."),
  ).toBeVisible();
  state.controls.analyticsError = false;
  await page
    .getByRole("button", { name: "Попробовать снова", exact: true })
    .click();
  await expect(page.locator(".metric strong").first()).toHaveText("87,65 EUR");
  const query = new URLSearchParams(
    state.requests.filter((r) => r.path === "/admin/overview").at(-1)!.query,
  );
  expect(Object.fromEntries(query)).toMatchObject({
    currency: "EUR",
    timezone: "UTC",
    interval: "week",
    source: "web",
    product_id: "tier-2",
    date_from: "2026-10-01T00:00:00Z",
    date_to: "2026-10-13T00:00:00Z",
  });
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "CSV", exact: true }).click();
  const download = await downloadPromise;
  const csv = await readFile((await download.path())!, "utf8");
  expect(csv).toContain('"87.65";"EUR";"Неизвестно";"Неизвестно"');
  expect(csv).toContain(account.id);
  expect(csv).not.toContain('"RUB"');
  expect(state.writes()).toEqual([]);
});

test("admin security selected unblock cancel, MFA failure and mutation retry never unblocks other context", async ({
  page,
}) => {
  const state = await setup(page, { admin: true, section: "security" });
  await page.getByRole("button", { name: "Снять", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Отмена", exact: true })
    .click();
  expect(state.writes()).toEqual([]);
  await page.getByRole("button", { name: "Снять", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Причина изменения", { exact: true })
    .fill("Синтетическая проверка ложного ограничения");
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog.getByRole("checkbox").check();
  state.controls.mfaError = true;
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(
    dialog.getByText("Код дополнительной защиты неверен."),
  ).toBeVisible();
  expect(state.writes().filter((r) => r.path.endsWith("revoke"))).toEqual([]);
  state.controls.mfaError = false;
  state.controls.revokeError = true;
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("654321");
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(
    dialog.getByText("Снятие ограничения не подтверждено. Повторите запрос."),
  ).toBeVisible();
  state.controls.revokeError = false;
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("456789");
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const revoked = state.writes().filter((r) => r.path.endsWith("revoke"));
  expect(revoked).toHaveLength(2);
  expect(revoked[1].body).toEqual(revoked[0].body);
  expect(revoked[1].body).toMatchObject({
    confirmed: true,
    reason: "Синтетическая проверка ложного ограничения",
  });
});

test("enrolled MFA verification rejects wrong code, retries and enters admin without a new enrollment", async ({
  page,
}) => {
  const state = await setup(page, { admin: true, mfa: true, enrolled: true });
  await expect(
    page.getByRole("button", { name: "Настроить аутентификатор", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Код аутентификатора", { exact: true }).fill("123456");
  state.controls.mfaError = true;
  await page
    .getByRole("button", { name: "Подтвердить вход", exact: true })
    .click();
  await expect(
    page.getByText("Код дополнительной защиты неверен."),
  ).toBeVisible();
  state.controls.mfaError = false;
  await page.getByLabel("Код аутентификатора", { exact: true }).fill("654321");
  await page
    .getByRole("button", { name: "Подтвердить вход", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "CSV", exact: true }),
  ).toBeVisible();
  expect(state.writes().map((r) => r.path)).toEqual([
    "/admin/mfa/verify",
    "/admin/mfa/verify",
  ]);
});

test("security list and login history read errors retry independently, scoped search and pagination return back", async ({
  page,
}) => {
  const state = await setup(page, { admin: true, section: "security" });
  await page.getByRole("button", { name: "Далее", exact: true }).click();
  await expect
    .poll(() =>
      new URLSearchParams(
        state.requests.filter((r) => r.path === "/admin/security").at(-1)!
          .query,
      ).get("page"),
    )
    .toBe("2");
  await page.getByRole("button", { name: "Назад", exact: true }).click();
  await expect
    .poll(() =>
      new URLSearchParams(
        state.requests.filter((r) => r.path === "/admin/security").at(-1)!
          .query,
      ).get("page"),
    )
    .toBe("1");
  const history = page.locator(".login-history");
  await expect(
    history.getByText("Всего входов: 15 · страница 1", { exact: true }),
  ).toBeVisible();
  await history
    .getByRole("button", { name: "Далее по истории", exact: true })
    .click();
  await expect(
    history.getByText("Всего входов: 15 · страница 2", { exact: true }),
  ).toBeVisible();
  await history.getByRole("button", { name: "Ранее", exact: true }).click();
  await expect(
    history.getByText("Всего входов: 15 · страница 1", { exact: true }),
  ).toBeVisible();
  await history
    .getByLabel("Поиск в истории входов", { exact: true })
    .fill(account.id);
  await history
    .getByRole("button", { name: "Найти входы", exact: true })
    .click();
  await expect
    .poll(() =>
      new URLSearchParams(
        state.requests
          .filter((r) => r.path === "/admin/security/logins")
          .at(-1)!.query,
      ).get("search"),
    )
    .toBe(account.id);
  state.controls.historyError = true;
  await history
    .getByRole("button", { name: "Обновить историю", exact: true })
    .click();
  await expect(
    history.getByText("История недоступна. Повторите запрос."),
  ).toBeVisible();
  state.controls.historyError = false;
  await history
    .getByRole("button", { name: "Попробовать снова", exact: true })
    .click();
  await expect(history.getByText(account.id, { exact: true })).toBeVisible();
  await page
    .getByLabel("Поиск записей", { exact: true })
    .fill("block-synthetic");
  state.controls.securityError = true;
  await page.getByRole("button", { name: "Найти", exact: true }).click();
  await expect(
    page.getByText("Ограничения недоступны. Повторите запрос."),
  ).toBeVisible();
  state.controls.securityError = false;
  await page
    .getByRole("button", { name: "Попробовать снова", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Снять", exact: true }),
  ).toBeVisible();
  expect(
    new URLSearchParams(
      state.requests.filter((r) => r.path === "/admin/security").at(-1)!.query,
    ).get("search"),
  ).toBe("block-synthetic");
  await page.getByRole("button", { name: "Открыть", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.writes()).toEqual([]);
});

test("MFA status read error retries without creating enrollment until the explicit setup action", async ({
  page,
}) => {
  const state = await setup(page, {
    admin: true,
    mfa: true,
    mfaStatusError: true,
  });
  await expect(
    page.getByText("Проверка защиты недоступна. Повторите запрос."),
  ).toBeVisible();
  expect(state.writes()).toEqual([]);
  state.controls.mfaStatusError = false;
  await page
    .getByRole("button", { name: "Попробовать снова", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Настроить аутентификатор", exact: true }),
  ).toBeVisible();
  expect(state.writes()).toEqual([]);
});
