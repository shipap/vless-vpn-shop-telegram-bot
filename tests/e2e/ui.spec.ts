import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFile, mkdir, access, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createHmac } from "node:crypto";
const root = fileURLToPath(new URL("../../", import.meta.url));
const artifacts = `${root}.artifacts/ui`;
const backend = "http://127.0.0.1:8150";
const runId = process.env.SUNRISE_E2E_AUTH_RUN_ID;
test.use({ baseURL: "http://127.0.0.1:3210" });
const widths = [360, 390, 768, 1440];
async function shot(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${artifacts}/${name}.png`, fullPage: true });
}
async function noOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
}
async function a11y(page: Page, name: string) {
  const result = await new AxeBuilder({ page }).analyze();
  await writeFile(
    `${artifacts}/${name}-axe.json`,
    JSON.stringify(result.violations, null, 2),
  );
  expect(
    result.violations.filter((v) =>
      ["critical", "serious"].includes(v.impact || ""),
    ),
  ).toEqual([]);
}
function totp(secret: string) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const c of secret.toUpperCase().replace(/=+$/, ""))
    bits += alphabet.indexOf(c).toString(2).padStart(5, "0");
  const bytes = Buffer.from(bits.match(/.{8}/g)!.map((x) => parseInt(x, 2)));
  const time = Buffer.alloc(8);
  time.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000)));
  const h = createHmac("sha1", bytes).update(time).digest();
  const off = h[h.length - 1] & 15;
  return ((h.readUInt32BE(off) & 0x7fffffff) % 1000000)
    .toString()
    .padStart(6, "0");
}
async function freshTotp(secret: string) {
  const prior = totp(secret);
  await expect
    .poll(() => totp(secret), { timeout: 35000, intervals: [1000] })
    .not.toBe(prior);
  return totp(secret);
}
async function acceptCurrentConditions(page: Page) {
  const status = await page.request.get("/api/v1/legal/status");
  expect(status.status()).toBe(200);
  if (!(await status.json()).accepted) {
    const notice = page.getByRole("region", { name: "Условия сервиса" });
    await notice.getByRole("checkbox").check();
    await notice.getByRole("button", { name: "Принять условия" }).click();
    await expect(notice).toHaveCount(0);
  }
}
async function login(page: Page, email: string, fresh = false) {
  void fresh;
  await page.goto("/");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  const pending = page.waitForResponse(
    (r) =>
      r.url().endsWith("/auth/email/request") &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Получить код" }).click();
  const response = await pending;
  expect(response.status()).toBe(200);
  const { challenge_id } = await response.json();
  const mail = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/mail/${challenge_id}.json`,
      "utf8",
    ),
  );
  expect(mail.to).toBe(email);
  await page.getByLabel("Код из письма").fill(mail.code);
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  await expect(
    page.getByRole("heading", { name: "Мой VPN", exact: true }),
  ).toBeVisible();
  await acceptCurrentConditions(page);
}
test.beforeAll(async () => {
  await mkdir(artifacts, { recursive: true });
});
test.beforeEach(async ({ request }) => {
  expect(runId).toMatch(/^[a-f0-9-]{36}$/);
  const fixture = await request.post(`${backend}/__e2e__/ui/setup?mode=ui`, {
    headers: { "X-E2E-Run-ID": runId! },
  });
  expect(fixture.status()).toBe(200);
  expect(await fixture.json()).toMatchObject({
    run_id: runId,
    mode: "ui",
    accounts: 3,
  });
});

