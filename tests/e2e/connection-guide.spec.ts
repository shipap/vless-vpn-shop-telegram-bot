import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

const account = {
  id: "9007199254740993123",
  email: "guide@example.test",
  email_verified: true,
  telegram_id: null,
};
const link = "https://vpn.example.test/subscription/synthetic-only";
const notice =
  "Можно делиться подпиской с друзьями и семьёй. Не передавайте её людям, в законном использовании которых не уверены. Использование, запрещённое Публичной офертой, может привести к деактивации подписки.";
const fallback =
  "Запасная инструкция: скопируйте ссылку и импортируйте её в приложение. " +
  notice;
const guide = {
  source: "remnawave",
  notice,
  platforms: [
    {
      id: "android",
      name: "Android",
      apps: [
        {
          name: "Happ",
          steps: [
            {
              title: "Установите Happ",
              description: "Скачайте приложение из магазина.",
              links: [
                {
                  text: "Скачать Happ",
                  url: "https://download.example.test/happ",
                },
              ],
            },
            {
              title: "Импортируйте подписку",
              description: "Скопируйте ссылку ниже.\nВставьте её в приложение.",
              links: [],
            },
          ],
        },
        {
          name: "Второе приложение",
          steps: [
            {
              title: "Откройте второе приложение",
              description: "Добавьте подписку вручную.",
              links: [],
            },
          ],
        },
      ],
    },
    {
      id: "windows",
      name: "Windows",
      apps: [
        {
          name: "Клиент Windows",
          steps: [
            {
              title: "Установите клиент Windows",
              description: "Выберите установщик для компьютера.",
              links: [
                {
                  text: "Скачать для Windows",
                  url: "https://download.example.test/windows",
                },
              ],
            },
          ],
        },
      ],
    },
    {
      id: "tv",
      name: "Телевизор",
      apps: [
        {
          name: "Клиент телевизора",
          steps: [
            {
              title: "Подключите телевизор",
              description: "Откройте приложение на телевизоре.",
              links: [
                {
                  text: "Скачать для телевизора",
                  url: "https://download.example.test/tv",
                },
              ],
            },
          ],
        },
      ],
    },
  ],
};
const sub = (id: string) => ({
  id,
  product_id: "synthetic",
  name: `Подписка ${id}`,
  status: "active",
  expires_at: "2099-12-01T00:00:00Z",
  devices_limit: 4,
  version: 1,
  connection_url: link,
  devices: [],
  segments: [],
  recurring: null,
});

async function fixture(page: Page, ids = ["first"]) {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (value: string) => {
          (window as unknown as { copiedLink: string }).copiedLink = value;
        },
      },
    });
  });
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const legal = {
      configured: true,
      revision: "v1",
      documents: [
        {
          document_type: "offer",
          version: "v1",
          url: "https://legal.example.test/offer",
        },
        {
          document_type: "privacy",
          version: "v1",
          url: "https://legal.example.test/privacy",
        },
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
        : path === "/catalog"
          ? {
              products: [],
              periods: [],
              content: { help_center: { connection_instructions: fallback } },
              legal,
              recurring_enrollment_enabled: false,
            }
          : ["/legal", "/legal/status"].includes(path)
            ? { ...legal, accepted: true, missing: [], history: [] }
            : path === "/public-config"
              ? {
                  instance: syntheticInstance,
                  local_test: true,
                  turnstile_test_bypass: true,
                }
              : path === "/dashboard"
                ? {
                    account,
                    subscriptions: ids.map(sub),
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
                  }
                : path.endsWith("/connection-instructions")
                  ? guide
                  : {};
    return route.fulfill({ json });
  });
}

async function open(page: Page) {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Подключить VPN", exact: true })
    .click();
  return page.getByRole("dialog", { name: "Подключить VPN", exact: true });
}

