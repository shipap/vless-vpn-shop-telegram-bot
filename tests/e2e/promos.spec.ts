import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { components } from "../../packages/api-client/schema";
import { mkdir } from "node:fs/promises";
import { adminStaffFixture, ownerStaffFixture } from "./staff-fixture";

const account = {
  id: "9007199254740993123",
  email: "promo@example.test",
  email_verified: true,
  telegram_id: null,
};
const documents = [
  {
    document_type: "offer",
    version: "test-v1",
    url: "https://example.test/offer",
  },
  {
    document_type: "privacy",
    version: "test-v1",
    url: "https://example.test/privacy",
  },
];
const legal = { configured: true, revision: "test-v1", documents };
const product = {
  id: "tariff-test",
  code: "test",
  name: "Тариф подарка",
  description: "Синтетический тариф",
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
const subscription = (id: string, name: string) => ({
  id,
  name,
  product_id: product.id,
  status: "active",
  expires_at: "2099-12-01T00:00:00Z",
  devices_limit: 4,
  version: 1,
  connection_url: null,
  devices: [],
  segments: [],
  recurring: null,
});
type Pending = components["schemas"]["PendingPromoDto"];
const discount: Pending = {
  code: "SAVED",
  available: true,
  state: "pending",
  reward_type: "percent",
  percent: "15",
  amount_rub: null,
  reward_days: null,
  product_id: null,
};
const gift: Pending = {
  code: "DAYS",
  available: true,
  state: "pending",
  reward_type: "free_days",
  percent: "0",
  amount_rub: null,
  reward_days: 7,
  product_id: product.id,
};
async function setup(
  page: Page,
  options: {
    admin?: boolean;
    pending?: Pending;
    subscriptions?: ReturnType<typeof subscription>[];
    claimFailure?: boolean;
    products?: (typeof product)[];
  } = {},
) {
  let pending: Pending | null = options.pending || null;
  let failClaim = Boolean(options.claimFailure);
  const writes: {
    path: string;
    body: Record<string, unknown>;
    csrf?: string;
  }[] = [];
  if (options.admin)
    await page.route("**/admin", async (route) =>
      route.fulfill({
        response: await route.fetch({
          url: `${test.info().project.use.baseURL}/`,
        }),
      }),
    );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (request.method() !== "GET") {
      const body = request.postDataJSON() as Record<string, unknown>;
      writes.push({ path, body, csrf: request.headers()["x-csrf-token"] });
      if (path === "/promos/activate") {
        pending = { ...discount, code: String(body.code) };
        return route.fulfill({ json: pending });
      }
      if (path === "/promos/claim") {
        if (failClaim) {
          failClaim = false;
          return route.fulfill({
            status: 409,
            json: {
              error: {
                code: "subscription_required",
                message: "Подписки изменились. Выберите подписку снова.",
              },
            },
          });
        }
        pending = null;
        return route.fulfill({
          json: {
            code: "DAYS",
            state: "used",
            subscription_id: body.subscription_id || "new-test",
            reward_days: 7,
            expires_at: "2099-12-08T00:00:00Z",
            provisioning_state: "pending",
          },
        });
      }
      if (path === "/quotes")
        return route.fulfill({
          status: 422,
          json: {
            error: {
              code: "synthetic",
              message: "Синтетический расчёт получен",
            },
          },
        });
      return route.fulfill({ json: {} });
    }
    const json =
      path === "/auth/me"
        ? {
            account,
            csrf_token: "synthetic-csrf",
            is_admin: Boolean(options.admin),
            ...(options.admin
              ? adminStaffFixture
              : { staff_role: null, capabilities: [] }),
            mfa_verified: true,
          }
        : path === "/public-config"
          ? {
              instance: syntheticInstance,
              local_test: true,
              capabilities: { payments: false, recurring: false },
            }
          : path === "/catalog"
            ? {
                products: options.products ?? [product],
                periods: product.periods,
                legal,
                content: {},
                recurring_enrollment_enabled: false,
              }
            : path === "/legal/status"
              ? { ...legal, accepted: true, missing: [], history: [] }
              : path === "/dashboard"
                ? {
                    account,
                    subscriptions: options.subscriptions || [],
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
                  : path === "/admin/promos"
                    ? {
                        items: [
                          {
                            code: "OWNER",
                            percent: "15",
                            enabled: true,
                            audience: "all",
                            per_account_limit: 1,
                            total_limit: null,
                            activated_users: 3,
                            redeemed_count: 2,
                            remaining_uses: null,
                          },
                        ],
                      }
                    : path === "/admin/discounts"
                      ? {
                          items: [
                            {
                              id: "discounts",
                              value: {
                                cap: "40",
                                bonus_cap: "50",
                                referral_percent: "15",
                                promos: [
                                  {
                                    code: "OWNER",
                                    percent: "15",
                                    enabled: true,
                                    audience: "all",
                                    per_account_limit: 1,
                                    total_limit: null,
                                  },
                                ],
                                loyalty_period_days: 30,
                                loyalty_milestones: [],
                              },
                            },
                          ],
                          total: 1,
                        }
                      : {};
    return route.fulfill({ json });
  });
  return writes;
}
async function confirm(page: Page) {
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await expect(dialog.locator('input[type="password"]')).toHaveCount(0);
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
}

