import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import { ownerStaffFixture } from "./staff-fixture";

const account = {
  id: "9007199254740993123",
  email: "synthetic@example.test",
  email_verified: true,
  telegram_id: null,
};
const botUrl = `https://t.me/synthetic_bot?start=ref_${account.id}`;
const websiteUrl = `https://example.test/?ref=ref_${account.id}`;
const referral = {
  referral_code: `ref_${account.id}`,
  referral_url: botUrl,
  website_referral_url: websiteUrl,
  reward_percent: "15",
  invited_count: 0,
  rewarded_amount: "0",
  currency: "RUB",
  enrollment_available: true,
};

async function fixture(
  page: Page,
  options: {
    admin?: boolean;
    ledgerFailure?: boolean;
    fallback?: boolean;
    clipboardFailure?: boolean;
    unsafe?: boolean;
    nodes?: boolean;
    nodeStatuses?: (string | null)[];
    faq?: boolean;
    sessions?: boolean;
    service?: ServiceFixture;
  } = {},
) {
  await page.addInitScript(
    ({ fail }) => {
      const copied: string[] = [];
      (window as unknown as { copiedLinks: string[] }).copiedLinks = copied;
      Object.defineProperty(navigator, "clipboard", {
        value: {
          writeText: async (text: string) => {
            if (fail) throw new Error("Synthetic clipboard denied");
            copied.push(text);
          },
        },
      });
    },
    { fail: Boolean(options.clipboardFailure) },
  );
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
  if (options.admin)
    await page.route("**/admin", async (route) =>
      route.fulfill({
        response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
      }),
    );
  const links = options.unsafe
    ? {
        ...referral,
        referral_url: "javascript:alert(1)",
        website_referral_url: "https://user:password@example.test/",
      }
    : referral;
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: Boolean(options.admin),
        mfa_verified: Boolean(options.admin),
        ...(options.admin
          ? ownerStaffFixture
          : { staff_role: null, capabilities: [] }),
      };
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: true,
      };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: options.faq
          ? {
              help_center: {
                faq: [
                  {
                    id: "default",
                    title: "Стандартный вопрос",
                    body: "Стандартный ответ",
                  },
                ],
                custom_faq: [
                  {
                    id: "published",
                    question: "Опубликованный вопрос",
                    answer: [
                      {
                        type: "paragraph",
                        children: [
                          {
                            type: "link",
                            href: "https://example.test/help",
                            children: [
                              { type: "text", text: "Ссылка владельца" },
                            ],
                          },
                          {
                            type: "emoji",
                            id: "90071992547409999",
                            fallback: "☀️",
                            label: "Солнце",
                          },
                        ],
                      },
                    ],
                  },
                ],
              },
            }
          : {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/legal" || path === "/legal/status")
      json = { accepted: true, configured: false, documents: [], missing: [] };
    if (path === "/promos/active") json = null;
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: options.service ? [options.service.subscription] : [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_reserved: "0",
        credit_available: "0",
        credit_currency: null,
        credit_provenance: "unknown",
        referral: options.fallback ? null : links,
      };
    if (path === "/referral") json = links;
    if (path === "/account/bonuses") {
      if (options.ledgerFailure)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "ledger_unavailable",
              message: "История начислений временно недоступна.",
            },
          },
        });
      json = { referral_url: null, ledger: [] };
    }
    if (path === "/notifications")
      json = {
        items: options.nodes
          ? [
              {
                id: "legacy",
                kind: "device",
                title: "Новое устройство",
                body: "Текстовый ответ",
                severity: "info",
                created_at: "2026-10-09T00:00:00Z",
                read_at: null,
                target: "/?tab=subscriptions",
                nodes: [
                  {
                    type: "paragraph",
                    children: [
                      {
                        type: "link",
                        href: "https://example.test/help",
                        children: [
                          { type: "text", text: "Инструкция из сообщения" },
                        ],
                      },
                      {
                        type: "emoji",
                        id: "90071992547409999",
                        fallback: "☀️",
                        label: "Солнце",
                      },
                    ],
                  },
                ],
              },
              {
                id: "inbox",
                kind: "broadcast",
                title: "Объявление",
                body: "Объявление без разметки",
                severity: "info",
                created_at: "2026-10-09T00:00:00Z",
                read_at: null,
                target: "/#notifications",
                nodes: null,
              },
              {
                id: "invalid",
                kind: "broadcast",
                title: "Некорректная разметка",
                body: "Безопасный запасной текст",
                severity: "info",
                created_at: "2026-10-09T00:00:00Z",
                read_at: null,
                target: "/?token=secret",
                nodes: [null],
              },
            ]
          : [],
        unread_count: options.nodes ? 3 : 0,
      };
    if (path === "/account/sessions")
      json = {
        items: [
          {
            id: "synthetic-session",
            current: true,
            auth_method: "email_otp",
            created_at: "2026-10-09T00:00:00Z",
            last_seen_at: null,
            expires_at: "2099-01-01T00:00:00Z",
            ip_address: null,
            browser_label: null,
          },
        ],
      };
    if (path === "/nodes")
      json = {
        items: [
          {
            id: "node",
            name: "Рабочий сервер",
            status: "CONNECTED",
            observed_at: "2026-10-09T00:00:00Z",
          },
        ],
        source: "panel",
        state: "fresh",
      };
    if (path === "/notifications/preferences")
      json = { browser_enabled: false, push_subscriptions: [] };
    if (path === "/admin/users") json = { items: [], total: 0 };
    if (path === "/nodes" && options.nodeStatuses)
      json = {
        items: options.nodeStatuses.map((status, index) => ({
          id: String(index),
          name: `Сервер ${index}`,
          status,
          observed_at: "2026-10-09T00:00:00Z",
        })),
        source: "panel",
        state: "fresh",
      };
    if (options.service) {
      if (
        path === "/subscriptions/synthetic-sub/inventory" ||
        path === "/subscriptions/synthetic-sub/inventory/refresh"
      )
        json = options.service.inventory;
      if (path === "/subscriptions/synthetic-sub/devices/revoke-bulk")
        json = {
          status: options.service.result,
          device_ids: ["device"],
          job_ids: ["job"],
        };
      if (path === "/subscriptions/synthetic-sub/credentials/rotate") {
        options.service.subscription.credential_rotation_state = "pending";
        options.service.subscription.connection_url = null;
        json = { status: "rotate_pending", job_id: "job" };
      }
    }
    return route.fulfill({ json });
  });
}