test("shared guide chooses backend platforms and apps while retaining copy, QR and legal notice", async ({
  page,
}) => {
  await mkdir(".artifacts/ui/connection-guide", { recursive: true });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await fixture(page);
  for (const width of [320, 390, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const dialog = await open(page);
    await expect(
      dialog.getByRole("heading", { name: "Установите Happ", exact: true }),
    ).toBeVisible();
    const download = dialog.getByRole("link", {
      name: "Скачать Happ",
      exact: true,
    });
    await expect(download).toHaveAttribute(
      "href",
      "https://download.example.test/happ",
    );
    await expect(download).toHaveAttribute("rel", /noopener/);
    await dialog
      .getByRole("combobox", { name: "Приложение", exact: true })
      .focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(
      dialog.getByRole("heading", { name: "Откройте второе приложение" }),
    ).toBeVisible();
    await dialog
      .getByRole("button", { name: "Телевизор", exact: true })
      .focus();
    await page.keyboard.press("Enter");
    await expect(
      dialog.getByRole("button", { name: "Телевизор", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(
      dialog.getByRole("heading", { name: "Подключите телевизор" }),
    ).toBeVisible();
    await expect(
      dialog.getByRole("combobox", { name: "Приложение", exact: true }),
    ).toHaveCount(0);
    await expect(dialog.getByText(notice, { exact: true })).toBeVisible();
    await expect(
      dialog.getByRole("link", { name: "Перейти по ссылке", exact: true }),
    ).toHaveAttribute("href", link);
    await dialog
      .getByRole("button", { name: "Скопировать ссылку", exact: true })
      .click();
    expect(
      await page.evaluate(
        () => (window as unknown as { copiedLink: string }).copiedLink,
      ),
    ).toBe(link);
    await dialog
      .getByRole("button", { name: "Показать QR-код", exact: true })
      .click();
    await expect(dialog.locator("img.qr-code")).toHaveAttribute(
      "src",
      /^data:image\/png;base64,/,
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
    ).toBe(false);
    const a11y = await new AxeBuilder({ page }).include("dialog").analyze();
    expect(
      a11y.violations.filter((violation) =>
        ["serious", "critical"].includes(violation.impact || ""),
      ),
    ).toEqual([]);
    await dialog
      .getByRole("button", { name: "Телевизор", exact: true })
      .scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `.artifacts/ui/connection-guide/tv-${width}.png`,
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Подключить VPN", exact: true }),
    ).toBeFocused();
  }
  expect(errors).toEqual([]);
});

test("a pending guide request does not block existing connection actions", async ({
  page,
}) => {
  await fixture(page);
  let requested = false;
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(
    "**/subscriptions/first/connection-instructions",
    async (route) => {
      requested = true;
      await blocked;
      await route.fulfill({ json: guide });
    },
  );
  const dialog = await open(page);
  await expect.poll(() => requested).toBe(true);
  await expect(
    dialog.getByText("Загружаем инструкции…", { exact: true }),
  ).toBeVisible();
  await expect(dialog.locator(".connection-guide-notice")).toHaveCount(0);
  await dialog
    .getByRole("button", { name: "Скопировать ссылку", exact: true })
    .click();
  expect(
    await page.evaluate(
      () => (window as unknown as { copiedLink: string }).copiedLink,
    ),
  ).toBe(link);
  await dialog
    .getByRole("button", { name: "Показать QR-код", exact: true })
    .click();
  await expect(dialog.locator("img.qr-code")).toBeVisible();
  release();
  await expect(
    dialog.getByRole("heading", { name: "Установите Happ", exact: true }),
  ).toBeVisible();
});

test("fallback preserves configured instructions and failed loading offers a nonblocking retry", async ({
  page,
}) => {
  await fixture(page);
  let tries = 0;
  await page.route(
    "**/subscriptions/first/connection-instructions",
    (route) => {
      tries++;
      return tries === 1
        ? route.fulfill({
            status: 503,
            json: {
              error: {
                code: "guide_unavailable",
                message: "Синтетический отказ",
              },
            },
          })
        : route.fulfill({
            json: { source: "fallback", platforms: [], notice },
          });
    },
  );
  const dialog = await open(page);
  await expect(dialog.getByText(fallback, { exact: true })).toBeVisible();
  await expect(dialog.locator(".connection-guide-notice")).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Обновить инструкцию", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Скопировать ссылку", exact: true }),
  ).toBeEnabled();
  await dialog
    .getByRole("button", { name: "Обновить инструкцию", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "Обновить инструкцию", exact: true }),
  ).toHaveCount(0);
  await expect(dialog.getByText(fallback, { exact: true })).toBeVisible();
  expect(
    await dialog.evaluate(
      (element, wording) => element.textContent!.split(wording).length - 1,
      notice,
    ),
  ).toBe(1);
  await expect(
    dialog.getByRole("group", { name: "Устройство", exact: true }),
  ).toHaveCount(0);
});