test("local emoji assets keep exact IDs and safe fallback without delaying checkout", async ({
  page,
}) => {
  await login(page, "customer@sunrise.test");
  const landing = await page.context().newPage();
  await landing.setViewportSize({ width: 390, height: 900 });
  const hugeId = "90071992547409999";
  const requests: string[] = [];
  let releaseSlow!: () => void;
  const slow = new Promise<void>((resolve) => {
    releaseSlow = resolve;
  });
  await landing.route("**/api/v1/catalog", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    const standard = data.products.find(
      (p: { code: string }) => p.code === "standard",
    );
    expect(data.products).toHaveLength(2);
    standard.content = {
      ...standard.content,
      name: [{ type: "text", text: standard.name }],
      description: [
        { type: "text", text: standard.description },
        {
          type: "emoji",
          id: hugeId,
          fallback: "☀️",
          label: "Загруженное солнце",
        },
        {
          type: "emoji",
          id: "90071992547409998",
          fallback: "🌙",
          label: "Недоступная луна",
        },
        {
          type: "emoji",
          id: "90071992547409997",
          fallback: "⭐",
          label: "Ожидающая звезда",
        },
      ],
    };
    await route.fulfill({ response, json: data });
  });
  await landing.route("**/api/v1/content/emoji/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path.endsWith(hugeId))
      await route.fulfill({
        contentType: "image/png",
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jf9sAAAAASUVORK5CYII=",
          "base64",
        ),
      });
    else if (path.endsWith("997")) {
      await slow;
      await route.fulfill({ status: 404 });
    } else await route.fulfill({ status: 404 });
  });
  try {
    await landing.goto("http://127.0.0.1:3110");
    const loaded = landing.getByRole("img", { name: "Загруженное солнце" });
    await loaded.scrollIntoViewIfNeeded();
    await expect(loaded.locator("img")).toHaveClass(/loaded/);
    expect(
      await loaded
        .locator("img")
        .evaluate((element: HTMLImageElement) => element.naturalWidth),
    ).toBe(1);
    expect(requests).toContain(`/api/v1/content/emoji/${hugeId}`);
    const failed = landing.getByRole("img", { name: "Недоступная луна" });
    await expect(failed.locator("img")).toHaveCount(0);
    await expect(failed).toHaveText("🌙");
    await expect(
      landing.getByRole("img", { name: "Ожидающая звезда" }),
    ).toHaveText("⭐");
    await noOverflow(landing);
    await shot(landing, "emoji-fallback-390");
    await page.getByRole("button", { name: "Добавить подписку" }).click();
    await page
      .getByRole("combobox", { name: "Тариф", exact: true })
      .selectOption("standard");
    await page
      .getByRole("dialog")
      .getByRole("radio", { name: "СБП", exact: true })
      .check();
    await page
      .getByRole("dialog")
      .getByRole("checkbox", { name: /Принимаю оферту/ })
      .check();
    await page.getByRole("button", { name: "Рассчитать стоимость" }).click();
    await expect(page.getByText("Итого к оплате")).toBeVisible();
    await page.getByRole("button", { name: "Подтвердить заказ" }).click();
    await page.getByLabel("Подтверждаю тестовую оплату").check();
    await page
      .getByRole("button", { name: "Выполнить тестовую оплату" })
      .click();
    await expect(page.getByRole("dialog")).toContainText(
      /Оплата подтверждена|подключаем|Доступ выдан/i,
    );
    releaseSlow();
    await expect(
      landing.getByRole("img", { name: "Ожидающая звезда" }).locator("img"),
    ).toHaveCount(0);
    const beforeReduced = requests.length;
    await landing.emulateMedia({ reducedMotion: "reduce" });
    await landing.reload();
    await expect(
      landing.getByRole("img", { name: "Загруженное солнце" }),
    ).toHaveText("☀️");
    await expect(landing.locator(".emoji-asset")).toHaveCount(0);
    expect(requests.length).toBe(beforeReduced);
    await shot(landing, "emoji-reduced-motion-390");
  } finally {
    releaseSlow();
    await landing.close();
  }
});
test("keyboard skip, navigation and modal dismissal preserve route and focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await login(page, "customer@sunrise.test");
  await page.goto("/#payments");
  await expect(
    page.getByRole("heading", { name: "Платежи", exact: true }),
  ).toBeVisible();
  await expect(page.locator("#main-content")).toBeFocused();
  await page.getByRole("link", { name: "К содержимому" }).focus();
  await expect(page.getByRole("link", { name: "К содержимому" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  expect(new URL(page.url()).hash).toBe("#payments");
  const menu = page.getByRole("button", { name: "Открыть меню" });
  await menu.focus();
  await page.keyboard.press("Enter");
  await page.getByRole("link", { name: "Устройства", exact: true }).focus();
  await page.keyboard.press("Escape");
  await expect(menu).toBeFocused();
  await expect(menu).toHaveAttribute("aria-expanded", "false");
  await menu.click();
  await page.getByRole("link", { name: "Мой VPN", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "Мой VPN", exact: true }),
  ).toBeVisible();
  const connect = page.getByRole("button", {
    name: /Подключить VPN|Статус подключения/,
  });
  await connect.click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(connect).toBeFocused();
});
test("built previews preserve genuine admin404 and private-file isolation", async ({
  request,
}) => {
  for (const port of [3111, 3211]) {
    expect((await request.get(`http://127.0.0.1:${port}/`)).status()).toBe(200);
    for (const path of [
      "/.private/owner-access.json",
      "/@fs/C:/Users/example/workspace/vpn-shop/.private/local-resources.json",
      "/.env",
      "/private-audit/DB-FINDINGS.md",
    ])
      expect([403, 404]).toContain(
        (await request.get(`http://127.0.0.1:${port}${path}`)).status(),
      );
  }
  expect((await request.get("http://127.0.0.1:3211/admin")).status()).toBe(404);
  expect((await request.get("http://127.0.0.1:3211/%61dmin")).status()).toBe(
    404,
  );
  expect(
    (await request.get("http://127.0.0.1:3111/api/v1/auth/me")).status(),
  ).toBe(404);
});
test("public landing: live catalog, responsive layout and accessibility", async ({
  page,
}) => {
  const catalog = await (await page.request.get("/api/v1/catalog")).json();
  expect(catalog.products.map((p: { code: string }) => p.code).sort()).toEqual([
    "standard",
    "universal",
  ]);
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("http://127.0.0.1:3110");
    await expect(
      page.getByRole("heading", { name: "Ваш VPN. Для ваших устройств." }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Стандартный", exact: true }),
    ).toBeVisible();
    await noOverflow(page);
    await shot(page, `landing-${width}`);
    if (width === 390) await a11y(page, "landing-390");
  }
  expect(errors).toEqual([]);
});
test("private files, encoded traversal, anonymous admin and landing API denied", async ({
  request,
}) => {
  for (const path of [
    "/admin",
    "/admin/users",
    "/%61dmin",
    "/@fs/C:/Users/example/workspace/vpn-shop/.private/owner-access.json",
    "/@fs/C:/Users/example/workspace/vpn-shop/%2eprivate/local-resources.json",
    "/@fs/C:/Users/example/workspace/vpn-shop/private-audit/DB-FINDINGS.md",
    "/.env",
  ]) {
    const r = await request.get(path);
    expect([403, 404]).toContain(r.status());
  }
  expect(
    (await request.get("http://127.0.0.1:3110/api/v1/auth/me")).status(),
  ).toBe(404);
});
test("email login, real server quote and synthetic checkout, customer responsive pages", async ({
  page,
}) => {
  const errors: string[] = [];
  const failures: { path: string; status: number }[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (r) => {
    if (r.status() >= 500)
      failures.push({ path: new URL(r.url()).pathname, status: r.status() });
  });
  await page.setViewportSize({ width: 1440, height: 960 });
  await login(page, "customer@sunrise.test", true);
  await page.getByRole("button", { name: "Добавить подписку" }).click();
  await page
    .getByRole("combobox", { name: "Тариф", exact: true })
    .selectOption("standard");
  await page
    .getByRole("dialog")
    .getByRole("radio", { name: "СБП", exact: true })
    .check();
  await page
    .getByRole("dialog")
    .getByRole("checkbox", { name: /Принимаю оферту/ })
    .check();
  await page.getByRole("button", { name: "Рассчитать стоимость" }).click();
  await expect(page.getByText("Итого к оплате")).toBeVisible();
  await page.getByRole("button", { name: "Подтвердить заказ" }).click();
  await page.getByLabel("Подтверждаю тестовую оплату").check();
  await page.getByRole("button", { name: "Выполнить тестовую оплату" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    /Оплата подтверждена|подключаем|Доступ выдан/i,
  );
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  const dashboard = await (await page.request.get("/api/v1/dashboard")).json();
  const standard = dashboard.subscriptions.find(
    (s: { product_id: string }) => s.product_id === "standard",
  );
  expect(standard).toBeTruthy();
  for (const width of widths) {
    await page.setViewportSize({ width, height: 960 });
    await page.goto("/#home");
    if (dashboard.subscriptions.length > 1)
      await page
        .getByRole("combobox", { name: "Подписка", exact: true })
        .selectOption(standard.id);
    await expect(
      page.getByRole("button", { name: /Подключить VPN|Статус подключения/ }),
    ).toBeVisible();
    await noOverflow(page);
    await shot(page, `dashboard-${width}`);
    if (width === 390) await a11y(page, "dashboard-390");
  }
  await page.setViewportSize({ width: 390, height: 900 });
  await page
    .getByRole("button", { name: /Подключить VPN|Статус подключения/ })
    .click();
  await expect(
    page.getByRole("dialog", { name: /Подключить VPN|Статус подключения/ }),
  ).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText(
    "Ссылка подключения пока недоступна",
  );
  await a11y(page, "connection");
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  for (const section of ["devices", "payments", "bonuses", "account", "help"]) {
    await page.goto(`/#${section}`);
    await expect(page.getByRole("main")).toBeVisible();
    if (section === "bonuses") {
      const referralResponse = await page.request.get("/api/v1/referral");
      expect(referralResponse.status()).toBe(200);
      const referral = await referralResponse.json();
      expect(referral.referral_code).toBe(`ref_${dashboard.account.id}`);
      expect(new URL(referral.referral_url).searchParams.get("start")).toBe(
        referral.referral_code,
      );
      expect(new URL(referral.referral_url).pathname).toBe(
        "/synthetic_fixture_bot",
      );
      expect(new URL(referral.website_referral_url).origin).toBe(
        "http://127.0.0.1:3110",
      );
      expect(
        new URL(referral.website_referral_url).searchParams.get("ref"),
      ).toBe(referral.referral_code);
      for (const [label, url] of [
        ["Скопировать ссылку в Telegram", referral.referral_url],
        ["Скопировать ссылку на сайт", referral.website_referral_url],
      ]) {
        await expect(
          page.getByRole("button", { name: label, exact: true }),
        ).toBeVisible();
        await expect(
          page.getByRole("link", { name: url, exact: true }),
        ).toHaveAttribute("href", url);
      }
    }
    await noOverflow(page);
    await shot(page, `customer-${section}-390`);
  }
  expect(errors).toEqual([]);
  expect(failures).toEqual([]);
  await writeFile(
    `${artifacts}/browser-evidence.json`,
    JSON.stringify({ errors, serverErrors: failures }, null, 2),
  );
});
test("admin verified email and real MFA, all operational sections, responsive and axe", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await login(page, "owner@sunrise.test");
  await page.goto("/admin");
  const current = await (await page.request.get("/api/v1/auth/me")).json();
  expect(current.staff_role).toBe("owner");
  expect(current.capabilities).toEqual(
    expect.arrayContaining([
      "subscriptions_manage",
      "settings_write",
      "security_manage",
    ]),
  );
  if (!current.mfa_verified) {
    const owner = JSON.parse(
      await readFile(
        `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
        "utf8",
      ),
    );
    await page
      .getByLabel("Код аутентификатора", { exact: true })
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByRole("button", { name: "Подтвердить вход", exact: true })
      .click();
  }
  await expect(
    page.getByRole("heading", { name: "Обзор", exact: true }),
  ).toBeVisible();
  for (const width of widths) {
    await page.setViewportSize({ width, height: 960 });
    await page.goto("/admin#overview");
    await expect(
      page.getByRole("heading", { name: "Оплаты и возвраты" }),
    ).toBeVisible();
    await noOverflow(page);
    await shot(page, `admin-${width}`);
    if (width === 390) await a11y(page, "admin-390");
  }
  for (const section of [
    "users",
    "subscriptions",
    "orders",
    "recurring",
    "catalog",
    "discounts",
    "content",
    "broadcasts",
    "security",
    "operations",
  ]) {
    await page.setViewportSize({ width: 390, height: 960 });
    await page.goto(`/admin#${section}`);
    await expect(
      page.getByRole("button", {
        name: section === "catalog" ? "Добавить тариф" : "Найти",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await noOverflow(page);
    if (section === "operations") {
      const queueTable = page.locator("table").filter({
        has: page.getByRole("button", {
          name: "Повторить задачу",
          exact: true,
        }),
      });
      await expect(queueTable).toBeVisible();
      const heights = await queueTable
        .locator("tbody tr")
        .evaluateAll((rows) =>
          rows.map((row) => row.getBoundingClientRect().height),
        );
      expect(Math.max(...heights)).toBeLessThan(200);
    }
    await shot(page, `admin-${section}-390`);
    if (["content", "security", "operations"].includes(section))
      await a11y(page, `admin-${section}-390`);
    if (section === "users") {
      await page
        .getByRole("textbox", { name: "Поиск записей" })
        .fill("customer@sunrise.test");
      await page.getByRole("button", { name: "Найти", exact: true }).click();
      await expect(page.getByRole("table")).toContainText(
        "customer@sunrise.test",
      );
      await page
        .getByRole("row")
        .filter({
          has: page.getByRole("cell", {
            name: "customer@sunrise.test",
            exact: true,
          }),
        })
        .getByRole("button", { name: "Открыть", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Карточка пользователя" }),
      ).toBeVisible();
      for (const title of [
        "Способы входа",
        "Бонусные операции",
        "Сеансы входа",
        "История действий",
      ])
        await expect(
          page
            .getByRole("dialog")
            .getByRole("heading", { name: title, exact: true }),
        ).toBeVisible();
      await page.getByRole("button", { name: "Закрыть", exact: true }).click();
    }
    if (section === "orders") {
      const payments = await (
        await page.request.get("/api/v1/admin/payments?page_size=25")
      ).json();
      expect(payments.items.length).toBeGreaterThan(0);
      const id = payments.items[0].id;
      await page
        .getByLabel("ID платежа или номер у платёжного сервиса")
        .fill(id);
      await page
        .getByRole("button", { name: "Открыть платёж", exact: true })
        .click();
      await expect(page.getByRole("dialog")).toContainText(id);
      await expect(page.getByRole("dialog")).toContainText("Расчёт заказа");
      await page.getByRole("button", { name: "Закрыть", exact: true }).click();
    }
    if (section === "security") {
      await page
        .getByLabel("Поиск в истории входов", { exact: true })
        .fill("customer@sunrise.test");
      const findLogins = page.getByRole("button", {
        name: "Найти входы",
        exact: true,
      });
      const filteredResponse = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return (
          response.request().method() === "GET" &&
          url.pathname === "/api/v1/admin/security/logins" &&
          url.searchParams.get("search") === "customer@sunrise.test" &&
          url.searchParams.get("page") === "1" &&
          url.searchParams.get("page_size") === "10"
        );
      });
      await findLogins.click();
      const response = await filteredResponse;
      expect(response.status()).toBe(200);
      await response.finished();
      const history = page.locator(".login-history");
      await expect(history.getByRole("status")).toHaveCount(0);
      await expect(history.getByRole("table")).toBeVisible();
      await expect(history.getByRole("table")).toContainText(
        "customer@sunrise.test",
      );
      await expect(history.getByRole("table")).toContainText("Успешный вход");
      await expect(history.getByRole("table")).toContainText(
        "Прямое соединение",
      );
      await findLogins.focus();
      await expect(findLogins).toBeFocused();
      await page.keyboard.press("Tab");
      const scrollRegion = history.getByRole("region", {
        name: "Таблица истории входов",
      });
      await expect(scrollRegion).toBeFocused();
      await page.keyboard.press("ArrowRight");
      await expect
        .poll(() => scrollRegion.evaluate((element) => element.scrollLeft))
        .toBeGreaterThan(0);
      await noOverflow(page);
      await shot(page, "admin-login-history-390");
    }
    if (section === "content") {
      const row = page.getByRole("row").filter({
        has: page.getByRole("cell", {
          name: "content:ui_quality_check",
          exact: true,
        }),
      });
      await row.getByRole("button", { name: "Открыть", exact: true }).click();
      await expect(
        page.getByRole("dialog", { name: "История материала" }),
      ).toContainText("Версия");
      await page.getByRole("button", { name: "Закрыть", exact: true }).click();
    }
    if (section === "broadcasts") {
      await expect(page.getByRole("main")).toContainText(
        "Все пользователи с действующим согласием",
      );
      await page
        .getByLabel("Текст сообщения для предпросмотра")
        .fill("Локальная проверка списка получателей");
      await page
        .getByRole("button", { name: "Рассчитать получателей", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Предпросмотр получателей" }),
      ).toBeVisible();
      await expect(page.getByRole("main")).toContainText(
        "Разрешено на локальном тестовом стенде",
      );
    }
    if (section === "operations") {
      await page.locator("summary", { hasText: "Состояние операций" }).click();
      await expect(
        page.getByRole("heading", { name: "Состояние сервисов" }),
      ).toBeVisible();
      await expect(page.getByRole("main")).toContainText("Резервная копия");
    }
    if (section === "discounts") {
      await page
        .getByRole("button", { name: "Рассчитать без сохранения", exact: true })
        .click();
      await expect(page.getByRole("main")).toContainText(
        "Стоимость после скидок",
      );
      await expect(page.getByRole("alert")).toHaveCount(0);
      const row = page.getByRole("row").filter({
        has: page.getByRole("cell", { name: "discounts", exact: true }),
      });
      await row.getByRole("button", { name: "Изменить", exact: true }).click();
      await expect(
        page.getByLabel("Длительность периода стажа, дней"),
      ).toHaveValue("30");
      await expect(page.getByRole("dialog")).toContainText(
        "Кампания продления",
      );
      await page.getByRole("button", { name: "Закрыть", exact: true }).click();
    }
  }
});

test("subscription restriction and safe synchronization preserve paid expiry through audited MFA", async ({
  page,
}) => {
  // Fresh MFA codes require separate time windows; allow bounded recovery after interruption.
  test.setTimeout(240_000);
  await login(page, "owner@sunrise.test");
  const owner = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
      "utf8",
    ),
  );
  const session = await (await page.request.get("/api/v1/auth/me")).json();
  const headers = { "X-CSRF-Token": session.csrf_token };
  expect(
    (
      await page.request.post("/api/v1/admin/mfa/verify", {
        headers,
        data: { code: await freshTotp(owner.totp_secret) },
      })
    ).status(),
  ).toBe(200);
  const users = await (
    await page.request.get("/api/v1/admin/users?search=customer%40sunrise.test")
  ).json();
  const detail = await (
    await page.request.get(`/api/v1/admin/users/${users.items[0].id}`)
  ).json();
  const chosen = detail.subscriptions.find(
    (sub: { product_id: string }) => sub.product_id === "standard",
  );
  expect(typeof chosen.id).toBe("string");
  const listPath = `/api/v1/admin/subscriptions?search=${encodeURIComponent(chosen.id)}`;
  const initial = (await (await page.request.get(listPath)).json()).items.find(
    (sub: { id: string }) => sub.id === chosen.id,
  );
  if (initial.restriction.suspended) {
    expect(
      (
        await page.request.post(
          `/api/v1/admin/subscriptions/${chosen.id}/resume`,
          {
            headers,
            data: {
              reason:
                "Восстановление тестовой подписки после прерванного браузерного сценария",
              confirmed: true,
            },
          },
        )
      ).status(),
    ).toBe(200);
  }
  const before = (await (await page.request.get(listPath)).json()).items.find(
    (sub: { id: string }) => sub.id === chosen.id,
  );
  expect(before.expires_at).toBe(initial.expires_at);
  expect(before.restriction.suspended).toBe(false);
  async function action(button: string, operation: string) {
    await expect(
      page.getByRole("textbox", { name: "Поиск записей" }),
    ).toHaveValue(chosen.id);
    const row = page.getByRole("row").filter({
      has: page.getByRole("cell", { name: chosen.id, exact: true }),
    });
    await expect(row).toHaveCount(1);
    await expect(page.locator("main .table-scroll tbody > tr")).toHaveCount(1);
    await row.getByRole("button", { name: button, exact: true }).click();
    await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
    await page
      .getByLabel("Свежий код аутентификатора")
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByLabel("Я проверил изменения и подтверждаю действие.")
      .check();
    const response = page.waitForResponse(
      (r) =>
        r.url().endsWith(`/subscriptions/${chosen.id}/${operation}`) &&
        r.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Подтвердить изменение", exact: true })
      .click();
    expect((await response).status()).toBe(200);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(
      page.getByRole("textbox", { name: "Поиск записей" }),
    ).toHaveValue(chosen.id);
    await expect(row).toHaveCount(1);
    await expect(page.locator("main .table-scroll tbody > tr")).toHaveCount(1);
  }
  try {
    await page.goto("/admin#subscriptions");
    await page.getByRole("textbox", { name: "Поиск записей" }).fill(chosen.id);
    await page.getByRole("button", { name: "Найти", exact: true }).click();
    await action("Приостановить", "suspend");
    await expect(
      page.getByRole("button", { name: "Возобновить", exact: true }),
    ).toBeVisible();
    const suspended = (
      await (await page.request.get(listPath)).json()
    ).items.find((sub: { id: string }) => sub.id === chosen.id);
    expect(suspended.expires_at).toBe(before.expires_at);
    expect(suspended.desired_panel_status).toBe("DISABLED");
    expect(suspended.status).toBe("suspension_pending");
    await action("Возобновить", "resume");
    await action("Сверить с панелью", "synchronize");
    const restored = (
      await (await page.request.get(listPath)).json()
    ).items.find((sub: { id: string }) => sub.id === chosen.id);
    expect(restored.expires_at).toBe(before.expires_at);
    expect(restored.restriction.suspended).toBe(false);
    expect(restored.desired_panel_status).toBe("ACTIVE");
    await page.setViewportSize({ width: 390, height: 900 });
    await noOverflow(page);
    await shot(page, "admin-subscription-controls-390");
  } finally {
    const current = (
      await (await page.request.get(listPath)).json()
    ).items.find((sub: { id: string }) => sub.id === chosen.id);
    if (current.restriction.suspended) {
      expect(
        (
          await page.request.post("/api/v1/admin/mfa/verify", {
            headers,
            data: { code: await freshTotp(owner.totp_secret) },
          })
        ).status(),
      ).toBe(200);
      expect(
        (
          await page.request.post(
            `/api/v1/admin/subscriptions/${chosen.id}/resume`,
            {
              headers,
              data: {
                reason:
                  "Восстановление исходного доступа после локальной проверки",
                confirmed: true,
              },
            },
          )
        ).status(),
      ).toBe(200);
    }
  }
});

test("known cash credit pays full service value without an external payment and restores fixture by audited MFA", async ({
  page,
}) => {
  const fixture = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/cash-credit-fixture.json`,
      "utf8",
    ),
  );
  expect(fixture.environment).toBe("synthetic_local");
  expect(fixture.currency).toBe("RUB");
  await login(page, fixture.email);
  const initial = await (await page.request.get("/api/v1/dashboard")).json();
  expect(initial.account.id).toBe(fixture.account_id);
  expect(initial.credit_available).toBe(fixture.amount);
  expect(initial.credit_currency).toBe(fixture.currency);
  expect(/^0+(\.0+)?$/.test(initial.credit_reserved)).toBe(true);
  const priorPayments = initial.payments
    .map((payment: { id: string }) => payment.id)
    .sort();
  let consumed = false;
  try {
    await page.setViewportSize({ width: 390, height: 900 });
    await page
      .getByRole("button", { name: "Добавить подписку", exact: true })
      .click();
    await page
      .getByRole("combobox", { name: "Тариф", exact: true })
      .selectOption(fixture.product_id);
    await page
      .getByRole("dialog")
      .getByRole("radio", { name: "СБП", exact: true })
      .check();
    await page
      .getByLabel("Использовать денежные средства", { exact: true })
      .check();
    await page.getByLabel("Сумма из денежных средств").fill(fixture.amount);
    const quotePending = page.waitForResponse(
      (response) =>
        response.url().endsWith("/quotes") &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("dialog")
      .getByRole("checkbox", { name: /Принимаю оферту/ })
      .check();
    await page
      .getByRole("button", { name: "Рассчитать стоимость", exact: true })
      .click();
    const quoteResponse = await quotePending;
    expect(quoteResponse.status()).toBe(200);
    expect(quoteResponse.request().postDataJSON().credit_amount).toBe(
      fixture.amount,
    );
    const quote = await quoteResponse.json();
    expect(quote.credit_amount).toBe(fixture.amount);
    expect(quote.credit_currency).toBe(fixture.currency);
    expect(quote.total).toBe("0.00");
    expect(quote.bonus_amount).toBe("0.00");
    await shot(page, "cash-credit-quote-390");
    const orderPending = page.waitForResponse(
      (response) =>
        response.url().endsWith("/orders") &&
        response.request().method() === "POST",
    );
    await page
      .getByRole("button", { name: "Подтвердить заказ", exact: true })
      .click();
    const orderResponse = await orderPending;
    expect(orderResponse.status()).toBe(200);
    const order = await orderResponse.json();
    consumed = ["paid", "completed", "fulfilled"].includes(order.status);
    expect(order.total).toBe("0.00");
    expect(order.checkout_url).toBeNull();
    expect(order.status).toBe("paid");
    await expect(page.getByLabel("Подтверждаю тестовую оплату")).toHaveCount(0);
    await page.getByRole("button", { name: "Закрыть", exact: true }).click();
    const paid = await (await page.request.get("/api/v1/dashboard")).json();
    expect(paid.credit_available).toBe("0.00");
    expect(/^0+(\.0+)?$/.test(paid.credit_reserved)).toBe(true);
    expect(
      paid.payments.map((payment: { id: string }) => payment.id).sort(),
    ).toEqual(priorPayments);
    expect(paid.subscriptions.length).toBe(initial.subscriptions.length + 1);
    await page.goto("/#bonuses");
    await expect(page.locator(".cash-wallet")).toContainText("0 ₽");
    await shot(page, "cash-wallet-after-payment-390");
  } finally {
    if (consumed) {
      await page.context().clearCookies();
      await login(page, "owner@sunrise.test");
      const owner = JSON.parse(
        await readFile(
          `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
          "utf8",
        ),
      );
      const session = await (await page.request.get("/api/v1/auth/me")).json();
      const headers = { "X-CSRF-Token": session.csrf_token };
      expect(
        (
          await page.request.post("/api/v1/admin/mfa/verify", {
            headers,
            data: { code: await freshTotp(owner.totp_secret) },
          })
        ).status(),
      ).toBe(200);
      const restore = await page.request.post(
        `/api/v1/admin/users/${fixture.account_id}/credit`,
        {
          headers,
          data: {
            amount: fixture.amount,
            currency: fixture.currency,
            provenance: "owner_adjustment",
            operation_id: crypto.randomUUID(),
            reason:
              "Восстановление синтетического денежного остатка после проверки оплаты",
            confirmed: true,
          },
        },
      );
      expect(restore.status()).toBe(200);
      expect((await restore.json()).credit_available).toBe(fixture.amount);
    }
  }
});

test("BIGINT survives browser user search, detail and paired audited bonus mutations", async ({
  page,
}) => {
  const fixture = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/bigint-fixture.json`,
      "utf8",
    ),
  );
  const id: string = fixture.id;
  expect(id).toBe("9007199254740993");
  await login(page, "owner@sunrise.test");
  await page.goto("/admin");
  const owner = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
      "utf8",
    ),
  );
  if (
    !(await (await page.request.get("/api/v1/auth/me")).json()).mfa_verified
  ) {
    await page
      .getByLabel("Код аутентификатора", { exact: true })
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByRole("button", { name: "Подтвердить вход", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Обзор", exact: true }),
    ).toBeVisible();
  }
  await page.goto("/admin#users");
  await page.getByRole("textbox", { name: "Поиск записей" }).fill(id);
  await page.getByRole("button", { name: "Найти", exact: true }).click();
  const row = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: id, exact: true }) });
  await expect(row).toBeVisible();
  const before = await (
    await page.request.get(`/api/v1/admin/users/${id}`)
  ).json();
  expect(before.account.id).toBe(id);
  expect(typeof before.account.id).toBe("string");
  expect(Number(before.account.bonus_balance)).toBe(0);
  await row.getByRole("button", { name: "Открыть", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(id);
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  for (const amount of ["0.01", "-0.01"]) {
    const currentRow = page
      .getByRole("row")
      .filter({ has: page.getByRole("cell", { name: id, exact: true }) });
    await currentRow
      .getByRole("button", { name: "Бонусы", exact: true })
      .click();
    await page.getByLabel("Изменение бонусного баланса").fill(amount);
    await page
      .getByRole("button", { name: "Сохранить изменения", exact: true })
      .click();
    await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
    await page
      .getByLabel("Свежий код аутентификатора")
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByLabel("Я проверил изменения и подтверждаю действие.")
      .check();
    const pending = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        new URL(request.url()).pathname === `/api/v1/admin/users/${id}/bonus`,
    );
    await page
      .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
      .click();
    const request = await pending;
    expect(new URL(request.url()).pathname).toContain(id);
    expect(request.postDataJSON().amount).toBe(amount);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Пользователи", exact: true }),
    ).toBeVisible();
    await page.getByRole("textbox", { name: "Поиск записей" }).fill(id);
    await page.getByRole("button", { name: "Найти", exact: true }).click();
  }
  const after = await (
    await page.request.get(`/api/v1/admin/users/${id}`)
  ).json();
  expect(after.account.id).toBe(id);
  expect(Number(after.account.bonus_balance)).toBe(0);
  expect(after.ledger).toHaveLength(before.ledger.length + 2);
});
test("admin changes use fresh MFA, explicit confirmation, reason and persist content safely", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await login(page, "owner@sunrise.test");
  await page.goto("/admin");
  const owner = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
      "utf8",
    ),
  );
  const session = await (await page.request.get("/api/v1/auth/me")).json();
  if (!session.mfa_verified) {
    await page
      .getByLabel("Код аутентификатора", { exact: true })
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByRole("button", { name: "Подтвердить вход", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Обзор", exact: true }),
    ).toBeVisible();
  }
  await page.goto("/admin#content");
  const originalContentResponse = await page.request.get(
    "/api/v1/admin/content?search=ui_quality_check&page_size=200",
  );
  expect(originalContentResponse.status()).toBe(200);
  const originalContent = (await originalContentResponse.json()).items.find(
    (item: { id: string }) => item.id === "content:ui_quality_check",
  );
  const originalNodes = originalContent?.content?.nodes || [];
  await page
    .getByRole("button", { name: "Новый материал", exact: true })
    .click();
  await page.getByLabel("Ключ материала").fill("ui_quality_check");
  await page
    .getByLabel("Быстро добавить абзац")
    .fill("Проверка безопасного контента <script>alert(1)</script> ☀️");
  await page
    .getByRole("button", { name: "Добавить абзац", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "<script>alert(1)</script>",
  );
  await page
    .getByRole("button", { name: "Сохранить изменения", exact: true })
    .click();
  await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await page
    .getByLabel("Свежий код аутентификатора")
    .fill(await freshTotp(owner.totp_secret));
  await page.getByLabel("Я проверил изменения и подтверждаю действие.").check();
  await page
    .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("table")).toContainText(
    "content:ui_quality_check",
  );
  // Restore the dedicated synthetic content key through the same audited API.
  const freshSession = await (await page.request.get("/api/v1/auth/me")).json();
  const headers = { "X-CSRF-Token": freshSession.csrf_token };
  expect(
    (
      await page.request.post("/api/v1/admin/mfa/verify", {
        headers,
        data: { code: await freshTotp(owner.totp_secret) },
      })
    ).status(),
  ).toBe(200);
  expect(
    (
      await page.request.patch("/api/v1/admin/content/ui_quality_check", {
        headers,
        data: {
          nodes: originalNodes,
          reason: "Восстановление исходного тестового материала после проверки",
          confirmed: true,
        },
      })
    ).status(),
  ).toBe(200);
  await page.goto("/admin#catalog");
  const row = page.getByRole("article").filter({
    has: page.getByRole("heading", { name: "Стандартный", exact: true }),
  });
  await row.getByRole("button", { name: "Изменить", exact: true }).click();
  await page
    .getByRole("button", { name: "Сохранить тариф", exact: true })
    .click();
  await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await page
    .getByLabel("Свежий код аутентификатора")
    .fill(await freshTotp(owner.totp_secret));
  await page.getByLabel("Я проверил изменения и подтверждаю действие.").check();
  await page
    .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("maintenance blocks new checkout without losing choice and keeps account reads available", async ({
  page,
}) => {
  await login(page, "owner@sunrise.test");
  await page.goto("/admin");
  const owner = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
      "utf8",
    ),
  );
  let session = await (await page.request.get("/api/v1/auth/me")).json();
  if (!session.mfa_verified) {
    await page
      .getByLabel("Код аутентификатора", { exact: true })
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByRole("button", { name: "Подтвердить вход", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Обзор", exact: true }),
    ).toBeVisible();
  }
  const initialHealth = await (
    await page.request.get("/api/v1/admin/operations/health")
  ).json();
  expect(initialHealth.maintenance.enabled).toBe(false);
  try {
    await page.goto("/admin#operations");
    await page.locator("summary", { hasText: "Состояние операций" }).click();
    await page
      .getByRole("button", { name: "Включить обслуживание", exact: true })
      .click();
    await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
    await page
      .getByLabel("Свежий код аутентификатора")
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByLabel("Я проверил изменения и подтверждаю действие.")
      .check();
    await page
      .getByRole("button", { name: "Подтвердить изменение", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Завершить обслуживание", exact: true }),
    ).toBeVisible();
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Мой VPN", exact: true }),
    ).toBeVisible();
    expect((await page.request.get("/api/v1/dashboard")).status()).toBe(200);
    await page
      .getByRole("button", { name: "Добавить подписку", exact: true })
      .click();
    const product = page.getByRole("combobox", { name: "Тариф", exact: true });
    await product.selectOption("standard");
    await page
      .getByRole("dialog")
      .getByRole("radio", { name: "СБП", exact: true })
      .check();
    await page
      .getByRole("dialog")
      .getByRole("checkbox", { name: /Принимаю оферту/ })
      .check();
    await page
      .getByRole("button", { name: "Рассчитать стоимость", exact: true })
      .click();
    await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
      /обслуживан/i,
    );
    await expect(product).toHaveValue("standard");
  } finally {
    session = await (await page.request.get("/api/v1/auth/me")).json();
    const headers = { "X-CSRF-Token": session.csrf_token };
    expect(
      (
        await page.request.post("/api/v1/admin/mfa/verify", {
          headers,
          data: { code: await freshTotp(owner.totp_secret) },
        })
      ).status(),
    ).toBe(200);
    expect(
      (
        await page.request.patch("/api/v1/admin/operations/maintenance", {
          headers,
          data: {
            enabled: false,
            confirmed: true,
            reason:
              "Восстановление исходного локального режима после браузерной проверки",
          },
        })
      ).status(),
    ).toBe(200);
    expect(
      (await (await page.request.get("/api/v1/admin/operations/health")).json())
        .maintenance.enabled,
    ).toBe(false);
  }
});

test("admin catalog changes reach the real database and a three-tariff landing", async ({
  page,
}) => {
  await login(page, "owner@sunrise.test");
  const owner = JSON.parse(
    await readFile(
      `${root}.private/e2e-ui-fixtures/${runId}/owner.json`,
      "utf8",
    ),
  );
  const session = await (await page.request.get("/api/v1/auth/me")).json();
  const headers = { "X-CSRF-Token": session.csrf_token };
  async function authorize() {
    expect(
      (
        await page.request.post("/api/v1/admin/mfa/verify", {
          headers,
          data: { code: await freshTotp(owner.totp_secret) },
        })
      ).status(),
    ).toBe(200);
  }
  const before = await (await page.request.get("/api/v1/catalog")).json();
  expect(before.products).toHaveLength(2);
  await authorize();
  const created = await page.request.post("/api/v1/admin/catalog", {
    headers,
    data: {
      code: `e2e_v6_${Date.now()}`,
      name: "Проверка третьего тарифа V6",
      tier_level: 3,
      price: "499.00",
      currency: "RUB",
      included_devices: 4,
      available: true,
      visible_landing: true,
      visible_bot: true,
      semantic_role: "test",
      sort_order: 100,
      reason: "Изолированная браузерная проверка динамического каталога",
      confirmed: true,
    },
  });
  expect(created.status()).toBe(200);
  const product = await created.json();
  try {
    await page.goto("/admin#catalog");
    const card = page.getByRole("article").filter({
      has: page.locator(".eyebrow").filter({ hasText: product.code }),
    });
    await card.getByRole("button", { name: "Изменить", exact: true }).click();
    await page.getByLabel("Базовая цена", { exact: true }).fill("490.00");
    await page
      .getByRole("button", { name: "Сохранить тариф", exact: true })
      .click();
    await expect(page.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
    await page
      .getByLabel("Свежий код аутентификатора")
      .fill(await freshTotp(owner.totp_secret));
    await page
      .getByLabel("Я проверил изменения и подтверждаю действие.")
      .check();
    await page
      .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    const catalog = await (await page.request.get("/api/v1/catalog")).json();
    expect(
      catalog.products.find((p: { id: string }) => p.id === product.id).price,
    ).toBe("490.00");
    const landing = await page.context().newPage();
    try {
      await landing.goto("http://127.0.0.1:3110/");
      await expect(landing.locator(".sunrise-tariff")).toHaveCount(3);
      await expect(
        landing.locator(`[data-product-id="${product.id}"]`),
      ).toContainText(/490\s*₽/);
      await landing.setViewportSize({ width: 390, height: 900 });
      await noOverflow(landing);
      await a11y(landing, "v6-live-three-tariffs");
      await shot(landing, "v6-live-three-tariffs-390");
    } finally {
      await landing.close();
    }
  } finally {
    await authorize();
    expect(
      (
        await page.request.patch(`/api/v1/admin/catalog/${product.id}`, {
          headers,
          data: {
            available: false,
            reason: "Архивация только созданного тестового тарифа",
            confirmed: true,
          },
        })
      ).status(),
    ).toBe(200);
    expect(
      (await (await page.request.get("/api/v1/catalog")).json()).products,
    ).toHaveLength(2);
  }
});
