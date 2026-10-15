import { syntheticInstance } from "../../../tests/e2e/instance-fixture";
import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const artifacts = fileURLToPath(
  new URL("../../../.artifacts/v6-design/", import.meta.url),
);
const product = (index: number) => ({
  id: `9007199254740999${index}`,
  code: `synthetic-${index}`,
  name: ["Повседневный", "Расширенный", "Для команды"][index - 1],
  description: [
    "Для привычных задач и ваших устройств.",
    "Дополнительные возможности подключения.",
    "Для нескольких устройств в одной подписке.",
  ][index - 1],
  price: ["149.00", "329.00", "549.50"][index - 1],
  currency: "RUB",
  tier_level: index,
  version: 1,
  included_devices: index + 2,
  available: true,
  extra_device_price: null,
  content: {},
  semantic_role: `synthetic-${index}`,
  visible_landing: true,
  visible_bot: true,
  sort_order: index,
  highlighted: index === 1,
  icon: { fallback: ["✦", "◈", "◇"][index - 1], custom_emoji_id: null },
  base_period_days: 28,
  periods: [{ days: 28, months: 1, discount_percent: "0", price: "149.00" }],
  eligibility: { allow_upgrade: true, allow_downgrade: true },
});
const catalog = (count: number) => ({
  products: Array.from({ length: count }, (_, i) => product(i + 1)),
  periods: [{ days: 28, months: 1, discount_percent: "0" }],
  recurring_enrollment_enabled: false,
  capabilities: {
    one_time_payments: false,
    recurring: false,
    dynamic_recurring_amount: false,
    provisioning: "mock",
  },
  content: {
    faq: [
      {
        id: "access",
        question: "Где найти инструкцию подключения?",
        answer: [
          {
            type: "text",
            text: "Инструкция доступна в личном кабинете после подключения подписки.",
          },
        ],
      },
      {
        id: "devices",
        question: "Как узнать количество устройств?",
        answer: [
          {
            type: "text",
            text: "Количество устройств указано в карточке выбранного тарифа.",
          },
        ],
      },
      {
        id: "support",
        question: "Как обратиться за помощью?",
        answer: [
          { type: "text", text: "Откройте ссылку поддержки внизу страницы." },
        ],
      },
    ],
  },
  legal: {
    configured: true,
    revision: "synthetic-v1",
    documents: [
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
    ],
  },
});
async function routes(page: Page, count: number) {
  await page.route("**/api/v1/instance/assets/hero", (route) =>
    route.fulfill({
      contentType: "image/png",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aTioAAAAASUVORK5CYII=",
        "base64",
      ),
    }),
  );
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({ json: catalog(count) }),
  );
  await page.route("**/api/v1/public-config", (route) =>
    route.fulfill({
      json: {
        instance: {
          ...syntheticInstance,
          hero_url: "/api/v1/instance/assets/hero",
        },
        dashboard_url: "http://127.0.0.1:3200",
        bot_url: null,
        support_url: "https://example.test/support",
        turnstile_site_key: null,
        local_test: true,
      },
    }),
  );
}

test("shared catalog introduction preserves owner formatting without raw Telegram markup", async ({
  page,
}) => {
  await routes(page, 2);
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({
      json: {
        ...catalog(2),
        content: {
          ...catalog(2).content,
          catalog_description: "Каталог, отредактированный владельцем",
          catalog_description_content: [
            {
              type: "paragraph",
              children: [
                {
                  type: "emoji",
                  id: "6005924903419646424",
                  fallback: "🌅",
                  label: "Рассвет",
                },
                {
                  type: "bold",
                  children: [
                    {
                      type: "text",
                      text: "Каталог, отредактированный владельцем",
                    },
                  ],
                },
              ],
            },
          ],
        },
      },
    }),
  );
  await page.goto("http://127.0.0.1:3100");
  const section = page.locator("#tariffs");
  await expect(
    section.getByText("Каталог, отредактированный владельцем"),
  ).toBeVisible();
  await expect(
    section.locator(".sunrise-section-heading strong"),
  ).toContainText("Каталог, отредактированный владельцем");
  await expect(section).not.toContainText("<tg-emoji");
  await expect(
    section.locator(".sunrise-section-heading .emoji-asset"),
  ).toHaveCount(0);
  await expect(section.locator(".sunrise-section-heading")).not.toContainText(
    "🌅",
  );
});
async function ready(page: Page) {
  await page.goto("http://127.0.0.1:3100");
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      Array.from(document.images).map((image) =>
        image.decode().catch(() => undefined),
      ),
    );
  });
}