test("guide descriptions are plain text and only credential-free HTTPS download links become interactive", async ({
  page,
}) => {
  await fixture(page);
  const title = "Установка <img src=x onerror=alert(1)>";
  await page.route("**/subscriptions/first/connection-instructions", (route) =>
    route.fulfill({
      json: {
        source: "remnawave",
        notice,
        platforms: [
          {
            id: "tv",
            name: "Телевизор",
            apps: [
              {
                name: "Клиент",
                steps: [
                  {
                    title,
                    description: "<script>window.unsafe=true</script>",
                    links: [
                      {
                        text: "Безопасная ссылка",
                        url: "https://download.example.test/app",
                      },
                      { text: "Опасный протокол", url: "javascript:alert(1)" },
                      {
                        text: "Незащищённый адрес",
                        url: "http://127.0.0.1/download",
                      },
                      {
                        text: "Данные в адресе",
                        url: "https://user:pass@download.example.test/app",
                      },
                      { text: "Относительный адрес", url: "/download" },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
    }),
  );
  const dialog = await open(page);
  await expect(
    dialog.getByRole("heading", { name: title, exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("link", { name: "Безопасная ссылка", exact: true }),
  ).toHaveAttribute("href", "https://download.example.test/app");
  for (const name of [
    "Опасный протокол",
    "Незащищённый адрес",
    "Данные в адресе",
    "Относительный адрес",
  ])
    await expect(dialog.getByRole("link", { name, exact: true })).toHaveCount(
      0,
    );
  await expect(
    dialog.locator(".connection-guide img,.connection-guide script"),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => (window as unknown as { unsafe?: boolean }).unsafe,
    ),
  ).toBeUndefined();
});

test("closing a delayed guide cannot replace instructions for a subsequently selected subscription", async ({
  page,
}) => {
  await fixture(page, ["first", "second"]);
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => {
    release = resolve;
  });
  let firstRequested = false;
  await page.route(
    "**/subscriptions/first/connection-instructions",
    async (route) => {
      firstRequested = true;
      await blocked;
      await route.fulfill({ json: guide }).catch(() => {});
    },
  );
  await page.route("**/subscriptions/second/connection-instructions", (route) =>
    route.fulfill({
      json: {
        ...guide,
        platforms: [
          {
            id: "new",
            name: "Новое устройство",
            apps: [
              {
                name: "Новый клиент",
                steps: [
                  {
                    title: "Инструкция второй подписки",
                    description: "Только для второй подписки.",
                    links: [],
                  },
                ],
              },
            ],
          },
        ],
      },
    }),
  );
  let dialog = await open(page);
  await expect.poll(() => firstRequested).toBe(true);
  await page.keyboard.press("Escape");
  await page.locator(".subscription-select select").selectOption("second");
  await page
    .getByRole("button", { name: "Подключить VPN", exact: true })
    .click();
  dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", {
      name: "Инструкция второй подписки",
      exact: true,
    }),
  ).toBeVisible();
  release();
  await expect(
    dialog.getByRole("heading", { name: "Установите Happ", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Новое устройство", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("bounded long guide titles and download labels wrap inside the narrow dialog", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await fixture(page);
  await page.route("**/subscriptions/first/connection-instructions", (route) =>
    route.fulfill({
      json: {
        source: "remnawave",
        notice,
        platforms: [
          {
            id: "tv",
            name: "Телевизор",
            apps: [
              {
                name: "Приложение".repeat(9),
                steps: [
                  {
                    title: "Инструкция".repeat(9),
                    description: "Откройте приложение.",
                    links: [
                      {
                        text: "Скачать".repeat(12),
                        url: "https://download.example.test/app",
                      },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      },
    }),
  );
  const dialog = await open(page);
  await expect(
    dialog.getByRole("heading", { name: "Инструкция".repeat(9), exact: true }),
  ).toBeVisible();
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
});