test("real referral DTO supplies both canonical invitation links while bonuses endpoint returns only ledger", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/#bonuses");
  await expect(
    page.getByRole("button", {
      name: "Скопировать ссылку на сайт",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: websiteUrl, exact: true }),
  ).toHaveAttribute("href", websiteUrl);
  await expect(
    page.getByRole("link", { name: botUrl, exact: true }),
  ).toHaveAttribute("href", botUrl);
  await page
    .getByRole("button", { name: "Скопировать ссылку на сайт", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Скопировать ссылку в Telegram", exact: true })
    .click();
  expect(
    await page.evaluate(
      () => (window as unknown as { copiedLinks: string[] }).copiedLinks,
    ),
  ).toEqual([websiteUrl, botUrl]);
});

test("canonical referral fallback remains usable when independent bonus ledger fails", async ({
  page,
}) => {
  await fixture(page, { fallback: true, ledgerFailure: true });
  await page.goto("/#bonuses");
  await expect(
    page.getByRole("button", {
      name: "Скопировать ссылку на сайт",
      exact: true,
    }),
  ).toBeEnabled();
  await expect(
    page.getByText("История начислений временно недоступна.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: botUrl, exact: true }),
  ).toHaveAttribute("href", botUrl);
});

test("clipboard denial keeps visible referral links available without claiming copied success", async ({
  page,
}) => {
  await fixture(page, { clipboardFailure: true });
  await page.goto("/#bonuses");
  await page
    .getByRole("button", { name: "Скопировать ссылку на сайт", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: websiteUrl, exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Ссылка на сайт скопирована", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("alert")).toBeVisible();
});

test("unsafe referral URLs never become invitation anchors or copy actions", async ({
  page,
}) => {
  await fixture(page, { unsafe: true });
  await page.goto("/#bonuses");
  await expect(
    page.getByRole("heading", { name: "Приглашайте друзей", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Скопировать ссылку/ }),
  ).toHaveCount(0);
  await expect(
    page.locator('a[href^="javascript:"], a[href*="user:password"]'),
  ).toHaveCount(0);
});

for (const admin of [false, true])
  test(`${admin ? "owner" : "customer"} notification control stays at header right edge with mobile keyboard access`, async ({
    page,
  }) => {
    await fixture(page, { admin });
    await page.goto(admin ? "/admin#users" : "/#bonuses");
    await mkdir(".artifacts/ui/v11-bonus-header", { recursive: true });
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const bell = page.getByRole("button", { name: /^Уведомления,/ });
      await expect(bell).toBeVisible();
      const geometry = await page
        .locator(".workspace-top")
        .evaluate((header) => {
          const button = header.querySelector<HTMLButtonElement>(
            ".bell-toggle__button",
          )!;
          const edge =
            header.getBoundingClientRect().right -
            parseFloat(getComputedStyle(header).paddingRight);
          const rect = button.getBoundingClientRect();
          return {
            gap: edge - rect.right,
            left: rect.left,
            right: rect.right,
            width: innerWidth,
            overflow: document.documentElement.scrollWidth > innerWidth,
          };
        });
      expect(Math.abs(geometry.gap)).toBeLessThanOrEqual(2);
      expect(geometry.left).toBeGreaterThanOrEqual(0);
      expect(geometry.right).toBeLessThanOrEqual(geometry.width);
      expect(geometry.overflow).toBe(false);
      await page.screenshot({
        path: `.artifacts/ui/v11-bonus-header/${admin ? "owner" : "customer"}-${width}.png`,
        fullPage: true,
      });
      if (width === 390) {
        const audit = await new AxeBuilder({ page }).analyze();
        expect(
          audit.violations.filter((item) =>
            ["critical", "serious"].includes(item.impact ?? ""),
          ),
        ).toEqual([]);
      }
    }
    await bellKeyboard(page);
  });

async function bellKeyboard(page: Page) {
  const bell = page.getByRole("button", { name: /^Уведомления,/ });
  await bell.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("dialog", { name: "Уведомления", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(bell).toBeFocused();
  await page.setViewportSize({ width: 320, height: 900 });
  const toggle = page.getByRole("button", {
    name: /Открыть меню|Меню управления/,
    exact: true,
  });
  await toggle.click();
  await expect(page.getByRole("navigation")).toBeVisible();
  await page.getByRole("navigation").getByRole("link").first().focus();
  await page.keyboard.press("Escape");
  await expect(toggle).toBeFocused();
}

test("real notification AST preserves links and emoji, exact legacy device target, and inbox action", async ({
  page,
}) => {
  await fixture(page, { nodes: true });
  await page.goto("/#bonuses");
  await page.getByRole("button", { name: /Уведомления,/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("link", { name: "Инструкция из сообщения" }),
  ).toHaveAttribute("href", "https://example.test/help");
  await expect(dialog.getByText("Безопасный запасной текст")).toBeVisible();
  await expect(
    dialog.getByRole("img", { name: "Солнце", exact: true }),
  ).toBeVisible();
  await expect(
    dialog
      .locator("article")
      .filter({ hasText: "Новое устройство" })
      .getByRole("link", { name: "Открыть" }),
  ).toHaveAttribute("href", "/#devices");
  await dialog
    .locator("article")
    .filter({ hasText: "Объявление без разметки" })
    .getByRole("link", { name: "Открыть" })
    .click();
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/#bonuses$/);
  await expect(
    dialog
      .locator("article")
      .filter({ hasText: "Некорректная разметка" })
      .getByRole("link", { name: "Открыть" }),
  ).toHaveCount(0);
});
test("real email OTP session provenance and raw connected node status are readable", async ({
  page,
}) => {
  await fixture(page, { sessions: true });
  await page.goto("/#account");
  await expect(page.locator("main")).toContainText("Вход по почте");
  await page.goto("/#help");
  await expect(page.locator(".service-nodes")).toContainText("Работает");
});
test("dashboard FAQ includes published owner semantic content while keeping default questions", async ({
  page,
}) => {
  await fixture(page, { faq: true });
  await page.goto("/#help");
  await page.getByText("Опубликованный вопрос", { exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Ссылка владельца" }),
  ).toHaveAttribute("href", "https://example.test/help");
  await expect(
    page.getByText("Стандартный вопрос", { exact: true }),
  ).toBeVisible();
});

type ServiceFixture = ReturnType<typeof serviceFixture>;
function serviceFixture(result = "revoke_pending") {
  return {
    result,
    subscription: {
      id: "synthetic-sub",
      name: "Мой VPN",
      product_id: null,
      status: "active",
      expires_at: "2099-01-01T00:00:00Z",
      devices_limit: 4,
      version: 1,
      connection_url: "https://vpn.example.test/old" as string | null,
      credential_rotation_state: null as string | null,
      devices: [],
      segments: [],
      recurring: null,
    },
    inventory: {
      subscription_id: "synthetic-sub",
      source: "remnawave",
      status: "ACTIVE",
      observed_at: "2026-10-09T00:00:00Z",
      error: null as string | null,
      traffic_bytes: null,
      traffic_limit_bytes: null,
      paid_expires_at: "2099-01-01T00:00:00Z",
      devices: [
        {
          id: "device",
          name: "Телефон",
          platform: "android",
          generation: 1,
          revoked: false,
          observed_at: "2026-10-09T00:00:00Z",
        },
      ],
    },
  };
}
async function revoke(page: Page) {
  await page.getByRole("button", { name: "Отвязать", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить отвязку", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
}
test("queued device action completes only after fresh matching server generation evidence", async ({
  page,
}) => {
  const service = serviceFixture();
  await page.clock.setFixedTime(new Date("2098-01-01T00:00:00Z"));
  await fixture(page, { service });
  await page.goto("/#devices");
  await revoke(page);
  const rotate = page.getByRole("button", {
    name: "Перевыпустить ссылку",
    exact: true,
  });
  await expect(rotate).toBeDisabled();
  service.inventory.devices[0].revoked = true;
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(rotate).toBeDisabled();
  service.inventory.observed_at = "2026-10-09T00:01:00Z";
  service.inventory.source = "local";
  service.inventory.error = "panel_unavailable";
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(rotate).toBeDisabled();
  service.inventory.source = "remnawave";
  service.inventory.error = null;
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(rotate).toBeEnabled();
  await expect(page.locator("main")).toContainText(
    "Отвязка подтверждена панелью",
  );
});
test("changed device generation and unavailable observations never claim old revocation completion", async ({
  page,
}) => {
  const service = serviceFixture();
  await fixture(page, { service });
  await page.goto("/#devices");
  await revoke(page);
  service.inventory.devices[0].revoked = true;
  service.inventory.devices[0].generation = 2;
  service.inventory.observed_at = "2026-10-09T00:01:00Z";
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(
    page.getByRole("button", { name: "Перевыпустить ссылку", exact: true }),
  ).toBeDisabled();
  await expect(page.locator("main")).toContainText(
    "Поколение устройства изменилось",
  );
});
for (const bulk of [false, true]) {
  test(`${bulk ? "bulk" : "individual"} unlink keeps the approved generation when inventory changes before confirmation`, async ({
    page,
  }) => {
    const service = serviceFixture();
    await fixture(page, { service });
    const bodies: Record<string, unknown>[] = [];
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route(
      "**/api/v1/subscriptions/synthetic-sub/inventory/refresh",
      async (route) => {
        await gate;
        await route.fulfill({ json: service.inventory });
      },
    );
    await page.route(
      "**/api/v1/subscriptions/synthetic-sub/devices/revoke-bulk",
      (route) => {
        bodies.push(route.request().postDataJSON());
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: "device_selection_changed",
              message:
                "Список устройств изменился. Обновите его и подтвердите заново.",
            },
          },
        });
      },
    );
    try {
      await page.goto("/#devices");
      await expect(
        page.getByRole("button", { name: "Отвязать", exact: true }),
      ).toBeVisible();
      const refreshing = page.waitForRequest((request) =>
        request.url().endsWith("/inventory/refresh"),
      );
      await page.getByRole("button", { name: "Проверить панель" }).click();
      await refreshing;
      await page
        .getByRole("button", {
          name: bulk ? "Отвязать все устройства" : "Отвязать",
          exact: true,
        })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Подтверждение отвязки",
      });
      await expect(dialog).toBeVisible();
      service.inventory.devices[0].generation = 2;
      const refreshed = page.waitForResponse((response) =>
        response.url().endsWith("/inventory/refresh"),
      );
      release();
      await refreshed;
      for (let attempt = 0; attempt < 2; attempt++) {
        await dialog
          .getByRole("button", { name: "Подтвердить отвязку" })
          .click();
        await expect.poll(() => bodies.length).toBe(attempt + 1);
        await expect(dialog).toContainText("Список устройств изменился");
      }
      expect(bodies[0]).toMatchObject({
        device_ids: ["device"],
        expected_generations: { device: 1 },
        confirmed: true,
      });
      expect(bodies[1]).toEqual(bodies[0]);
      await expect(page.locator("main")).not.toContainText(
        "Отвязка поставлена в очередь",
      );
      await expect(page.locator("main")).not.toContainText(
        "Отвязка подтверждена",
      );
    } finally {
      release();
    }
  });
}

test("review-required bulk response is shown honestly without reporting queued success", async ({
  page,
}) => {
  const service = serviceFixture("review_required");
  await fixture(page, { service });
  await page.goto("/#devices");
  await revoke(page);
  await expect(page.locator("main")).toContainText("Отвязка требует сверки");
  await expect(page.locator("main")).not.toContainText(
    "Отвязка поставлена в очередь",
  );
});
test("accepted rotation finishes only when server clears durable state and supplies a different safe URL", async ({
  page,
}) => {
  const service = serviceFixture();
  await fixture(page, { service });
  await page.goto("/#devices");
  await page
    .getByRole("button", { name: "Перевыпустить ссылку", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить перевыпуск", exact: true })
    .click();
  const rotate = page.getByRole("button", {
    name: "Перевыпустить ссылку",
    exact: true,
  });
  await expect(rotate).toBeDisabled();
  service.subscription.credential_rotation_state = null;
  service.subscription.connection_url = "https://vpn.example.test/old";
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(rotate).toBeDisabled();
  service.subscription.connection_url = "https://vpn.example.test/new";
  await page.getByRole("button", { name: "Проверить панель" }).click();
  await expect(rotate).toBeEnabled();
  await expect(page.locator("main")).toContainText(
    "Новая ссылка подтверждена сервером",
  );
});

test("incoming inbox URL opens the inbox and unknown customer notification targets stay noninteractive", async ({
  page,
}) => {
  await fixture(page, { nodes: true });
  await page.goto("/#notifications");
  await expect(
    page.getByRole("dialog", { name: "Уведомления", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("dialog").getByText("Безопасный запасной текст"),
  ).toBeVisible();
});

test("all real node enum values remain distinct and unknown never looks healthy", async ({
  page,
}) => {
  await fixture(page, {
    nodeStatuses: ["CONNECTED", "DISCONNECTED", "DISABLED", null],
  });
  await page.goto("/#help");
  for (const [index, label] of [
    "Работает",
    "Недоступен",
    "Отключён",
    "Статус уточняется",
  ].entries())
    await expect(
      page
        .locator(".service-nodes article")
        .filter({ hasText: `Сервер ${index}` }),
    ).toContainText(label);
});
test("already revoked response does not leave the operation permanently pending", async ({
  page,
}) => {
  const service = serviceFixture("revoked");
  await fixture(page, { service });
  await page.goto("/#devices");
  await revoke(page);
  await expect(
    page.getByRole("button", { name: "Перевыпустить ссылку", exact: true }),
  ).toBeEnabled();
  await expect(page.locator("main")).toContainText(
    "Отвязка подтверждена сервером",
  );
});

for (const action of ["individual", "bulk", "rotate"]) {
  test(`${action} confirmation clears previous success before a new operation is denied`, async ({
    page,
  }) => {
    const service = serviceFixture();
    await fixture(page, { service });
    await page.goto("/#devices");
    await revoke(page);
    service.inventory.devices[0].revoked = true;
    service.inventory.observed_at = "2026-10-09T00:01:00Z";
    await page.getByRole("button", { name: "Проверить панель" }).click();
    await expect(page.locator("main")).toContainText(
      "Отвязка подтверждена панелью.",
    );
    service.inventory.devices[0].revoked = false;
    service.inventory.devices[0].generation = 2;
    service.inventory.observed_at = "2026-10-09T00:02:00Z";
    await page.getByRole("button", { name: "Проверить панель" }).click();
    await expect(
      page.getByRole("button", { name: "Отвязать", exact: true }),
    ).toBeVisible();
    await page.route(
      `**/api/v1/subscriptions/synthetic-sub/${action === "rotate" ? "credentials/rotate" : "devices/revoke-bulk"}`,
      (route) =>
        route.fulfill({
          status: 409,
          json: {
            error: {
              code: "device_selection_changed",
              message: "Новая операция не выполнена. Обновите сведения.",
            },
          },
        }),
    );
    await page
      .getByRole("button", {
        name:
          action === "individual"
            ? "Отвязать"
            : action === "bulk"
              ? "Отвязать все устройства"
              : "Перевыпустить ссылку",
        exact: true,
      })
      .click();
    await expect(page.locator("main")).not.toContainText(
      "Отвязка подтверждена панелью.",
    );
    await page
      .getByRole("dialog")
      .getByRole("button", {
        name:
          action === "rotate"
            ? "Подтвердить перевыпуск"
            : "Подтвердить отвязку",
      })
      .click();
    await expect(page.getByRole("dialog")).toContainText(
      "Новая операция не выполнена",
    );
    await expect(page.locator("main")).not.toContainText(
      "Отвязка подтверждена панелью.",
    );
  });
}

for (const fails of [false, true])
  test(`account switch never displays previous referral cache while next dashboard is ${fails ? "failed" : "delayed"}`, async ({
    page,
  }) => {
    await fixture(page);
    const nextAccount = {
      ...account,
      id: "9007199254740993999",
      email: "second@example.test",
    };
    let identity: typeof account | null = account;
    let requests = 0;
    let releaseA!: () => void, releaseB!: () => void;
    const lateA = new Promise<void>((resolve) => (releaseA = resolve));
    const delayedB = new Promise<void>((resolve) => (releaseB = resolve));
    const sessionFor = (value: typeof account) => ({
      account: value,
      csrf_token: "synthetic",
      is_admin: false,
      mfa_verified: false,
      staff_role: null,
      capabilities: [],
    });
    const dashboardFor = (value: typeof account) => ({
      account: value,
      subscriptions: [],
      payments: [],
      bonus_balance: "0",
      bonus_reserved: "0",
      credit_balance: "0",
      credit_reserved: "0",
      credit_available: "0",
      referral: {
        ...referral,
        referral_code: `ref_${value.id}`,
        referral_url: `https://t.me/synthetic_bot?start=ref_${value.id}`,
        website_referral_url: `https://example.test/?ref=ref_${value.id}`,
      },
    });
    await page.route("**/api/v1/legal", (route) =>
      route.fulfill({
        json: {
          configured: true,
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
      }),
    );
    await page.route("**/api/v1/auth/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/logout")) {
        identity = null;
        return route.fulfill({ json: { status: "ok" } });
      }
      if (path.endsWith("/email/request"))
        return route.fulfill({
          json: { challenge_id: "synthetic-challenge", resend_after: 120 },
        });
      if (path.endsWith("/email/verify")) {
        identity = nextAccount;
        return route.fulfill({ json: sessionFor(nextAccount) });
      }
      return identity
        ? route.fulfill({ json: sessionFor(identity) })
        : route.fulfill({
            status: 401,
            json: { error: { code: "unauthorized" } },
          });
    });
    await page.route("**/api/v1/dashboard", async (route) => {
      const requested = identity;
      if (requested?.id === account.id) {
        requests++;
        if (requests > 1) await lateA;
      } else await delayedB;
      if (requested?.id === nextAccount.id && fails)
        return route
          .fulfill({
            status: 503,
            json: {
              error: {
                code: "dashboard_unavailable",
                message: "Сведения временно недоступны.",
              },
            },
          })
          .catch(() => {});
      if (requested)
        await route.fulfill({ json: dashboardFor(requested) }).catch(() => {});
    });
    try {
      await page.goto("/#bonuses");
      await expect(
        page.getByRole("link", { name: websiteUrl, exact: true }),
      ).toBeVisible();
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await expect.poll(() => requests).toBeGreaterThan(1);
      await page.getByRole("button", { name: "Выйти", exact: true }).click();
      await page.getByLabel("Email", { exact: true }).fill(nextAccount.email);
      await page.getByRole("checkbox").check();
      await page
        .getByRole("button", { name: "Получить код", exact: true })
        .click();
      await page.getByLabel("Код из письма", { exact: true }).fill("123456");
      await page
        .getByRole("button", { name: "Войти в кабинет", exact: true })
        .click();
      await expect(page.getByRole("complementary")).toContainText(
        nextAccount.email,
      );
      await expect(
        page.getByRole("link", { name: websiteUrl, exact: true }),
      ).toHaveCount(0);
      await expect(page.locator("main")).not.toContainText(account.id);
      releaseB();
      if (fails)
        await expect(page.locator("main")).toContainText(
          "Сведения временно недоступны",
        );
      else
        await expect(
          page.getByRole("link", {
            name: `https://example.test/?ref=ref_${nextAccount.id}`,
            exact: true,
          }),
        ).toBeVisible();
      releaseA();
      await expect(
        page.getByRole("link", { name: websiteUrl, exact: true }),
      ).toHaveCount(0);
    } finally {
      releaseA();
      releaseB();
    }
  });