test("country flags render as colored glyphs on Windows Chromium without Telegram emoji assets", async ({
  page,
}) => {
  await routes(page, 1);
  const updated = catalog(1);
  updated.products[0].description = "🇪🇸 Испания · 🇩🇪 Германия · 🇳🇱 Нидерланды";
  updated.products[0].content = { description: [] };
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({ json: updated }),
  );
  await page.setViewportSize({ width: 390, height: 900 });
  await ready(page);
  const description = page.locator(".sunrise-tariff-description");
  await expect(description).toContainText("🇪🇸 Испания");
  const pixels = await description.evaluate(async (element) => {
    const loaded = await document.fonts.load(
      '48px "Twemoji Country Flags"',
      "🇪🇸",
    );
    const canvas = document.createElement("canvas");
    canvas.width = 120;
    canvas.height = 70;
    const context = canvas.getContext("2d")!;
    context.font = '48px "Twemoji Country Flags", sans-serif';
    context.fillStyle = "#000";
    context.fillText("🇪🇸", 10, 54);
    const rgba = context.getImageData(0, 0, 120, 70).data;
    let red = 0,
      yellow = 0;
    for (let index = 0; index < rgba.length; index += 4) {
      if (rgba[index + 3] < 200) continue;
      if (rgba[index] > 150 && rgba[index + 1] < 120 && rgba[index + 2] < 80)
        red++;
      if (rgba[index] > 150 && rgba[index + 1] > 130 && rgba[index + 2] < 80)
        yellow++;
    }
    return {
      loaded: loaded.length,
      red,
      yellow,
      font: getComputedStyle(element).fontFamily,
    };
  });
  expect(pixels.loaded).toBeGreaterThan(0);
  expect(pixels.red).toBeGreaterThan(100);
  expect(pixels.yellow).toBeGreaterThan(100);
  expect(pixels.font).toContain("Twemoji Country Flags");
  await expect(description.locator(".emoji-asset")).toHaveCount(0);
  await page.screenshot({
    path: ".artifacts/w17-country-flags-windows.png",
    fullPage: true,
  });
});

