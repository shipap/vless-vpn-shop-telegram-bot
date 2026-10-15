import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

test.use({ timezoneId: "America/Los_Angeles" });

const account = {
  id: "9007199254740993123",
  email: "promo@example.test",
  email_verified: true,
  telegram_id: null,
};
const documents = (version: string) => [
  {
    document_type: "offer",
    version,
    url: `https://example.test/offer-${version}`,
  },
  {
    document_type: "privacy",
    version,
    url: `https://example.test/privacy-${version}`,
  },
];
const product = {
  id: "synthetic-tariff",
  name: "Синтетический тариф",
  description: "Локальная проверка",
  price: "200.00",
  currency: "RUB",
  tier_level: 1,
  available: true,
  included_devices: 4,
  version: 1,
  content: {},
  base_period_days: 30,
  periods: [{ days: 30, months: 1, discount_percent: "0", price: "200.00" }],
};
const gift = {
  code: "GIFT",
  state: "pending",
  reward_type: "free_days",
  percent: "0",
  amount_rub: null,
  reward_days: 7,
  product_id: product.id,
  available: true,
  unavailable_reason: null,
};
const subscription = {
  id: "9007199254740993998",
  name: "Личная подписка",
  product_id: product.id,
  status: "active",
  expires_at: "2099-12-01T00:00:00Z",
  devices_limit: 4,
  version: 1,
  connection_url: null,
  devices: [],
  segments: [],
  recurring: null,
};

async function fixture(
  page: Page,
  options: {
    pending?: Record<string, unknown>;
    existing?: boolean;
    staleLegal?: boolean;
    staleOrder?: boolean;
    activeFailure?: boolean;
  } = {},
) {
  let pending: Record<string, unknown> | null = options.pending || null;
  let legalVersion = "v1";
  let stale = Boolean(options.staleLegal);
  let activeFailure = Boolean(options.activeFailure);
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const legal = () => ({
      configured: true,
      revision: legalVersion,
      documents: documents(legalVersion),
    });
    if (request.method() !== "GET") {
      const body = request.postDataJSON() || {};
      writes.push({ path, body });
      if (path === "/legal/accept") {
        if (!["registration", "checkout", "first_use"].includes(body.context))
          return route.fulfill({
            status: 422,
            json: {
              error: {
                code: "validation_error",
                message: "Недопустимый контекст согласия",
              },
            },
          });
        if (stale) {
          stale = false;
          legalVersion = "v2";
          return route.fulfill({
            status: 409,
            json: {
              error: {
                code: "legal_versions_stale",
                message: "Документы обновились. Прочитайте текущие версии.",
              },
            },
          });
        }
        return route.fulfill({
          json: { ...legal(), accepted: true, missing: [], history: [] },
        });
      }
      if (path === "/promos/activate") {
        if (!/^[A-Za-z0-9_-]+$/.test(String(body.code)))
          return route.fulfill({
            status: 422,
            json: {
              error: {
                code: "validation_error",
                message: "Проверьте символы промокода",
              },
            },
          });
        pending = {
          ...gift,
          code: String(body.code),
          reward_type: "percent",
          percent: "15",
          reward_days: null,
          product_id: null,
        };
        return route.fulfill({ json: pending });
      }
      if (path === "/orders" && options.staleOrder) {
        legalVersion = "v2";
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: "legal_consent_required",
              message:
                "Ознакомьтесь с текущими документами и подтвердите согласие.",
            },
          },
        });
      }
      if (path === "/promos/claim") {
        pending = null;
        return route.fulfill({
          json: {
            code: "GIFT",
            state: "used",
            subscription_id: body.subscription_id || "synthetic-new",
            reward_days: 7,
            expires_at: "2099-12-08T21:15:00Z",
            provisioning_state: "pending",
          },
        });
      }
      if (path === "/quotes")
        return route.fulfill({
          json: {
            id: "synthetic-quote",
            action: body.action,
            product_id: product.id,
            subscription_id: body.subscription_id || null,
            base_amount: "200.00",
            discount_amount:
              pending?.reward_type === "fixed_rub" ? "25.00" : "30.00",
            promo_discount_amount:
              pending?.reward_type === "fixed_rub" ? "25.00" : "30.00",
            period_discount_amount: "0.00",
            secondary_discount_amount: "0.00",
            bonus_amount: "0.00",
            credit_amount: "0.00",
            credit_currency: "RUB",
            total: pending?.reward_type === "fixed_rub" ? "175.00" : "170.00",
            currency: "RUB",
            resulting_expires_at: "2099-12-08T21:15:00Z",
            devices_limit: 4,
            expires_at: "2099-12-08T21:15:00Z",
            version: 1,
            promo_code: pending?.code || null,
            breakdown: [
              {
                label: "Промокод",
                amount:
                  pending?.reward_type === "fixed_rub" ? "25.00" : "30.00",
              },
            ],
          },
        });
      return route.fulfill({ json: {} });
    }
    if (path === "/promos/active" && activeFailure) {
      activeFailure = false;
      return route.fulfill({
        status: 503,
        json: {
          error: {
            code: "protection_unavailable",
            message: "Сервис промокодов временно недоступен",
          },
        },
      });
    }
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
                periods: product.periods,
                legal: legal(),
                content: {},
                recurring_enrollment_enabled: false,
              }
            : ["/legal", "/legal/status"].includes(path)
              ? { ...legal(), accepted: true, missing: [], history: [] }
              : path === "/dashboard"
                ? {
                    account,
                    subscriptions: options.existing ? [subscription] : [],
                    payments: [],
                    bonus_balance: "0.00",
                    bonus_reserved: "0.00",
                    credit_balance: "0.00",
                    credit_reserved: "0.00",
                    credit_available: "0.00",
                    credit_currency: "RUB",
                    credit_provenance: "synthetic",
                    referrer_id: null,
                    recurring_enrollment_enabled: false,
                    active_promo: pending,
                  }
                : path === "/promos/active"
                  ? pending
                  : {};
    return route.fulfill({ json });
  });
  return writes;
}

