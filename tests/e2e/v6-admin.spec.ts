import { expect, test, type Page } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";
import { ownerStaffFixture } from "./staff-fixture";

const documents = [
  { document_type: "offer", version: "v1", url: "https://example.test/offer" },
  {
    document_type: "privacy",
    version: "v1",
    url: "https://example.test/privacy",
  },
];
const product = {
  id: "test-tariff",
  code: "test",
  name: "Тестовый тариф",
  description: "Описание",
  price: "199.00",
  currency: "RUB",
  tier_level: 1,
  available: true,
  included_devices: 3,
  version: 7,
  semantic_role: "standard",
  visible_landing: true,
  visible_bot: true,
  highlighted: false,
  sort_order: 2,
  base_period_days: 30,
  periods: [{ days: 30, months: 1, discount_percent: "0", price: "199.00" }],
  eligibility: { allow_upgrade: true, allow_downgrade: true },
  icon: { fallback: "☀", custom_emoji_id: "9007199254740993123" },
  content: {},
  internal_squad_uuid: "11111111-1111-4111-8111-111111111111",
};
type TestProduct = Omit<typeof product, "base_period_days" | "periods"> & {
  base_period_days: number | null;
  periods: {
    days: number;
    months: number | string;
    discount_percent: string;
    price?: string;
  }[];
  extra_device_price?: string | null;
  presentation_overrides?: Record<string, unknown>;
};
async function setup(
  page: Page,
  options: {
    policy?: Record<string, unknown>;
    products?: TestProduct[];
    periods?: { days: number; months: number; discount_percent: string }[];
    publicContent?: Record<string, unknown>;
    legalDocuments?: typeof documents;
  } = {},
) {
  const writes: {
    path: string;
    body: Record<string, unknown>;
    csrf: string | undefined;
  }[] = [];
  // Только тестовый HTML: production guard остаётся без изменений, API полностью перехвачен.
  await page.route("**/admin", async (route) =>
    route.fulfill({
      response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
    }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (request.method() !== "GET") {
      writes.push({
        path,
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
      });
      return route.fulfill({ json: {} });
    }
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: {
          id: "9007199254740993123",
          email: "owner@example.test",
          email_verified: true,
        },
        is_admin: true,
        ...ownerStaffFixture,
        mfa_verified: true,
        csrf_token: "synthetic-csrf",
      };
    if (path === "/catalog")
      json = {
        products: options.products ?? [product],
        periods: options.periods ?? [],
        content: {},
        recurring_enrollment_enabled: false,
      };
    if (path === "/admin/catalog")
      json = {
        items: options.products ?? [product],
        total: options.products?.length ?? 1,
      };
    if (path === "/admin/discounts")
      json = {
        items:
          options.policy === undefined
            ? []
            : [{ id: "discounts", value: options.policy }],
        total: options.policy === undefined ? 0 : 1,
      };
    if (path === "/admin/legal")
      json = {
        configured: true,
        documents: options.legalDocuments ?? documents,
        revision: "v1",
      };
    if (path === "/admin/legal/missing")
      json = {
        items: [
          {
            id: "9007199254740993123",
            email: "test@example.test",
            missing: ["offer", "privacy"],
          },
        ],
        total: 1,
        page_size: 25,
      };
    if (path === "/admin/refund-policy")
      json = { max_age_hours: 24, max_traffic_bytes: 100000000 };
    if (path === "/admin/refund-reviews")
      json = {
        items: [
          {
            id: "review-test",
            account_id: "9007199254740993123",
            amount: "199.00",
            currency: "RUB",
            state: "pending",
            subscription_version: 7,
            evidence: { usage_bytes: null },
          },
        ],
        total: 1,
      };
    if (path === "/admin/telegram-theme")
      json = {
        icons: {
          connect: { fallback: "☀", custom_emoji_id: "9007199254740993123" },
        },
        styles: {
          primary: "primary",
          success: "success",
          danger: "danger",
          neutral: "neutral",
          admin: "primary",
        },
      };
    if (path === "/admin/public-content")
      json = options.publicContent ?? {
        faq: [
          {
            id: "faq1",
            question: "Как подключиться?",
            answer: [
              {
                type: "paragraph",
                children: [{ type: "text", text: "Откройте кабинет" }],
              },
            ],
          },
        ],
        how_to_connect: [],
      };
    if (path === "/admin/panel-mapping")
      json = { external_squad_uuid: null, project_prefix: "sunrise_test_" };
    await route.fulfill({ json });
  });
  return writes;
}