test("catalog descriptions preserve physical newlines instead of wrapping a single collapsed paragraph", async ({
  page,
}) => {
  await routes(page, 1);
  const updated = catalog(1);
  updated.products[0].description = "🌍 Первая\nВторая";
  updated.products[0].content = { description: [] };
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({ json: updated }),
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await ready(page);
  const description = page.locator(".sunrise-tariff-description");
  await expect(description).toContainText("Первая");
  const lines = await description.evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node: Node | null;
    while ((node = walker.nextNode())) {
      const text = node.textContent ?? "";
      const first = text.indexOf("Первая");
      const second = text.indexOf("Вторая");
      if (first < 0 || second < 0) continue;
      const firstRange = document.createRange();
      firstRange.setStart(node, first);
      firstRange.setEnd(node, first + 6);
      const secondRange = document.createRange();
      secondRange.setStart(node, second);
      secondRange.setEnd(node, second + 6);
      const firstRect = firstRange.getBoundingClientRect();
      const secondRect = secondRange.getBoundingClientRect();
      return {
        firstTop: firstRect.top,
        secondTop: secondRect.top,
        combinedWidth: firstRect.width + secondRect.width + 32,
        availableWidth: element.clientWidth,
      };
    }
    return null;
  });
  expect(lines).not.toBeNull();
  expect(lines!.combinedWidth).toBeLessThan(lines!.availableWidth);
  expect(lines!.secondTop).toBeGreaterThan(lines!.firstTop + 5);
});
test.beforeAll(async () => {
  await mkdir(artifacts, { recursive: true });
});
for (const width of [360, 768, 1280]) {
  test(`structured and fallback catalog descriptions remain readable at ${width}px`, async ({
    page,
  }) => {
    await routes(page, 3);
    const updated = catalog(3);
    updated.products[0].description =
      "🌍 Подключение\n1. Первый пункт\n2. Второй пункт\n\n🇩🇪 Локация А\n🇫🇮 Локация Б";
    updated.products[0].content = { description: [] };
    updated.products[1].description = "Запасной текст без дублирования";
    updated.products[1].content = {
      description: [
        { type: "emoji", fallback: "🌐" },
        { type: "text", text: " Описание владельца" },
        { type: "br" },
        {
          type: "list",
          ordered: true,
          items: [
            [{ type: "text", text: "Первое условие" }],
            [{ type: "text", text: "Второе условие" }],
          ],
        },
        {
          type: "list",
          items: [
            [{ type: "text", text: "🇩🇪 Локация А" }],
            [{ type: "text", text: "🇫🇮 Локация Б" }],
          ],
        },
      ],
    };
    updated.products[2].description = "Обычное описание остаётся доступным.";
    updated.products[2].content = {
      description: Array.from({ length: 501 }, () => ({
        type: "text",
        text: "Лишний узел",
      })),
    };
    await page.route("**/api/v1/catalog", (route) =>
      route.fulfill({ json: updated }),
    );
    await page.setViewportSize({ width, height: 1000 });
    await ready(page);
    const descriptions = page.locator(".sunrise-tariff-description");
    await expect(descriptions).toHaveCount(3);
    await expect(descriptions.nth(0)).not.toContainText("🌍");
    await expect(descriptions.nth(0)).toContainText("🇩🇪 Локация А");
    await expect(descriptions.nth(0)).toContainText("🇫🇮 Локация Б");
    await expect(descriptions.nth(1)).not.toContainText("🌐");
    await expect(descriptions.locator(".emoji-asset")).toHaveCount(0);
    await expect(descriptions.nth(1).locator("ol > li")).toHaveCount(2);
    await expect(descriptions.nth(1).locator("ul > li")).toHaveCount(2);
    await expect(descriptions.nth(1).locator("br")).toHaveCount(1);
    await expect(descriptions.nth(1)).not.toContainText("Запасной текст");
    await expect(descriptions.nth(2)).toHaveText(
      "Обычное описание остаётся доступным.",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `${artifacts}/description-after-${width}.png`,
      fullPage: true,
    });
    // Повтор прежнего CSS-поведения на тех же данных для сравнения переносов.
    await descriptions.first().evaluate((element) => {
      (element as HTMLElement).style.whiteSpace = "normal";
    });
    await page.screenshot({
      path: `${artifacts}/description-before-normal-whitespace-${width}.png`,
      fullPage: true,
    });
  });
}
test("catalog empty state preserves a clear navigation path", async ({
  page,
}) => {
  await routes(page, 0);
  await ready(page);
  await expect(page.getByText("Сейчас нет доступных тарифов")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Личный кабинет" }),
  ).toBeVisible();
});
for (const width of [360, 390, 768, 1440]) {
  for (const count of [1, 2, 3]) {
    test(`${count} configured tariffs fit ${width}px`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await routes(page, count);
      await page.setViewportSize({ width, height: 1000 });
      await ready(page);
      await expect(page.locator(".sunrise-tariff")).toHaveCount(count);
      await expect(page.locator(".sunrise-tariff").first()).toContainText(
        "28 дн.",
      );
      await expect(page.locator(".sunrise-header img")).toHaveCount(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(
        await page
          .locator(".sunrise-mascot")
          .evaluate((image: HTMLImageElement) => image.naturalWidth > 0),
      ).toBe(true);
      await expect(
        page.getByRole("link", { name: "Публичная оферта" }),
      ).toHaveAttribute("href", "https://example.test/offer");
      expect(errors).toEqual([]);
      await page.screenshot({
        path: `${artifacts}/${count}-tariffs-${width}.png`,
        fullPage: true,
      });
      if (count === 2) {
        const result = await new AxeBuilder({ page }).analyze();
        await writeFile(
          `${artifacts}/axe-${width}.json`,
          JSON.stringify(result.violations, null, 2),
        );
        expect(
          result.violations.filter((v) =>
            ["serious", "critical"].includes(v.impact || ""),
          ),
        ).toEqual([]);
      }
    });
  }
}
test("admin presentation overrides rank and renamed catalog content without frontend constants", async ({
  page,
}) => {
  await routes(page, 3);
  const updated = catalog(3);
  updated.products[0] = {
    ...updated.products[0],
    name: "Переименован владельцем",
    price: "987.65",
    base_period_days: 42,
    highlighted: false,
    sort_order: 10,
  };
  updated.products[1] = {
    ...updated.products[1],
    highlighted: true,
    sort_order: 0,
  };
  updated.products[2].visible_landing = false;
  updated.content.faq[0].question = "Новый вопрос владельца";
  await page.route("**/api/v1/catalog", (route) =>
    route.fulfill({ json: updated }),
  );
  await ready(page);
  await expect(page.locator(".sunrise-tariff")).toHaveCount(2);
  await expect(page.locator(".sunrise-tariff").first()).toContainText(
    "Расширенный",
  );
  await expect(page.locator(".sunrise-tariff").first()).toHaveClass(
    /is-highlighted/,
  );
  await expect(page.locator(".sunrise-tariff").last()).toContainText(
    "987,65 ₽",
  );
  await expect(page.locator(".sunrise-tariff").last()).toContainText("42 дн.");
  await expect(
    page.getByRole("link", { name: "Выбрать тариф: Переименован владельцем" }),
  ).toHaveAttribute("href", /product=90071992547409991$/);
  await expect(page.getByText("Новый вопрос владельца")).toBeVisible();
});
test("keyboard accordion and reduced motion remain usable", async ({
  page,
}) => {
  await routes(page, 2);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await ready(page);
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Перейти к содержимому" }),
  ).toBeFocused();
  const question = page.locator(".sunrise-faq summary").first();
  await question.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".sunrise-faq details").first()).toHaveAttribute(
    "open",
    "",
  );
  expect(
    await question.evaluate(
      (el) => getComputedStyle(el.querySelector("span")!).transitionDuration,
    ),
  ).toBe("0s");
  await page.keyboard.press("Space");
  await expect(
    page.locator(".sunrise-faq details").first(),
  ).not.toHaveAttribute("open", "");
});
test("catalog failure offers recovery and loaded retry renders records", async ({
  page,
}) => {
  await routes(page, 2);
  let failed = true;
  await page.route("**/api/v1/catalog", (route) =>
    failed
      ? route.fulfill({
          status: 503,
          json: {
            error: {
              message: "Каталог временно недоступен",
              code: "TEMPORARY",
            },
          },
        })
      : route.fulfill({ json: catalog(2) }),
  );
  await ready(page);
  await expect(
    page.getByText("Каталог временно недоступен").first(),
  ).toBeVisible();
  failed = false;
  await page.getByRole("button", { name: "Попробовать снова" }).first().click();
  await expect(page.locator(".sunrise-tariff")).toHaveCount(2);
});
