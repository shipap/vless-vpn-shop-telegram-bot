import { syntheticInstance } from "./instance-fixture";
import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";
import { ownerStaffFixture } from "./staff-fixture";

const documents = [
  { document_type: "offer", version: "v1", url: "https://example.test/offer" },
  {
    document_type: "privacy",
    version: "v1",
    url: "https://example.test/privacy",
  },
];
const session = {
  account: {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
  },
  csrf_token: "synthetic",
  is_admin: false,
  mfa_verified: false,
};

async function sdk(page: Page, initData = "synthetic-signed-init-data") {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: `
    window.__telegramCalls={ready:0,expand:0}; window.__telegramEvents={};
    window.Telegram={WebApp:{initData:${JSON.stringify(initData)},initDataUnsafe:{user:{id:123}},
      safeAreaInset:{top:20,bottom:10,left:0,right:0},contentSafeAreaInset:{top:46,bottom:0,left:0,right:0},viewportStableHeight:800,
      ready(){window.__telegramCalls.ready++},expand(){window.__telegramCalls.expand++},
      onEvent(name,cb){window.__telegramEvents[name]=cb},offEvent(name,cb){if(window.__telegramEvents[name]===cb)delete window.__telegramEvents[name]}
    }};
  `,
    }),
  );
}

async function apis(
  page: Page,
  options: {
    loggedIn?: boolean;
    bypass?: boolean;
    siteKey?: string;
    miniApp?: boolean;
    oidc?: boolean;
  } = {},
) {
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET")
      writes.push({ path, body: route.request().postDataJSON() });
    if (path === "/auth/me")
      return route.fulfill(
        options.loggedIn
          ? { json: session }
          : { status: 401, json: { error: { code: "auth_required" } } },
      );
    let json: unknown = {};
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        local_test: true,
        turnstile_test_bypass: options.bypass ?? false,
        turnstile_site_key: options.siteKey ?? null,
        telegram_miniapp_enabled: options.miniApp ?? false,
        telegram_login_enabled: options.oidc ?? false,
      };
    if (path === "/legal" || path === "/legal/status")
      json = { configured: true, accepted: true, documents, missing: [] };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: true, documents },
      };
    if (path === "/promos/active") json = null;
    if (path === "/dashboard")
      json = {
        account: session.account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path === "/auth/email/request")
      json = { challenge_id: "synthetic-challenge", resend_after: 0 };
    return route.fulfill({ json });
  });
  return writes;
}