test("отмена сохранения документов оставляет введённую версию без записи", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/admin#legal");
  await page.getByLabel("Версия оферты").fill("draft-v2");
  await page.getByRole("button", { name: "Сохранить документы" }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", { name: "После изменения" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Отмена", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByLabel("Версия оферты")).toHaveValue("draft-v2");
  expect(writes).toHaveLength(0);
});

test("ошибка MFA не сохраняет документы", async ({ page }) => {
  const writes = await setup(page);
  await page.route("**/api/v1/admin/mfa/verify", (route) =>
    route.fulfill({
      status: 403,
      json: {
        error: { code: "mfa_invalid", message: "Неверный код подтверждения." },
      },
    }),
  );
  await page.goto("/admin#legal");
  await page.getByLabel("Версия оферты").fill("v2");
  await page.getByRole("button", { name: "Сохранить документы" }).click();
  await confirm(page);
  await expect(page.getByRole("dialog")).toContainText(
    "Неверный код подтверждения.",
  );
  expect(writes).toHaveLength(0);
});

test("отклонённое влияние тарифа сохраняет форму и не открывает подтверждение", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.route("**/api/v1/admin/catalog/test-tariff/impact", (route) =>
    route.fulfill({
      status: 422,
      json: {
        error: {
          code: "invalid_mapping",
          message: "Сквад недоступен для тарифа.",
        },
      },
    }),
  );
  await page.goto("/admin#catalog");
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  await page.getByLabel("Базовая цена").fill("299");
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  const editor = page.getByRole("dialog", {
    name: "Изменить тариф",
    exact: true,
  });
  await expect(editor).toContainText("Сквад недоступен для тарифа.");
  await expect(editor.getByLabel("Базовая цена")).toHaveValue("299");
  await expect(
    page.getByRole("button", { name: "Подтвердить и сохранить", exact: true }),
  ).toHaveCount(0);
  expect(writes).toHaveLength(0);
});

async function confirm(page: Page) {
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await dialog.locator('input[type="password"]').fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: /Подтвердить/ }).click();
}
test("документы сохраняют версии с MFA, автоматическим аудитом и CSRF; список сохраняет BIGINT", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/admin#legal");
  await expect(page.getByText("9007199254740993123")).toBeVisible();
  await page.getByLabel("Версия оферты").fill("v2");
  await page.getByRole("button", { name: "Сохранить документы" }).click();
  await expect(
    page.getByRole("button", { name: "Подтвердить и сохранить", exact: true }),
  ).toBeDisabled();
  expect(writes).toHaveLength(0);
  await confirm(page);
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({
    path: "/admin/legal",
    csrf: "synthetic-csrf",
    body: {
      confirmed: true,
      documents: [{ ...documents[0], version: "v2" }, documents[1]],
    },
  });
});
test("возврат ограничивает пороги и отправляет версию подписки при сокращении", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/admin#refunds");
  await page.getByLabel("Максимальный возраст, часы").fill("25");
  await page.getByRole("button", { name: "Сохранить пороги" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(writes).toHaveLength(0);
  await page.getByLabel("Решение по возврату").selectOption("shorten");
  await page.getByLabel("Новая дата окончания (МСК)").fill("2026-10-20T12:00");
  await page.getByRole("button", { name: "Сохранить решение" }).click();
  await confirm(page);
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].body).toMatchObject({
    action: "shorten",
    ends_at: "2026-10-20T09:00:00.000Z",
    expected_subscription_version: 7,
  });
});
test("каталог меняет представление и сохраняет строковый ID значка", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/admin#catalog");
  await page.getByRole("button", { name: "Изменить" }).click();
  await expect(page.getByLabel("ID эмодзи тарифа")).toHaveValue(
    "9007199254740993123",
  );
  await page.getByLabel("Название тарифа").fill("Переименованный тариф");
  await page.getByLabel("Показывать на сайте").uncheck();
  await expect(page.getByText("Скрыт на сайте")).toBeVisible();
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  await confirm(page);
  await expect
    .poll(() => writes.some((w) => w.path === "/admin/catalog/test-tariff"))
    .toBe(true);
  expect(
    writes.find((w) => w.path === "/admin/catalog/test-tariff")?.body,
  ).toMatchObject({
    name: "Переименованный тариф",
    visible_landing: false,
  });
  const saved = writes.find(
    (w) => w.path === "/admin/catalog/test-tariff",
  )?.body;
  expect(saved).not.toHaveProperty("icon");
  expect(saved).not.toHaveProperty("periods");
});