test("gift claim uses the supported consent contract and preserves the existing BIGINT subscription", async ({
  page,
}) => {
  const writes = await fixture(page, { pending: gift, existing: true });
  await page.goto("/#account");
  const promo = page.getByRole("region", { name: "Промокод", exact: true });
  await expect(
    promo.getByRole("button", { name: "Получить подарочные дни" }),
  ).toBeDisabled();
  await promo.getByRole("checkbox").check();
  await promo.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(promo.getByRole("status")).toContainText("Дни начислены");
  await expect(promo.getByRole("status")).toContainText(
    "9 декабря 2099 г. в 00:15",
  );
  expect(writes.find((write) => write.path === "/legal/accept")?.body).toEqual({
    documents: documents("v1"),
    context: "checkout",
  });
  expect(writes.find((write) => write.path === "/promos/claim")?.body).toEqual({
    code: "GIFT",
    subscription_id: "9007199254740993998",
  });
});

test("gift claim reloads stale legal documents and requires fresh explicit consent", async ({
  page,
}) => {
  const writes = await fixture(page, { pending: gift, staleLegal: true });
  await page.goto("/#account");
  const promo = page.getByRole("region", { name: "Промокод", exact: true });
  await promo.getByRole("checkbox").check();
  await promo.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(
    promo.getByText("Документы обновились. Прочитайте текущие версии."),
  ).toBeVisible();
  await expect(promo.getByRole("checkbox")).not.toBeChecked();
  await expect(
    promo.getByRole("link", { name: "Публичная оферта" }),
  ).toHaveAttribute("href", "https://example.test/offer-v2");
  await expect(
    promo.getByRole("button", { name: "Получить подарочные дни" }),
  ).toBeDisabled();
  expect(writes.filter((write) => write.path === "/promos/claim")).toHaveLength(
    0,
  );
  await promo.getByRole("checkbox").check();
  await promo.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(promo.getByRole("status")).toContainText("Дни начислены");
  expect(
    writes.filter((write) => write.path === "/legal/accept").at(-1)?.body
      .documents,
  ).toEqual(documents("v2"));
});