test("admin promo counters stay read-only and deletion waits for explicit confirmation", async ({
  page,
}) => {
  const writes = await setup(page, { admin: true });
  await page.goto("/admin#promos");
  const card = page
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: "OWNER", exact: true }) });
  await expect(
    card.getByText("Активировали пользователей", { exact: true }),
  ).toBeVisible();
  await expect(card.locator("dd").nth(0)).toHaveText("3");
  await expect(card.locator("dd").nth(1)).toHaveText("2");
  await expect(card.locator("dd").nth(2)).toHaveText("Без ограничения");
  await card
    .getByRole("button", { name: "Удалить промокод OWNER", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Удалить промокод OWNER?" }),
  ).toBeVisible();
  expect(writes).toHaveLength(0);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  expect(writes).toHaveLength(0);
  await card
    .getByRole("button", { name: "Удалить промокод OWNER", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await dialog.locator('input[type="password"]').fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(writes.map((write) => write.path)).toEqual([
    "/admin/mfa/verify",
    "/admin/promos/OWNER",
  ]);
  expect(writes[1].body).toEqual({
    confirmed: true,
  });
});

test("ordinary public content keeps confirmation and MFA while audit reason is automatic", async ({
  page,
}) => {
  const writes = await setup(page, { admin: true });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        account,
        csrf_token: "synthetic-csrf",
        is_admin: true,
        mfa_verified: true,
        ...ownerStaffFixture,
      },
    }),
  );
  await page.route("**/api/v1/admin/public-content", (route) =>
    route.request().method() === "GET"
      ? route.fulfill({ json: { faq: [], how_to_connect: [] } })
      : route.fallback(),
  );
  await page.route("**/api/v1/admin/content?**", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.goto("/admin#content");
  await page
    .locator("summary", { hasText: "FAQ и инструкции подключения" })
    .click();
  await page
    .getByRole("button", { name: "Сохранить FAQ и инструкции", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await expect(dialog).not.toContainText("Укажите причину");
  await expect(
    dialog.getByRole("button", {
      name: "Подтвердить и сохранить",
      exact: true,
    }),
  ).toBeDisabled();
  expect(writes).toHaveLength(0);
  await dialog.locator('input[type="password"]').fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(writes.map((write) => write.path)).toEqual([
    "/admin/mfa/verify",
    "/admin/public-content",
  ]);
  expect(writes[1].body).not.toHaveProperty("reason");
  expect(writes[1].body.confirmed).toBe(true);
});

test("closing a nested promo deletion confirmation does not submit its settings form", async ({
  page,
}) => {
  const writes = await setup(page, { admin: true });
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill({
      json: {
        account,
        csrf_token: "synthetic-csrf",
        is_admin: true,
        mfa_verified: true,
        ...ownerStaffFixture,
      },
    }),
  );
  await page.goto("/admin#discounts");
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Удалить промокод OWNER", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(2);
  await expect(
    page
      .getByRole("dialog")
      .last()
      .getByRole("button", { name: "Закрыть", exact: true }),
  ).toHaveAttribute("type", "button");
  await page
    .getByRole("dialog")
    .last()
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(
    page.getByRole("dialog").getByRole("combobox", { name: "Тип награды" }),
  ).toBeVisible();
  expect(writes).toHaveLength(0);
});

