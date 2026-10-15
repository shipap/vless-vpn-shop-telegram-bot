import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

async function fixture(page: Page) {
  const writes: { path: string; body: any }[] = [];
  const controls = { quoteFailure: false, orderFailure: false };
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  const legal = {
    configured: true,
    accepted: true,
    revision: "synthetic",
    missing: [],
    documents: [
      {
        document_type: "offer",
        version: "1",
        url: "https://example.test/offer",
      },
      {
        document_type: "privacy",
        version: "1",
        url: "https://example.test/privacy",
      },
    ],
  };
  const periods = [
    { days: 31, months: 1, discount_percent: "0", price: "123.45" },
    { days: 93, months: 3, discount_percent: "10", price: "321.09" },
    { days: 186, months: 6, discount_percent: "15", price: "654.32" },
    { days: 365, months: 12, discount_percent: "20", price: "987.65" },
  ];
  const products = [
    {
      id: "first",
      name: "Первый тариф",
      description: "Описание первого тарифа",
      currency: "RUB",
      available: true,
      tier_level: 1,
      periods,
      content: {},
      included_devices: 4,
    },
    {
      id: "second",
      name: "Второй тариф",
      description: "Описание второго тарифа",
      currency: "EUR",
      available: true,
      tier_level: 2,
      periods: [
        { days: 45, months: 2, discount_percent: "8.75", price: "47.89" },
      ],
      content: {},
      included_devices: 6,
    },
  ];
  await page.route("**/*", (route) =>
    ["127.0.0.1", "localhost"].includes(new URL(route.request().url()).hostname)
      ? route.continue()
      : route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const body =
      route.request().method() === "POST"
        ? route.request().postDataJSON()
        : null;
    if (body) writes.push({ path, body });
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
        capabilities: [],
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products,
        periods,
        content: {},
        legal,
        payment_methods: [
          {
            id: "crypto",
            label: "Криптовалюта",
            discount_percent: "7.5",
            available: true,
          },
          { id: "sbp", label: "СБП", discount_percent: "0", available: true },
        ],
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        credit_balance: "0",
        credit_reserved: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
      };
    if (path.startsWith("/legal")) json = legal;
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/promos/active") json = null;
    if (path === "/quotes") {
      if (controls.quoteFailure)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "temporarily_unavailable",
              message: "Расчёт временно недоступен. Повторите попытку.",
            },
          },
        });
      json = {
        id: `q-${writes.length}`,
        action: body.action,
        product_id: body.product_id,
        period_days: body.period_days,
        payment_method: {
          id: body.payment_method,
          label: body.payment_method === "crypto" ? "Криптовалюта" : "СБП",
          discount_percent: "0",
        },
        total: "41.23",
        currency: body.product_id === "second" ? "EUR" : "RUB",
        base_amount: "47.89",
        discount_amount: "6.66",
        bonus_amount: "0",
        credit_amount: "0",
        payment_method_discount_amount: "0",
        devices_limit: 6,
        resulting_expires_at: "2027-01-01T00:00:00Z",
        expires_at: "2099-01-01T00:00:00Z",
        breakdown: [],
      };
    }
    if (path === "/orders" || path === "/orders/order-synthetic") {
      if (controls.orderFailure && path === "/orders")
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "temporarily_unavailable",
              message: "Результат заказа проверяется. Повторите запрос.",
            },
          },
        });
      json = {
        id: "order-synthetic",
        status: "paid",
        provisioning_status: "pending",
        total: "41.23",
        currency: "EUR",
        environment: "sandbox",
        subscription_id: "sub-synthetic",
        checkout_url: null,
      };
    }
    await route.fulfill({ json });
  });
  await page.goto("/#home");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .first()
    .click();
  await page.getByRole("dialog").waitFor();
  return { writes, controls };
}