test("checkout reloads stale legal documents without accepting them on the user's behalf", async ({
  page,
}) => {
  const writes = await fixture(page, { staleLegal: true });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await expect(
    dialog.getByText("Документы обновились. Прочитайте текущие версии."),
  ).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", { name: /Принимаю оферту/ }),
  ).not.toBeChecked();
  await expect(
    dialog.getByRole("link", { name: "Публичная оферта" }),
  ).toHaveAttribute("href", "https://example.test/offer-v2");
  expect(writes.filter((write) => write.path === "/quotes")).toHaveLength(0);
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await expect(
    dialog.getByRole("button", { name: "Подтвердить заказ" }),
  ).toBeVisible();
});

test("invalid promo syntax stays in the editable input without an activation request", async ({
  page,
}) => {
  const writes = await fixture(page);
  await page.goto("/");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("link", { name: "Активировать промокод в профиле" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(/#account$/);
  const promo = page.getByRole("region", { name: "Промокод", exact: true });
  const input = promo.getByRole("textbox", { name: "Промокод", exact: true });
  await input.fill("BAD CODE");
  await promo.getByRole("button", { name: "Активировать промокод" }).click();
  expect(
    await input.evaluate(
      (element: HTMLInputElement) => element.validity.patternMismatch,
    ),
  ).toBe(true);
  await expect(input).toHaveValue("BAD CODE");
  expect(
    writes.filter((write) => write.path === "/promos/activate"),
  ).toHaveLength(0);
  await input.fill("  GOOD-CODE_1  ");
  await promo.getByRole("button", { name: "Активировать промокод" }).click();
  await expect(promo.getByText("GOOD-CODE_1", { exact: true })).toBeVisible();
});

test("legal update after a quote returns checkout to explicit consent and a new calculation", async ({
  page,
}) => {
  const writes = await fixture(page, { staleOrder: true });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await dialog.getByRole("button", { name: "Подтвердить заказ" }).click();
  await expect(
    dialog.getByRole("button", { name: "Рассчитать стоимость" }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", { name: /Принимаю оферту/ }),
  ).not.toBeChecked();
  await expect(
    dialog.getByRole("link", { name: "Публичная оферта" }),
  ).toHaveAttribute("href", "https://example.test/offer-v2");
  await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await dialog.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await expect(
    dialog.getByRole("button", { name: "Подтвердить заказ" }),
  ).toBeVisible();
  expect(writes.filter((write) => write.path === "/quotes")).toHaveLength(2);
  expect(writes.filter((write) => write.path === "/orders")).toHaveLength(1);
});

test("access deadline stays on the Moscow calendar day in a different browser timezone", async ({
  page,
}) => {
  await fixture(page, { existing: true });
  await page.goto("/");
  await expect(
    page
      .locator(".subscription-meta")
      .getByText("1 декабря 2099 г.", { exact: true }),
  ).toBeVisible();
});

test("unavailable promo status blocks activation until explicit retry succeeds", async ({
  page,
}) => {
  const writes = await fixture(page, { activeFailure: true });
  await page.goto("/#account");
  const promo = page.getByRole("region", { name: "Промокод", exact: true });
  await expect(
    promo.getByText("Сервис промокодов временно недоступен"),
  ).toBeVisible();
  await expect(
    promo.getByRole("textbox", { name: "Промокод", exact: true }),
  ).toBeDisabled();
  await expect(
    promo.getByRole("button", { name: "Активировать промокод" }),
  ).toBeDisabled();
  expect(writes).toHaveLength(0);
  await promo.getByRole("button", { name: "Попробовать снова" }).click();
  await expect(
    promo.getByRole("textbox", { name: "Промокод", exact: true }),
  ).toBeEnabled();
  await promo
    .getByRole("textbox", { name: "Промокод", exact: true })
    .fill("RECOVERED");
  await promo.getByRole("button", { name: "Активировать промокод" }).click();
  await expect(promo.getByText("RECOVERED", { exact: true })).toBeVisible();
});

for (const reward of [
  {
    type: "percent",
    code: "PERCENT",
    percent: "15",
    amount: null,
    description: "Скидка в процентах: 15%",
    total: "170 ₽",
  },
  {
    type: "fixed_rub",
    code: "FIXED",
    percent: "0",
    amount: "25.00",
    description: "Скидка в рублях: 25 ₽",
    total: "175 ₽",
  },
]) {
  test(`${reward.type} pending promo renders the server quote, zero-bonus choices and Moscow dates without mobile overflow`, async ({
    page,
  }) => {
    await mkdir(".artifacts/ui/v10-promo-ux", { recursive: true });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const writes = await fixture(page, {
      pending: {
        ...gift,
        code: reward.code,
        reward_type: reward.type,
        percent: reward.percent,
        amount_rub: reward.amount,
        reward_days: null,
        product_id: null,
      },
    });
    for (const width of [320, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/#account");
      const promo = page.getByRole("region", { name: "Промокод", exact: true });
      await expect(
        promo.getByText(reward.description, { exact: true }),
      ).toBeVisible();
      await expect(
        promo.getByRole("textbox", { name: "Промокод", exact: true }),
      ).toHaveCount(0);
      await page.reload();
      await expect(promo.getByText(reward.code, { exact: true })).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
      ).toBe(false);
      await page.screenshot({
        path: `.artifacts/ui/v10-promo-ux/${reward.type}-account-${width}.png`,
        fullPage: true,
      });
      await page.goto("/");
      await page
        .getByRole("button", { name: "Добавить подписку", exact: true })
        .click();
      const dialog = page.getByRole("dialog");
      await expect(
        dialog.getByText(`Активный промокод: ${reward.code}`, { exact: true }),
      ).toBeVisible();
      await expect(dialog.getByRole("textbox", { name: /бонус/i })).toHaveCount(
        0,
      );
      await expect(
        dialog.getByRole("button", { name: "Рассчитать стоимость" }),
      ).toBeDisabled();
      await dialog.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
      await dialog
        .getByRole("button", { name: "Рассчитать стоимость" })
        .click();
      await expect(
        dialog.getByText(reward.total, { exact: true }),
      ).toBeVisible();
      await expect(
        dialog.getByText(`Промокод ${reward.code}`, { exact: true }),
      ).toBeVisible();
      await expect(
        dialog.getByText("Скидка за период", { exact: true }),
      ).toBeVisible();
      const stepHeader = await dialog.evaluate((element) => ({
        dialogTop: element.getBoundingClientRect().top,
        headerTop: element
          .querySelector(".dialog-head")!
          .getBoundingClientRect().top,
      }));
      expect(stepHeader.headerTop).toBeGreaterThanOrEqual(stepHeader.dialogTop);
      await expect(
        dialog.getByText("9 декабря 2099 г. в 00:15", { exact: true }).first(),
      ).toBeVisible();
      const quoteRequest = writes
        .filter((write) => write.path === "/quotes")
        .at(-1)!;
      expect(quoteRequest.body.bonus_amount).toBe("0.00");
      expect(quoteRequest.body).not.toHaveProperty("promo_code");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        ),
      ).toBe(false);
      const accessibility = await new AxeBuilder({ page })
        .include("dialog")
        .analyze();
      expect(
        accessibility.violations.filter((violation) =>
          ["serious", "critical"].includes(violation.impact || ""),
        ),
      ).toEqual([]);
      await page.screenshot({
        path: `.artifacts/ui/v10-promo-ux/${reward.type}-quote-${width}.png`,
        fullPage: true,
      });
    }
    expect(writes.filter((write) => write.path === "/orders")).toHaveLength(0);
    expect(errors).toEqual([]);
  });
}