test("account activation survives reload and checkout leaves automatic promo selection to backend", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/#account");
  await page
    .getByRole("textbox", { name: "Промокод", exact: true })
    .fill("SAVED");
  await page.getByRole("button", { name: "Активировать промокод" }).click();
  await expect(
    page.getByText("Активный промокод", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Промокод", exact: true }),
  ).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("SAVED", { exact: true })).toBeVisible();
  await page.goto("/#home");
  await page
    .getByRole("button", { name: "Добавить подписку", exact: true })
    .click();
  await expect(
    page.getByRole("dialog").getByText("Активный промокод: SAVED"),
  ).toBeVisible();
  await page
    .getByRole("dialog")
    .getByRole("checkbox", { name: /Принимаю оферту/ })
    .check();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /Рассчитать/ })
    .click();
  await expect(page.getByText("Синтетический расчёт получен")).toBeVisible();
  expect(
    writes.find((write) => write.path === "/promos/activate")?.body,
  ).toEqual({ code: "SAVED" });
  const quote = writes.find((write) => write.path === "/quotes");
  expect(quote?.body).not.toHaveProperty("promo_code");
  expect(quote?.csrf).toBe("synthetic-csrf");
});

test("free days require explicit selection among subscriptions and retain the promo on a rejected claim", async ({
  page,
}) => {
  const second = "9007199254740993999";
  const writes = await setup(page, {
    pending: gift,
    subscriptions: [
      subscription("9007199254740993998", "Первая подписка"),
      subscription(second, "Вторая подписка"),
    ],
    claimFailure: true,
  });
  await page.goto("/#account");
  await page.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await expect(
    page.getByRole("button", { name: "Получить подарочные дни" }),
  ).toBeDisabled();
  await page
    .getByRole("combobox", { name: "К какой подписке добавить дни" })
    .selectOption(second);
  await page.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(
    page.getByText("Подписки изменились. Выберите подписку снова."),
  ).toBeVisible();
  await expect(page.getByText("DAYS", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Промокод", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(
    page.getByText("Дни начислены. Подключение обновляется."),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Промокод", exact: true }),
  ).toBeEnabled();
  expect(
    writes
      .filter((write) => write.path === "/promos/claim")
      .map((write) => write.body),
  ).toEqual([
    { code: "DAYS", subscription_id: second },
    { code: "DAYS", subscription_id: second },
  ]);
});

test("free days without active access create the configured tariff without sending an invented subscription", async ({
  page,
}) => {
  const writes = await setup(page, { pending: gift });
  await page.goto("/#account");
  await expect(page.getByText(/Будет создана новая подписка/)).toBeVisible();
  await page.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  await page.getByRole("button", { name: "Получить подарочные дни" }).click();
  await expect(
    page.getByText("Дни начислены. Подключение обновляется."),
  ).toBeVisible();
  expect(writes.find((write) => write.path === "/promos/claim")?.body).toEqual({
    code: "DAYS",
    subscription_id: null,
  });
});

test("ordinary admin edits promo rewards, audience and limit without global discount access", async ({
  page,
}) => {
  const writes = await setup(page, { admin: true });
  await page.goto("/admin#promos");
  await page
    .getByRole("button", { name: "Изменить промокод OWNER", exact: true })
    .click();
  const editor = page.getByRole("dialog");
  await editor.getByRole("button", { name: "Закрыть", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toHaveLength(0);
  await page
    .getByRole("button", { name: "Изменить промокод OWNER", exact: true })
    .click();
  await editor
    .getByRole("combobox", { name: "Тип награды" })
    .selectOption("free_days");
  await editor.getByRole("spinbutton", { name: "Подарочных дней" }).fill("7");
  await editor
    .getByRole("combobox", { name: "Тариф новой подписки" })
    .selectOption(product.id);
  await editor
    .getByRole("combobox", { name: "Кому доступен" })
    .selectOption("first_purchase");
  const unlimited = editor.getByRole("button", {
    name: "Без ограничения общего числа активаций",
  });
  await expect(unlimited).toHaveAttribute("aria-pressed", "true");
  await unlimited.click();
  await editor
    .getByRole("spinbutton", { name: "Общее число активаций" })
    .fill("12");
  await unlimited.click();
  await expect(
    editor.getByRole("textbox", { name: "Общее число активаций" }),
  ).toBeDisabled();
  await mkdir(".artifacts/ui", { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: ".artifacts/ui/promos-admin-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: ".artifacts/ui/promos-admin-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  ).toBe(true);
  const axe = await new AxeBuilder({ page }).analyze();
  expect(
    axe.violations.filter((violation) =>
      ["critical", "serious"].includes(violation.impact || ""),
    ),
  ).toEqual([]);
  await editor.getByRole("button", { name: "Продолжить", exact: true }).click();
  await confirm(page);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const saved = writes.find((write) => write.path === "/admin/promos");
  expect(saved?.body).toMatchObject({
    code: "OWNER",
    enabled: true,
    audience: "first_purchase",
    reward_type: "free_days",
    percent: "0",
    amount_rub: null,
    reward_days: 7,
    product_id: product.id,
    total_limit: null,
    per_account_limit: 1,
  });
  expect(saved?.body.confirmed).toBe(true);
  expect(saved?.csrf).toBe("synthetic-csrf");
  expect(writes.some((write) => write.path === "/admin/mfa/verify")).toBe(
    false,
  );
  expect(writes.some((write) => write.path === "/admin/discounts")).toBe(false);
});

test("admin fixed RUB reward sends a decimal string and finite global limit", async ({
  page,
}) => {
  const writes = await setup(page, { admin: true });
  await page.goto("/admin#promos");
  await page
    .getByRole("button", { name: "Изменить промокод OWNER", exact: true })
    .click();
  const editor = page.getByRole("dialog");
  await editor
    .getByRole("combobox", { name: "Тип награды" })
    .selectOption("fixed_rub");
  await editor.getByRole("textbox", { name: "Сумма скидки, ₽" }).fill("50.25");
  await editor
    .getByRole("button", { name: "Без ограничения общего числа активаций" })
    .click();
  await editor
    .getByRole("spinbutton", { name: "Общее число активаций" })
    .fill("10");
  await editor.getByRole("button", { name: "Продолжить", exact: true }).click();
  await confirm(page);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(
    writes.find((write) => write.path === "/admin/promos")?.body,
  ).toMatchObject({
    code: "OWNER",
    enabled: true,
    audience: "all",
    reward_type: "fixed_rub",
    percent: "0",
    amount_rub: "50.25",
    reward_days: null,
    product_id: null,
    total_limit: 10,
    per_account_limit: 1,
  });
});

test("отсутствующий ранг текущего тарифа запрещает смену вместо выдуманного уровня", async ({
  page,
}) => {
  await setup(page, {
    subscriptions: [
      {
        ...subscription("removed", "Сохраненная подписка"),
        product_id: "removed-product",
      },
    ],
  });
  await page.goto("/#home");
  await expect(
    page.getByRole("button", { name: "Сменить тариф" }),
  ).toBeDisabled();
  await expect(
    page.getByText(
      "Смена тарифа недоступна: текущий тариф отсутствует в каталоге.",
    ),
  ).toBeVisible();
});

test("смена тарифа сравнивает реальные ранги вместо предположения о двух уровнях", async ({
  page,
}) => {
  const current = { ...product, tier_level: 7 };
  const higher = {
    ...product,
    id: "higher-rank",
    name: "Следующий уровень",
    tier_level: 11,
  };
  await setup(page, {
    subscriptions: [subscription("custom", "Сохраненная подписка")],
    products: [current, higher],
  });
  await page.goto("/#home");
  await page.getByRole("button", { name: "Сменить тариф" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("combobox", { name: "Тариф", exact: true }),
  ).toHaveValue(higher.id);
  await expect(
    dialog
      .getByRole("combobox", { name: "Тариф", exact: true })
      .locator("option"),
  ).toHaveCount(1);
});

test("средний ранг показывает обе доступные смены и сохраняет строгие границы", async ({
  page,
}) => {
  const current = { ...product, tier_level: 7 };
  const lower = {
    ...product,
    id: "lower-rank",
    name: "Младший уровень",
    tier_level: 3,
  };
  const higher = {
    ...product,
    id: "higher-rank",
    name: "Старший уровень",
    tier_level: 11,
  };
  await setup(page, {
    subscriptions: [subscription("middle", "Сохраненная подписка")],
    products: [lower, current, higher],
  });
  await page.goto("/#home");
  await page.getByRole("button", { name: "Повысить тариф" }).click();
  let select = page
    .getByRole("dialog")
    .getByRole("combobox", { name: "Тариф", exact: true });
  await expect(select).toHaveValue(higher.id);
  await expect(select.locator("option")).toHaveCount(1);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Закрыть", exact: true })
    .click();
  await page.getByRole("button", { name: "Понизить тариф" }).click();
  select = page
    .getByRole("dialog")
    .getByRole("combobox", { name: "Тариф", exact: true });
  await expect(select).toHaveValue(lower.id);
  await expect(select.locator("option")).toHaveCount(1);
});