test("Mini App SDK starts once, adds system and content safe areas, and survives navigation", async ({
  page,
}) => {
  await sdk(page);
  await apis(page, { loggedIn: true });
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute(
    "data-telegram-mini-app",
    "true",
  );
  expect(
    await page.evaluate(
      () => (window as unknown as { __telegramCalls: unknown }).__telegramCalls,
    ),
  ).toEqual({ ready: 1, expand: 1 });
  expect(
    await page
      .locator("#root")
      .evaluate((element) => getComputedStyle(element).paddingTop),
  ).toBe("66px");
  await page.goto("/#account");
  await expect(
    page.getByRole("heading", { name: "Ваш аккаунт", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => (window as unknown as { __telegramCalls: unknown }).__telegramCalls,
    ),
  ).toEqual({ ready: 1, expand: 1 });
});

test("local_test does not bypass Turnstile; each email request consumes the actual widget token", async ({
  page,
}) => {
  await sdk(page, "");
  await page.route(
    "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `
    window.__captcha={resets:0}; window.turnstile={ready(cb){cb()},render(el,options){window.__captcha.options=options;options.callback('synthetic-token-1');return 'widget';},reset(){window.__captcha.resets++;},remove(){}};
  `,
      }),
  );
  const writes = await apis(page, { siteKey: "synthetic-site-key" });
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Получить код", exact: true }).click();
  await expect(page.getByLabel("Код из письма", { exact: true })).toBeVisible();
  expect(
    writes.find((write) => write.path === "/auth/email/request")?.body,
  ).toEqual({
    email: "synthetic@example.test",
    turnstile_token: "synthetic-token-1",
  });
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __captcha: { resets: number } }).__captcha
          .resets,
    ),
  ).toBe(1);
  await expect(
    page.getByRole("button", { name: "Отправить код ещё раз", exact: true }),
  ).toBeDisabled();
});

test("missing widget configuration cannot silently use the local-test marker", async ({
  page,
}) => {
  await sdk(page, "");
  const writes = await apis(page);
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await expect(
    page.getByRole("button", { name: "Получить код", exact: true }),
  ).toBeDisabled();
  expect(writes).toHaveLength(0);
});

test("asynchronously loaded Turnstile renders directly without the incompatible ready API", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await sdk(page, "");
  await page.route(
    "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `
    window.__captcha={renders:0};window.turnstile={
      ready(){throw new Error('[Cloudflare Turnstile] Remove async/defer from the Turnstile api.js script tag before using turnstile.ready().')},
      render(el,options){window.__captcha.renders++;window.__captcha.language=options.language;el.innerHTML='<div aria-label="Проверка безопасности пройдена">Проверка пройдена</div>';options.callback('synthetic-async-token');return 'widget'},
      reset(){},remove(){}
    };`,
      }),
  );
  await apis(page, { siteKey: "synthetic-site-key" });
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await expect(
    page.getByRole("button", { name: "Получить код", exact: true }),
  ).toBeEnabled({ timeout: 5000 });
  await expect(
    page.getByLabel("Проверка безопасности пройдена", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __captcha: { renders: number } }).__captcha
          .renders,
    ),
  ).toBe(1);
  expect(errors).toEqual([]);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __captcha: { language?: string } }).__captcha
          .language,
    ),
  ).toBe("ru");
});

test("first Mini App login waits for explicit legal consent and sends only signed initData", async ({
  page,
}) => {
  await sdk(page);
  await apis(page, { miniApp: true });
  let authenticated = false;
  const attempts: Record<string, unknown>[] = [];
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(
      authenticated
        ? { json: session }
        : { status: 401, json: { error: { code: "auth_required" } } },
    ),
  );
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    const body = route.request().postDataJSON();
    attempts.push(body);
    if (body.legal_accepted !== true)
      return route.fulfill({
        status: 409,
        json: {
          error: {
            code: "legal_consent_required",
            message: "Подтвердите согласие.",
          },
        },
      });
    authenticated = true;
    return route.fulfill({ json: session });
  });
  await page.goto("/");
  await expect(page.getByRole("checkbox")).toBeVisible();
  await mkdir(".artifacts/ui/v10-telegram-auth", { recursive: true });
  for (const width of [1440, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(() => document.fonts.ready);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(
      accessibility.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      ),
    ).toEqual([]);
    await page.screenshot({
      path: `.artifacts/ui/v10-telegram-auth/miniapp-consent-${width}.png`,
      fullPage: true,
    });
  }
  expect(attempts).toEqual([{ init_data: "synthetic-signed-init-data" }]);
  await expect(page.locator(".dashboard-layout")).toHaveCount(0);
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Продолжить в Telegram", exact: true })
    .click();
  await expect(page.locator(".dashboard-layout")).toBeVisible();
  expect(attempts[1]).toEqual({
    init_data: "synthetic-signed-init-data",
    legal_accepted: true,
    documents,
  });
  await page.goto("/#account");
  expect(attempts).toHaveLength(2);
});

test("already accepted Mini App account resumes without another checkbox", async ({
  page,
}) => {
  await sdk(page);
  await apis(page, { miniApp: true });
  let authenticated = false;
  const attempts: unknown[] = [];
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(
      authenticated
        ? { json: session }
        : { status: 401, json: { error: { code: "auth_required" } } },
    ),
  );
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    attempts.push(route.request().postDataJSON());
    authenticated = true;
    return route.fulfill({ json: session });
  });
  await page.goto("/");
  await expect(page.locator(".dashboard-layout")).toBeVisible();
  expect(attempts).toEqual([{ init_data: "synthetic-signed-init-data" }]);
  await expect(page.getByRole("checkbox")).toHaveCount(0);
});

test("website Telegram login requires consent and starts the server OIDC flow", async ({
  page,
}) => {
  await sdk(page, "");
  const writes = await apis(page, { bypass: true, oidc: true });
  let startBody: unknown;
  await page.route("**/api/v1/auth/telegram/start", (route) => {
    startBody = route.request().postDataJSON();
    return route.fulfill({
      json: {
        authorization_url: "https://oauth.telegram.org/auth?synthetic=1",
      },
    });
  });
  await page.route("https://oauth.telegram.org/auth?synthetic=1", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<title>Telegram</title>Синтетическая страница провайдера",
    }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Войти через Telegram", exact: true }),
  ).toBeDisabled();
  const telegramBox = await page
    .getByRole("button", {
      name: "Войти через Telegram",
      exact: true,
    })
    .boundingBox();
  const emailBox = await page
    .getByRole("textbox", { name: "Email", exact: true })
    .boundingBox();
  expect(telegramBox!.y + telegramBox!.height).toBeLessThan(emailBox!.y);
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Войти через Telegram", exact: true })
    .click();
  await expect(page).toHaveURL("https://oauth.telegram.org/auth?synthetic=1");
  expect(startBody).toEqual({ legal_accepted: true, documents });
  expect(
    writes.filter((write) => write.path === "/auth/email/request"),
  ).toHaveLength(0);
});

test("Mini App logout does not immediately log the user in again", async ({
  page,
}) => {
  await sdk(page);
  await apis(page, { miniApp: true });
  let authenticated = true;
  let attempts = 0;
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(
      authenticated
        ? { json: session }
        : { status: 401, json: { error: { code: "auth_required" } } },
    ),
  );
  await page.route("**/api/v1/auth/logout", (route) => {
    authenticated = false;
    return route.fulfill({ json: {} });
  });
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    attempts++;
    return route.fulfill({ json: session });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Продолжить в Telegram", exact: true }),
  ).toBeVisible();
  expect(attempts).toBe(0);
});

test("Mini App activation checks the session and clears expired account data", async ({
  page,
}) => {
  await sdk(page);
  await apis(page);
  let expired = false;
  let checks = 0;
  await page.route("**/api/v1/auth/me", (route) => {
    checks++;
    return route.fulfill(
      expired
        ? { status: 401, json: { error: { code: "auth_required" } } }
        : { json: session },
    );
  });
  await page.goto("/");
  await expect(page.locator(".dashboard-layout")).toBeVisible();
  expired = true;
  await page.evaluate(() =>
    (
      window as unknown as { __telegramEvents: { activated: () => void } }
    ).__telegramEvents.activated(),
  );
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  expect(checks).toBe(2);
  await expect(page.locator(".dashboard-layout")).toHaveCount(0);
});

test("landing initializes the Mini App SDK without starting an auth exchange", async ({
  page,
}) => {
  await sdk(page);
  const writes = await apis(page, { miniApp: true });
  await page.goto("http://127.0.0.1:3100/");
  await expect(page.locator("body")).toHaveAttribute(
    "data-telegram-mini-app",
    "true",
  );
  expect(
    await page.evaluate(
      () => (window as unknown as { __telegramCalls: unknown }).__telegramCalls,
    ),
  ).toEqual({ ready: 1, expand: 1 });
  expect(writes).toHaveLength(0);
});

test("website login remains accessible and fits desktop and small phones", async ({
  page,
}) => {
  await sdk(page, "");
  await apis(page, { bypass: true });
  await mkdir(".artifacts/ui/v10-telegram-auth", { recursive: true });
  for (const width of [1440, 390, 360]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(
      accessibility.violations.filter((item) =>
        ["serious", "critical"].includes(item.impact ?? ""),
      ),
    ).toEqual([]);
    await page.screenshot({
      path: `.artifacts/ui/v10-telegram-auth/website-${width}.png`,
      fullPage: true,
    });
  }
});

test("a failed email request still resets its single-use Turnstile token", async ({
  page,
}) => {
  await sdk(page, "");
  await page.route(
    "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `window.__captcha={resets:0};window.turnstile={ready(cb){cb()},render(el,options){options.callback('synthetic-token');return 'widget'},reset(){window.__captcha.resets++},remove(){}};`,
      }),
  );
  await apis(page, { siteKey: "synthetic-site-key" });
  await page.route("**/api/v1/auth/email/request", (route) =>
    route.fulfill({
      status: 429,
      json: {
        error: { code: "resend_cooldown", message: "Повторите попытку позже." },
      },
    }),
  );
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Получить код", exact: true }).click();
  await expect(
    page.getByText("Повторите попытку позже.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Получить код", exact: true }),
  ).toBeDisabled();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { __captcha: { resets: number } }).__captcha
          .resets,
    ),
  ).toBe(1);
});

test("Mini App stale documents clear consent and require a new explicit acceptance", async ({
  page,
}) => {
  await sdk(page);
  await apis(page, { miniApp: true });
  let attempts = 0;
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    attempts++;
    return route.fulfill({
      status: 409,
      json: {
        error: {
          code: attempts === 1 ? "legal_consent_required" : "legal_stale",
          message: "Документы обновлены.",
        },
      },
    });
  });
  await page.goto("/");
  await page.getByRole("checkbox").check();
  await page
    .getByRole("button", { name: "Продолжить в Telegram", exact: true })
    .click();
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(
    page.getByRole("button", { name: "Продолжить в Telegram", exact: true }),
  ).toBeDisabled();
  expect(attempts).toBe(2);
});

test("Mini App offers an explicit in-memory switch to email login", async ({
  page,
}) => {
  test.setTimeout(20000);
  await sdk(page);
  await apis(page, { miniApp: true, bypass: true });
  let attempts = 0;
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    attempts++;
    return route.fulfill({
      status: 409,
      json: { error: { code: "legal_consent_required" } },
    });
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Войти по email", exact: true })
    .click();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  await expect(page.locator("body")).toHaveAttribute(
    "data-telegram-mini-app",
    "true",
  );
  expect(attempts).toBe(1);
  expect(new URL(page.url()).search).toBe("");
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("Mini App admin fresh email recovery reaches email login without another Telegram exchange", async ({
  page,
}) => {
  test.setTimeout(20000);
  await page.route("**/admin", async (route) =>
    route.fulfill({
      response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
    }),
  );
  await sdk(page);
  await apis(page, { miniApp: true, bypass: true });
  let authenticated = true;
  let attempts = 0;
  await page.route("**/api/v1/auth/me", (route) =>
    route.fulfill(
      authenticated
        ? { json: { ...session, is_admin: true, ...ownerStaffFixture } }
        : { status: 401, json: { error: { code: "auth_required" } } },
    ),
  );
  await page.route("**/api/v1/admin/mfa/status", (route) =>
    route.fulfill({
      json: {
        enrolled: false,
        enrollment_required: true,
        trusted: false,
        trust_expires_at: null,
      },
    }),
  );
  await page.route("**/api/v1/admin/mfa/enroll", (route) =>
    route.fulfill({
      status: 403,
      json: {
        error: {
          code: "fresh_login_required",
          message: "Подтвердите email заново.",
        },
      },
    }),
  );
  await page.route("**/api/v1/auth/logout", (route) => {
    authenticated = false;
    return route.fulfill({ json: {} });
  });
  await page.route("**/api/v1/auth/telegram/miniapp", (route) => {
    attempts++;
    return route.fulfill({ json: session });
  });
  await page.goto("/admin");
  await page
    .getByRole("button", { name: "Настроить аутентификатор", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Подтвердить почту повторно", exact: true })
    .click();
  await expect(page.getByLabel("Email", { exact: true })).toBeVisible();
  await expect(page).toHaveURL("http://127.0.0.1:3200/");
  expect(attempts).toBe(0);
  await expect(page.locator("body")).toHaveAttribute(
    "data-telegram-mini-app",
    "true",
  );
});