for (const basePeriod of [null, 31]) {
  test(`переименование сохраняет ${basePeriod === null ? "неизвестный" : "наследуемый"} базовый период без нового override`, async ({
    page,
  }) => {
    const writes = await setup(page, {
      products: [
        {
          ...product,
          base_period_days: basePeriod,
          periods:
            basePeriod === null
              ? []
              : [
                  {
                    days: 31,
                    months: 1,
                    discount_percent: "0",
                    price: "199.00",
                  },
                ],
          presentation_overrides: {},
        },
      ],
    });
    await page.goto("/admin#catalog");
    if (basePeriod === null)
      await expect(page.locator(".v6-catalog-grid")).toContainText(
        "Базовый период не задан",
      );
    await page.getByRole("button", { name: "Изменить", exact: true }).click();
    if (basePeriod === null) {
      for (const preview of await page
        .locator(".content-previews > section > .content-preview")
        .all()) {
        await expect(preview).toContainText("Базовый период не задан");
        await expect(preview).not.toContainText(/\/\s+дней/);
      }
    }
    await page.getByLabel("Название тарифа").fill("Переименованный тариф");
    const valid = await page
      .getByLabel("Базовый период, дни")
      .evaluate((input) => (input as HTMLInputElement).form!.checkValidity());
    expect(valid).toBe(true);
    await page.getByRole("button", { name: "Сохранить тариф" }).click();
    await confirm(page);
    await expect
      .poll(
        () => writes.find((w) => w.path === "/admin/catalog/test-tariff")?.body,
      )
      .toBeTruthy();
    const saved = writes.find(
      (w) => w.path === "/admin/catalog/test-tariff",
    )!.body;
    expect(saved).toMatchObject({
      name: "Переименованный тариф",
      confirmed: true,
    });
    expect(saved).not.toHaveProperty("base_period_days");
    expect(saved).not.toHaveProperty("periods");
    expect(saved).not.toHaveProperty("presentation_overrides");
    const impact = writes.find((w) => w.path.endsWith("/impact"))!.body;
    expect(impact).toEqual({ name: "Переименованный тариф" });
  });
}

