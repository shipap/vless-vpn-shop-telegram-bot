import { syntheticInstance } from "./instance-fixture";
import { expect, test } from "@playwright/test";

test("checkout requires explicit consent and uses the selected tariff periods", async ({
  page,
}) => {
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
  const legal = { configured: true, revision: "v1", documents };
  const account = {
    id: "9007199254740993",
    email: "customer@example.test",
    email_verified: true,
  };
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (request.method() !== "GET") {
      writes.push({ path, body: request.postDataJSON() });
      return route.fulfill(
        path === "/legal/accept"
          ? { json: { ...legal, accepted: true } }
          : {
              status: 422,
              json: {
                error: { code: "fixture", message: "Тестовый расчёт получен" },
              },
            },
      );
    }
    const product = {
      id: "custom",
      code: "custom",
      name: "На 28 дней",
      description: "Тестовый тариф",
      price: "149.00",
      currency: "RUB",
      tier_level: 1,
      available: true,
      included_devices: 4,
      version: 1,
      content: {},
      base_period_days: 28,
      periods: [
        { days: 28, months: 1, discount_percent: "0", price: "149.00" },
      ],
    };
    const json =
      path === "/auth/me"
        ? {
            account,
            csrf_token: "synthetic-csrf",
            is_admin: false,
            mfa_verified: false,
          }
        : path === "/public-config"
          ? {
              instance: syntheticInstance,
              local_test: true,
              turnstile_test_bypass: true,
              capabilities: { payments: false, recurring: false },
            }
          : path === "/catalog"
            ? {
                products: [product],
                periods: [{ days: 30, months: 1, discount_percent: "0" }],
                legal,
                content: {},
              }
            : path === "/legal/status"
              ? { ...legal, accepted: true, missing: [], history: [] }
              : path === "/dashboard"
                ? {
                    account,
                    subscriptions: [],
                    payments: [],
                    bonus_balance: "0.00",
                    bonus_reserved: "0.00",
                    credit_balance: "0.00",
                    credit_reserved: "0.00",
                    credit_available: "0.00",
                    credit_currency: "RUB",
                    credit_provenance: "synthetic",
                    recurring_enrollment_enabled: false,
                  }
                : {};
    return route.fulfill({ json });
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("button", { name: "Рассчитать стоимость" }),
  ).toBeDisabled();
  await expect(dialog.getByRole("combobox", { name: /^Период/ })).toHaveValue(
    "28",
  );
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await expect
    .poll(() => writes.map((write) => write.path))
    .toEqual(["/legal/accept", "/quotes"]);
  expect(writes[0].body).toEqual({ documents, context: "checkout" });
  expect(writes[1].body.period_days).toBe(28);
});

test("вход требует явного согласия с документами", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      status: 401,
      json: { error: { code: "unauthenticated" } },
    }),
  );
  await page.route("**/api/v1/public-config", (route) =>
    route.fulfill({
      json: {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: true,
      },
    }),
  );
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({ json: { products: [], periods: [] } }),
  );
  await page.route("**/api/v1/legal", (route) =>
    route.fulfill({
      json: {
        configured: true,
        revision: "test-v1",
        documents: [
          {
            document_type: "offer",
            version: "v1",
            url: "https://telegra.ph/Publichnaya-Oferta-servisa-sunrise-SHOP-03-25",
          },
          {
            document_type: "privacy",
            version: "v1",
            url: "https://telegra.ph/Politika-Konfidencialnosti-sunrise-SHOP-03-25",
          },
        ],
      },
    }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("link", { name: "Публичная оферта", exact: true }).first(),
  ).toBeVisible();
  await expect(
    page
      .getByRole("link", { name: "Политика конфиденциальности", exact: true })
      .first(),
  ).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: /Принимаю оферту/ }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: /Получить код/ }),
  ).toBeDisabled();
  await page.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await expect(
    page.getByRole("button", { name: /Получить код/ }),
  ).toBeEnabled();
});