test("upfront server prices follow product, custom durations and currency before any quote", async ({
  page,
}) => {
  const { writes } = await fixture(page);
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Описание первого тарифа")).toBeVisible();
  await expect(
    dialog.getByRole("status").filter({ hasText: "Стоимость срока: 123,45 ₽" }),
  ).toBeVisible();
  for (const label of [
    "1 месяц (31 дн.) · 123,45 ₽",
    "3 месяца (93 дн.) −10% · 321,09 ₽",
    "6 месяцев (186 дн.) −15% · 654,32 ₽",
    "12 месяцев (365 дн.) −20% · 987,65 ₽",
  ])
    await expect(
      dialog.getByRole("option", { name: label, exact: true }),
    ).toHaveCount(1);
  await dialog
    .getByRole("combobox", { name: "Период", exact: true })
    .selectOption("365");
  await expect(
    dialog.getByRole("status").filter({ hasText: "Стоимость срока: 987,65 ₽" }),
  ).toBeVisible();
  await dialog
    .getByRole("combobox", { name: "Тариф", exact: true })
    .selectOption("second");
  await expect(dialog.getByText("Описание второго тарифа")).toBeVisible();
  await expect(
    dialog
      .getByRole("status")
      .filter({ hasText: "Стоимость срока: 47,89 EUR" }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("combobox", { name: "Период", exact: true }),
  ).toHaveValue("45");
  await expect(
    dialog.getByRole("option", {
      name: "2 месяца (45 дн.) −8,75% · 47,89 EUR",
      exact: true,
    }),
  ).toHaveCount(1);
  expect(writes).toEqual([]);
  for (const width of [320, 360, 390, 768, 1280]) {
    await page.setViewportSize({ width, height: 740 });
    const price = dialog
      .getByRole("status")
      .filter({ hasText: "Стоимость срока: 47,89 EUR" });
    await price.scrollIntoViewIfNeeded();
    const bounds = await price.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - innerWidth,
      ),
    ).toBeLessThanOrEqual(1);
    await page.screenshot({
      path: `.artifacts/website-functional-resume/checkout-price-${width}.png`,
    });
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("quote retry, back, method recalculation and order retry preserve server totals and operation identity", async ({
  page,
}) => {
  const { writes, controls } = await fixture(page);
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("combobox", { name: "Тариф", exact: true })
    .selectOption("second");
  await dialog.getByRole("radio", { name: /Криптовалюта/ }).check();
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  controls.quoteFailure = true;
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await expect(
    dialog.getByText("Расчёт временно недоступен. Повторите попытку."),
  ).toBeVisible();
  controls.quoteFailure = false;
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await expect(dialog.locator(".quote-total strong")).toHaveText("41,23 EUR");
  expect(writes.filter((w) => w.path === "/orders")).toEqual([]);
  await dialog.getByRole("button", { name: "Изменить", exact: true }).click();
  await expect(
    dialog.getByRole("combobox", { name: "Тариф", exact: true }),
  ).toHaveValue("second");
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await dialog.getByRole("radio", { name: "СБП", exact: true }).check();
  await expect(dialog.locator(".quote-total strong")).toHaveText("41,23 EUR");
  expect(writes.filter((w) => w.path === "/quotes").at(-1)?.body).toMatchObject(
    { product_id: "second", period_days: 45, payment_method: "sbp" },
  );
  controls.orderFailure = true;
  await dialog
    .getByRole("button", { name: "Подтвердить заказ", exact: true })
    .click();
  await expect(
    dialog.getByText("Результат заказа проверяется. Повторите запрос."),
  ).toBeVisible();
  controls.orderFailure = false;
  await dialog
    .getByRole("button", { name: "Подтвердить заказ", exact: true })
    .click();
  await expect(
    dialog.getByText(
      "Оплата подтверждена. Подготовка подключения продолжается.",
    ),
  ).toBeVisible();
  const orders = writes.filter((w) => w.path === "/orders");
  expect(orders).toHaveLength(2);
  expect(orders[1].body).toEqual(orders[0].body);
  expect(writes.filter((w) => w.path.includes("test-pay"))).toEqual([]);
});