for (const clearFee of [false, true]) {
  test(`редактирование тарифа ${clearFee ? "явно очищает" : "сохраняет"} цену дополнительного места`, async ({
    page,
  }) => {
    const writes = await setup(page, {
      products: [{ ...product, extra_device_price: "17.00" }],
    });
    await page.goto("/admin#catalog");
    await page.getByRole("button", { name: "Изменить", exact: true }).click();
    if (clearFee)
      await page.getByLabel("Линейная цена места (если нет ступеней)").fill("");
    else await page.getByLabel("Название тарифа").fill("Переименованный тариф");
    await page.getByRole("button", { name: "Сохранить тариф" }).click();
    expect(writes.some((w) => w.path === "/admin/catalog/test-tariff")).toBe(
      false,
    );
    await confirm(page);
    await expect
      .poll(
        () => writes.find((w) => w.path === "/admin/catalog/test-tariff")?.body,
      )
      .toBeTruthy();
    const saved = writes.find(
      (w) => w.path === "/admin/catalog/test-tariff",
    )!.body;
    expect(saved).toMatchObject({
      confirmed: true,
    });
    expect(writes.some((w) => w.path === "/admin/mfa/verify")).toBe(true);
    expect(
      writes.find((w) => w.path === "/admin/catalog/test-tariff")!.csrf,
    ).toBe("synthetic-csrf");
    if (clearFee) {
      expect(saved.extra_device_price).toBeNull();
      expect(writes.find((w) => w.path.endsWith("/impact"))!.body).toEqual({
        extra_device_price: null,
      });
    } else expect(saved).not.toHaveProperty("extra_device_price");
  });
}
test("наследуемые дробные месяцы сохраняются при переименовании и требуют целого значения при изменении периодов", async ({
  page,
}) => {
  const writes = await setup(page, {
    products: [
      {
        ...product,
        base_period_days: null,
        periods: [
          { days: 31, months: "1.1", discount_percent: "0", price: "218.90" },
        ],
        presentation_overrides: {},
      },
    ],
  });
  await page.goto("/admin#catalog");
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  const months = page.getByLabel("Расчётные месяцы", { exact: true });
  await expect(months).toHaveValue("1.1");
  await page.getByLabel("Название тарифа").fill("Переименованный тариф");
  expect(
    await months.evaluate((input) =>
      (input as HTMLInputElement).form!.checkValidity(),
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  await confirm(page);
  await expect
    .poll(
      () => writes.find((w) => w.path === "/admin/catalog/test-tariff")?.body,
    )
    .toBeTruthy();
  expect(
    writes.find((w) => w.path === "/admin/catalog/test-tariff")!.body,
  ).not.toHaveProperty("periods");
  const previousWrites = writes.length;
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  await page.getByLabel("Дни доступа", { exact: true }).fill("32");
  expect(
    await months.evaluate((input) =>
      (input as HTMLInputElement).form!.checkValidity(),
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  expect(writes).toHaveLength(previousWrites);
  await months.fill("2");
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  await confirm(page);
  await expect
    .poll(
      () =>
        writes.filter((w) => w.path === "/admin/catalog/test-tariff").length,
    )
    .toBe(2);
  expect(
    writes.filter((w) => w.path === "/admin/catalog/test-tariff").at(-1)!.body,
  ).toMatchObject({
    periods: [{ days: 32, months: 2, discount_percent: "0" }],
  });
});
test("тема и FAQ редактируются через поля без JSON", async ({ page }) => {
  const writes = await setup(page);
  await page.goto("/admin#telegram");
  await page.locator("summary", { hasText: "Значки меню" }).click();
  await page.getByLabel("ID эмодзи: connect").fill("9007199254740993999");
  await page.getByRole("button", { name: "Сохранить тему" }).click();
  await confirm(page);
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].body).toMatchObject({
    theme: { icons: { connect: { custom_emoji_id: "9007199254740993999" } } },
  });
  await page.goto("/admin#content");
  await page
    .locator("summary", { hasText: "FAQ и инструкции подключения" })
    .click();
  await page.getByLabel("Вопрос 1").fill("Новый вопрос");
  await page
    .getByRole("button", { name: "Сохранить FAQ и инструкции" })
    .click();
  await confirm(page);
  await expect.poll(() => writes.length).toBe(4);
  expect(writes[3].body).toMatchObject({
    faq: [{ id: "faq1", question: "Новый вопрос" }],
  });
});

for (const promoChannels of [
  undefined,
  [{ id: "fixture-promo", label: "Канал" }],
]) {
  test(`FAQ отправляет только редактируемые поля при GET с вступлением${promoChannels ? " и каналами" : ""}`, async ({
    page,
  }) => {
    const intro = [
      {
        type: "paragraph",
        children: [{ type: "text", text: "Существующее вступление каталога" }],
      },
    ];
    const writes = await setup(page, {
      publicContent: {
        faq: [],
        how_to_connect: [],
        catalog_description_content: intro,
        ...(promoChannels ? { promo_channels: promoChannels } : {}),
      },
    });
    await page.goto("/admin#content");
    await page
      .locator("summary", { hasText: "FAQ и инструкции подключения" })
      .click();
    await page
      .getByRole("button", { name: "Добавить практические ответы" })
      .click();
    await page
      .getByRole("button", { name: "Сохранить FAQ и инструкции" })
      .click();
    await confirm(page);
    await expect
      .poll(
        () => writes.filter((w) => w.path === "/admin/public-content").length,
      )
      .toBe(1);
    const saved = writes.find((w) => w.path === "/admin/public-content")!;
    expect(Object.keys(saved.body).sort()).toEqual([
      "confirmed",
      "faq",
      "how_to_connect",
    ]);
    expect(saved.body).toMatchObject({
      faq: expect.arrayContaining([
        expect.objectContaining({ id: "help_payment_access" }),
        expect.objectContaining({ id: "help_vpn_connection" }),
      ]),
      how_to_connect: [],
      confirmed: true,
    });
    expect(saved.body.faq).toHaveLength(6);
    expect(saved.csrf).toBe("synthetic-csrf");
  });
}
test("админ V6 помещается на мобильном экране", async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/admin#refunds");
  await expect(page.getByLabel("Максимальный возраст, часы")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await mkdir(".artifacts/ui/v6-admin", { recursive: true });
  await page.screenshot({
    path: ".artifacts/ui/v6-admin/refunds-mobile.png",
    fullPage: true,
  });
});

for (const width of [320, 360, 390, 768, 1024]) {
  test(`юридическая форма с длинными URL и версиями помещается при ширине ${width}`, async ({
    page,
  }, testInfo) => {
    const longUrl =
      "https://example.test/legal/" + "public-document-".repeat(12);
    const longVersion = "fixture-" + "v".repeat(71);
    await setup(page, {
      legalDocuments: documents.map((d) => ({
        ...d,
        url: longUrl,
        version: longVersion,
      })),
    });
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/admin#legal");
    const save = page.getByRole("button", {
      name: "Сохранить документы",
      exact: true,
    });
    await expect(save).toBeVisible();
    await expect(page.getByLabel("URL оферты")).toHaveValue(longUrl);
    const measurements = await page.evaluate(() => ({
      viewport: innerWidth,
      document: document.documentElement.scrollWidth,
      controls: Array.from(
        document.querySelectorAll<HTMLElement>(
          ".v6-form, .v6-form fieldset, .v6-form input, .v6-form button",
        ),
      ).map((element) => ({
        tag: element.tagName,
        left: element.getBoundingClientRect().left,
        right: element.getBoundingClientRect().right,
        width: element.getBoundingClientRect().width,
      })),
    }));
    await testInfo.attach("legal-layout", {
      body: JSON.stringify(measurements),
      contentType: "application/json",
    });
    expect(measurements.document).toBeLessThanOrEqual(width);
    expect(measurements.controls.length).toBeGreaterThanOrEqual(8);
    for (const control of measurements.controls) {
      expect(control.left).toBeGreaterThanOrEqual(0);
      expect(control.right).toBeLessThanOrEqual(width);
      expect(control.width).toBeGreaterThan(0);
    }
    await save.scrollIntoViewIfNeeded();
    await expect(save).toBeInViewport();
  });
}
test("создание тарифа сохраняет ступени устройств и общие настройки панели отдельно", async ({
  page,
}) => {
  const writes = await setup(page);
  await page.goto("/admin#catalog");
  await page
    .getByRole("button", { name: "Добавить тариф", exact: true })
    .click();
  await page.getByLabel("Постоянный ключ тарифа").fill("new_test");
  await page.getByLabel("Название тарифа").fill("Новый тариф");
  await page.getByLabel("Семантический ключ тарифа").fill("standard");
  await page.getByLabel("Базовая цена").fill("250");
  await page.getByLabel("Валюта", { exact: true }).fill("RUB");
  await page.getByLabel("Включённые устройства").fill("9");
  await page.getByLabel("Базовый период, дни").fill("42");
  const rank = page.getByLabel("Ранг тарифа", { exact: true });
  await expect(rank).toHaveValue("");
  await rank.fill("0");
  expect(
    await rank.evaluate((input) =>
      (input as HTMLInputElement).form!.checkValidity(),
    ),
  ).toBe(false);
  await rank.fill("7");
  for (const [label, value] of [
    ["Постоянный ключ тарифа", "new-test"],
    ["Семантический ключ тарифа", "invalid-role"],
    ["Ранг тарифа", "1001"],
    ["Базовая цена", "123456789.00"],
    ["Базовая цена", "0.00"],
    ["Включённые устройства", "101"],
    ["Базовый период, дни", "731"],
    ["Порядок отображения", "10001"],
    ["Линейная цена места (если нет ступеней)", "0.00"],
  ]) {
    const accepted = await page
      .getByLabel(label, { exact: true })
      .evaluate((input, value) => {
        const field = input as HTMLInputElement;
        const original = field.value;
        field.value = value;
        const accepted = field.form!.checkValidity();
        field.value = original;
        return accepted;
      }, value);
    expect(accepted, label + ": " + value).toBe(false);
  }
  await expect(
    page.getByLabel("Название тарифа", { exact: true }),
  ).toHaveAttribute("maxlength", "200");
  await expect(
    page.getByLabel("Краткое описание", { exact: true }),
  ).toHaveAttribute("maxlength", "4000");
  const currency = page.getByLabel("Валюта", { exact: true });
  const invalidCurrencyAccepted = await currency.evaluate((input) => {
    const field = input as HTMLInputElement;
    field.value = "RUBX";
    return field.form!.checkValidity();
  });
  expect(invalidCurrencyAccepted).toBe(false);
  await expect(currency).toHaveAttribute("maxlength", "3");
  await currency.fill("RUB");
  await page.getByRole("button", { name: "Добавить ступень" }).click();
  await page.getByLabel("До дополнительного места включительно").fill("5");
  await page.getByLabel("Цена места на ступени").fill("50");
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  await expect(
    page.getByText(/последняя ступень — без верхней границы/),
  ).toBeVisible();
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "Добавить ступень" }).click();
  await page.getByLabel("Цена места на ступени").nth(1).fill("100");
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  await confirm(page);
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toMatchObject({
    path: "/admin/catalog",
    body: {
      code: "new_test",
      price: "250.00",
      included_devices: 9,
      base_period_days: 42,
      currency: "RUB",
      tier_level: 7,
      extra_device_price: null,
      extra_device_rules: [
        { through_extra: 5, price: "50.00" },
        { through_extra: null, price: "100.00" },
      ],
    },
  });
  await page.getByLabel("Префикс проекта").fill("isolated_shop_");
  await page
    .getByRole("button", { name: "Сохранить настройки панели" })
    .click();
  await confirm(page);
  await expect.poll(() => writes.length).toBe(4);
  expect(writes[3]).toMatchObject({
    path: "/admin/panel-mapping",
    body: { external_squad_uuid: null, project_prefix: "isolated_shop_" },
  });
});
test("устаревшее решение возврата показывает конфликт без повторной мутации", async ({
  page,
}) => {
  const writes = await setup(page);
  let attempts = 0;
  await page.route(
    "**/api/v1/admin/refund-reviews/review-test/resolve",
    async (route) => {
      attempts++;
      await route.fulfill({
        status: 409,
        json: {
          error: {
            code: "refund_version_conflict",
            message: "Подписка изменилась. Обновите очередь.",
          },
        },
      });
    },
  );
  await page.goto("/admin#refunds");
  await page.getByLabel("Решение по возврату").selectOption("revoke");
  await page.getByRole("button", { name: "Сохранить решение" }).click();
  await confirm(page);
  await expect(
    page.getByText("Подписка изменилась. Обновите очередь."),
  ).toBeVisible();
  expect(attempts).toBe(1);
  expect(writes).toHaveLength(1);
});
test("новые формы доступны с клавиатуры и не имеют серьёзных ошибок axe", async ({
  page,
}) => {
  await setup(page);
  await mkdir(".artifacts/ui/v6-admin", { recursive: true });
  for (const section of [
    "legal",
    "refunds",
    "telegram",
    "catalog",
    "content",
  ]) {
    await page.goto(`/admin#${section}`);
    if (section === "content")
      await page
        .locator("summary", { hasText: "FAQ и инструкции подключения" })
        .click();
    await expect(page.locator(".v6-form").first()).toBeVisible();
    const result = await new AxeBuilder({ page }).analyze();
    expect(
      result.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact || ""),
      ),
    ).toEqual([]);
    await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
    await page.screenshot({
      path: `.artifacts/ui/v6-admin/${section}-desktop.png`,
      fullPage: true,
    });
  }
  await page.goto("/admin#catalog");
  await page.getByRole("button", { name: "Изменить", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.screenshot({
    path: ".artifacts/ui/v6-admin/catalog-editor-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: ".artifacts/ui/v6-admin/catalog-editor-mobile.png",
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("калькулятор отправляет актуальные настройки и нестандартный период из БД", async ({
  page,
}) => {
  const writes = await setup(page, {
    policy: { cap: "27", bonus_cap: "19", referral_percent: "8" },
    periods: [{ days: 42, months: 2, discount_percent: "3" }],
  });
  await page.goto("/admin#discounts");
  await expect(page.getByLabel("Лимит скидки, %", { exact: true })).toHaveValue(
    "27",
  );
  await expect(page.getByLabel("Лимит бонусной оплаты, %")).toHaveValue("19");
  await expect(page.getByLabel("Период для расчёта")).toHaveValue("42");
  await page.getByRole("button", { name: "Рассчитать без сохранения" }).click();
  await expect
    .poll(
      () =>
        writes.find((item) => item.path === "/admin/discounts/preview")?.body,
    )
    .toMatchObject({
      product_id: "test-tariff",
      period_days: 42,
      cap: "27",
      bonus_cap: "19",
    });
});

test("ненастроенная политика оставляет обязательные поля пустыми без выдуманных ступеней", async ({
  page,
}) => {
  const writes = await setup(page, { policy: {} });
  await page.goto("/admin#discounts");
  await expect(page.getByLabel("Лимит скидки, %", { exact: true })).toHaveValue(
    "",
  );
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  const editor = page.getByRole("dialog", { name: "Правила скидок" });
  await expect(
    editor.getByLabel("Длительность периода стажа, дней"),
  ).toHaveValue("");
  await expect(editor.getByLabel("Скидка за стаж, %")).toHaveCount(0);
  await editor
    .getByRole("button", { name: "Сохранить изменения", exact: true })
    .click();
  expect(writes).toHaveLength(0);
});

test("сохранение настроенных скидок не добавляет неизвестную кампанию", async ({
  page,
}) => {
  const writes = await setup(page, {
    policy: {
      cap: "27",
      bonus_cap: "19",
      referral_percent: "8",
      promos: [],
      loyalty_period_days: 42,
      loyalty_milestones: [{ elapsed_periods: 3, percent: "4" }],
    },
  });
  await page.goto("/admin#discounts");
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Сохранить изменения", exact: true })
    .click();
  await confirm(page);
  await expect
    .poll(() => writes.find((item) => item.path === "/admin/discounts")?.body)
    .toBeTruthy();
  const saved = writes.find((item) => item.path === "/admin/discounts")?.body;
  expect(saved).toMatchObject({
    cap: "27",
    bonus_cap: "19",
    referral_percent: "8",
    loyalty_period_days: 42,
    loyalty_milestones: [{ elapsed_periods: 3, percent: "4" }],
  });
  expect(saved).not.toHaveProperty("renewal_campaign");
  expect(saved).not.toHaveProperty("referral_welcome");
});

test("пустой каталог требует явных устройств периода и валюты нового тарифа", async ({
  page,
}) => {
  const writes = await setup(page, { products: [] });
  await page.goto("/admin#catalog");
  await page
    .getByRole("button", { name: "Добавить тариф", exact: true })
    .click();
  await expect(page.getByLabel("Включённые устройства")).toHaveValue("");
  await expect(page.getByLabel("Базовый период, дни")).toHaveValue("");
  await expect(page.getByLabel("Валюта", { exact: true })).toHaveValue("");
  await page
    .getByRole("button", { name: "Добавить период", exact: true })
    .click();
  await expect(page.getByLabel("Дни доступа", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "Сохранить тариф" }).click();
  expect(writes).toHaveLength(0);
});
