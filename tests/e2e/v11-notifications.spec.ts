import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
test.beforeEach(async ({ page }) => {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
});
import AxeBuilder from "@axe-core/playwright";

async function notificationFixture(page: Page) {
  let unread = true;
  let incoming = false;
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  await page.addInitScript(() => {
    Object.defineProperty(Notification, "permission", {
      configurable: true,
      value: "default",
    });
    Notification.requestPermission = async () => {
      throw new Error("Permission must require an explicit click");
    };
    class SyntheticEvents {
      static current: SyntheticEvents;
      handlers = new Map<string, EventListener>();
      constructor() {
        SyntheticEvents.current = this;
      }
      addEventListener(name: string, handler: EventListener) {
        this.handlers.set(name, handler);
      }
      removeEventListener(name: string) {
        this.handlers.delete(name);
      }
      close() {}
    }
    Object.assign(window, {
      EventSource: SyntheticEvents,
      emitNotification: () =>
        SyntheticEvents.current.handlers.get("notification")?.(
          new MessageEvent("notification", { data: '{"id":"synthetic-new"}' }),
        ),
    });
  });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: false,
        mfa_verified: false,
        staff_role: null,
        capabilities: [],
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, web_push_public_key: null };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path === "/legal/status")
      json = { accepted: true, configured: false, missing: [], documents: [] };
    if (path === "/notifications/synthetic-old/read") unread = false;
    if (path === "/notifications/read-all") unread = false;
    if (path === "/notifications")
      json = {
        items: [
          {
            id: "synthetic-old",
            kind: "security",
            title: "Новый вход",
            body: "Проверьте свои сессии",
            severity: "info",
            created_at: "2026-10-08T09:00:00Z",
            read_at: unread ? null : "2026-10-08T10:00:00Z",
            target: "/#account",
          },
          ...(incoming
            ? [
                {
                  id: "synthetic-new",
                  kind: "subscription",
                  title: "Подписка продлена",
                  body: "Оплаченный доступ сохранён",
                  severity: "success",
                  created_at: "2026-10-08T10:00:00Z",
                  read_at: null,
                  target: "/#home",
                },
              ]
            : []),
        ],
        unread_count: Number(unread) + Number(incoming),
      };
    if (path === "/notifications/preferences")
      json = { browser_enabled: false, push_subscriptions: [] };
    await route.fulfill({ json });
  });
  return () => {
    incoming = true;
  };
}

test("inbox marks own message read without removing durable history or asking browser permission", async ({
  page,
}) => {
  await notificationFixture(page);
  await page.goto("/#home");
  await page
    .getByRole("button", { name: /Уведомления.*1 непрочитанное/ })
    .click();
  const dialog = page.getByRole("dialog", { name: "Уведомления" });
  await expect(dialog).toContainText("Новый вход");
  await dialog.getByRole("button", { name: "Отметить прочитанным" }).click();
  await expect(dialog).toContainText("Новый вход");
  await expect(
    dialog.getByRole("button", { name: "Отметить прочитанным" }),
  ).toHaveCount(0);
  await expect(dialog).toContainText("Уведомления в браузере не настроены");
});

test("SSE notification refetch creates one accessible dismissible toast while the inbox persists", async ({
  page,
}) => {
  const send = await notificationFixture(page);
  await page.goto("/#home");
  await expect(
    page.getByRole("button", { name: /Уведомления.*1 непрочитанное/ }),
  ).toBeVisible();
  send();
  await page.evaluate(() =>
    (window as unknown as { emitNotification: () => void }).emitNotification(),
  );
  const toast = page.locator(".notification-toast-stack").getByRole("status");
  await expect(toast).toContainText("Подписка продлена");
  await page.evaluate(() =>
    (window as unknown as { emitNotification: () => void }).emitNotification(),
  );
  await expect(toast).toHaveCount(1);
  await toast.getByRole("button", { name: "Закрыть уведомление" }).click();
  await expect(toast).toHaveCount(0);
  await page.getByRole("button", { name: /Уведомления.*2/ }).click();
  await expect(page.getByRole("dialog")).toContainText("Подписка продлена");
});

test("browser push asks permission only on click and enables only after native subscribe plus server registration", async ({
  page,
}) => {
  await notificationFixture(page);
  await page.addInitScript(() => {
    let permissionRequests = 0;
    const subscription = {
      endpoint: "https://fcm.googleapis.com/fcm/send/synthetic",
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/synthetic",
        keys: { p256dh: "synthetic-public-key", auth: "synthetic-auth" },
      }),
      unsubscribe: async () => true,
    };
    const registration = {
      pushManager: {
        getSubscription: async () => null,
        subscribe: async () => subscription,
      },
    };
    Object.defineProperty(Notification, "permission", {
      configurable: true,
      get: () => (permissionRequests ? "granted" : "default"),
    });
    Notification.requestPermission = async () => {
      permissionRequests++;
      return "granted";
    };
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        getRegistration: async () => undefined,
        register: async () => registration,
        ready: Promise.resolve(registration),
      },
    });
    Object.assign(window, { permissionRequests: () => permissionRequests });
  });
  const writes: unknown[] = [];
  const patches: unknown[] = [];
  await page.route("**/api/v1/notifications/preferences", (route) => {
    if (route.request().method() === "PATCH") {
      patches.push(route.request().postDataJSON());
      return route.fulfill({ json: { browser_enabled: true } });
    }
    return route.fulfill({
      json: { browser_enabled: false, push_subscriptions: [] },
    });
  });
  await page.route("**/api/v1/public-config", (route) =>
    route.fulfill({
      json: {
        instance: syntheticInstance,
        web_push_public_key:
          "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      },
    }),
  );
  await page.route("**/api/v1/notifications/push", (route) => {
    writes.push(route.request().postDataJSON());
    return route.fulfill({
      json: { id: "synthetic-push", browser_enabled: true },
    });
  });
  await page.goto("/#home");
  await page.getByRole("button", { name: /Уведомления.*1/ }).click();
  expect(
    await page.evaluate(() =>
      (
        window as unknown as { permissionRequests: () => number }
      ).permissionRequests(),
    ),
  ).toBe(0);
  await page
    .getByRole("button", { name: "Разрешить уведомления в браузере" })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Уведомления включены на этом устройстве",
  );
  expect(writes).toEqual([
    {
      endpoint: "https://fcm.googleapis.com/fcm/send/synthetic",
      keys: { p256dh: "synthetic-public-key", auth: "synthetic-auth" },
    },
  ]);
  expect(patches).toEqual([{ browser_enabled: true }]);
  expect(
    await page.evaluate(() =>
      (
        window as unknown as { permissionRequests: () => number }
      ).permissionRequests(),
    ),
  ).toBe(1);
  await page.route("**/api/v1/notifications/push/synthetic-push", (route) =>
    route.fulfill({ json: { status: "disabled" } }),
  );
  await page
    .getByRole("button", { name: "Отключить на этом устройстве" })
    .click();
  await expect(page.getByRole("dialog")).not.toContainText(
    "Уведомления включены на этом устройстве",
  );
  await expect(
    page.getByRole("button", { name: "Включить на этом устройстве" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Включить на этом устройстве" })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Уведомления включены на этом устройстве",
  );
  expect(writes).toHaveLength(2);
  expect(patches).toHaveLength(2);
});

test("server-disabled push after account merge stays disabled despite existing granted native subscription until explicit click", async ({
  page,
}) => {
  await notificationFixture(page);
  await page.addInitScript(() => {
    Object.defineProperty(Notification, "permission", {
      configurable: true,
      value: "granted",
    });
    const native = {
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/synthetic-old",
        keys: { p256dh: "synthetic", auth: "synthetic" },
      }),
      unsubscribe: async () => true,
    };
    const registration = {
      pushManager: { getSubscription: async () => native },
    };
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        getRegistration: async () => registration,
        register: async () => registration,
        ready: Promise.resolve(registration),
      },
    });
  });
  await page.route("**/api/v1/public-config", (route) =>
    route.fulfill({
      json: {
        instance: syntheticInstance,
        web_push_public_key:
          "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      },
    }),
  );
  let posts = 0;
  let patches = 0;
  await page.route("**/api/v1/notifications/preferences", (route) => {
    if (route.request().method() === "PATCH") {
      patches++;
      return route.fulfill({ json: { browser_enabled: true } });
    }
    return route.fulfill({
      json: { browser_enabled: false, push_subscriptions: [] },
    });
  });
  await page.route("**/api/v1/notifications/push", (route) => {
    posts++;
    return route.fulfill({
      json: { id: "synthetic-reactivated", browser_enabled: false },
    });
  });
  await page.goto("/#home");
  await page.getByRole("button", { name: /Уведомления.*1/ }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Разрешение получено. Включите доставку",
  );
  expect(posts).toBe(0);
  expect(patches).toBe(0);
  await page
    .getByRole("button", { name: "Включить на этом устройстве" })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Уведомления включены на этом устройстве",
  );
  expect(posts).toBe(1);
  expect(patches).toBe(1);
});

for (const permission of ["denied", "unsupported"] as const) {
  test(`browser permission ${permission} retains inbox and never attempts permission request`, async ({
    page,
  }) => {
    await notificationFixture(page);
    await page.addInitScript((state) => {
      if (state === "unsupported")
        Object.defineProperty(window, "Notification", {
          configurable: true,
          value: undefined,
        });
      else
        Object.defineProperty(Notification, "permission", {
          configurable: true,
          value: "denied",
        });
    }, permission);
    await page.goto("/#home");
    await page.getByRole("button", { name: /Уведомления.*1/ }).click();
    await expect(page.getByRole("dialog")).toContainText(
      permission === "denied"
        ? "запрещены в настройках"
        : "не поддерживает уведомления",
    );
    await expect(
      page.getByRole("button", { name: "Разрешить уведомления в браузере" }),
    ).toBeDisabled();
    await expect(page.getByRole("dialog")).toContainText("Новый вход");
  });
}

for (const remembered of [
  "synthetic-own",
  "synthetic-foreign",
  "synthetic-stale",
] as const) {
  test(`native restore uses own remembered ID ${remembered} without registration or enable writes`, async ({
    page,
  }) => {
    await notificationFixture(page);
    await page.addInitScript((id) => {
      localStorage.setItem("sunrise.push.device.v1.9007199254740993123", id);
      Object.defineProperty(Notification, "permission", {
        configurable: true,
        value: "granted",
      });
      Object.defineProperty(navigator, "serviceWorker", {
        configurable: true,
        value: {
          getRegistration: async () => ({
            pushManager: {
              getSubscription: async () => ({ unsubscribe: async () => true }),
            },
          }),
        },
      });
    }, remembered);
    let writes = 0;
    await page.route("**/api/v1/notifications/preferences", (route) => {
      if (route.request().method() !== "GET") writes++;
      return route.fulfill({
        json: {
          browser_enabled: true,
          push_subscriptions: [{ id: "synthetic-own", created_at: null }],
        },
      });
    });
    await page.route("**/api/v1/notifications/push", (route) => {
      writes++;
      return route.fulfill({ json: {} });
    });
    await page.goto("/#home");
    await page.getByRole("button", { name: /Уведомления.*1/ }).click();
    if (remembered === "synthetic-own")
      await expect(page.getByRole("dialog")).toContainText(
        "Уведомления включены на этом устройстве",
      );
    else
      await expect(page.getByRole("dialog")).toContainText(
        "Доставка настроена на другом устройстве",
      );
    expect(writes).toBe(0);
  });
}

test("failed native unsubscribe with another active device never resurrects the disabled device on dialog remount", async ({
  page,
}) => {
  await notificationFixture(page);
  await page.addInitScript(() => {
    Object.defineProperty(Notification, "permission", {
      configurable: true,
      value: "granted",
    });
    const native = {
      toJSON: () => ({
        endpoint: "https://fcm.googleapis.com/fcm/send/synthetic-retained",
        keys: { p256dh: "synthetic", auth: "synthetic" },
      }),
      unsubscribe: async () => false,
    };
    const registration = {
      pushManager: { getSubscription: async () => native },
    };
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        getRegistration: async () => registration,
        register: async () => registration,
        ready: Promise.resolve(registration),
      },
    });
  });
  let posts = 0;
  let patches = 0;
  let enabled = false;
  await page.route("**/api/v1/public-config", (route) =>
    route.fulfill({
      json: {
        instance: syntheticInstance,
        web_push_public_key:
          "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
      },
    }),
  );
  await page.route("**/api/v1/notifications/preferences", (route) => {
    if (route.request().method() === "PATCH") {
      patches++;
      return route.fulfill({ json: { browser_enabled: true } });
    }
    return route.fulfill({
      json: {
        browser_enabled: true,
        push_subscriptions: [
          { id: "synthetic-other", created_at: null },
          ...(enabled ? [{ id: "synthetic-current", created_at: null }] : []),
        ],
      },
    });
  });
  await page.route("**/api/v1/notifications/push", (route) => {
    posts++;
    enabled = true;
    return route.fulfill({
      json: { id: "synthetic-current", browser_enabled: true },
    });
  });
  await page.route(
    "**/api/v1/notifications/push/synthetic-current",
    (route) => {
      enabled = false;
      return route.fulfill({ json: { status: "disabled" } });
    },
  );
  await page.goto("/#home");
  await page.getByRole("button", { name: /Уведомления.*1/ }).click();
  await page
    .getByRole("button", { name: "Включить на этом устройстве" })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "Уведомления включены на этом устройстве",
  );
  await page
    .getByRole("button", { name: "Отключить на этом устройстве" })
    .click();
  await expect(page.getByRole("dialog")).toContainText("Доставка отключена");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /Уведомления.*1/ }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Доставка настроена на другом устройстве",
  );
  await expect(page.getByRole("dialog")).not.toContainText(
    "Уведомления включены на этом устройстве",
  );
  expect(posts).toBe(1);
  expect(patches).toBe(1);
  expect(
    await page.evaluate(() =>
      localStorage.getItem("sunrise.push.device.v1.9007199254740993123"),
    ),
  ).toBeNull();
});

test("cancelled swipe preserves the toast and inbox during reduced motion", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const send = await notificationFixture(page);
  await page.goto("/#home");
  await expect(
    page.getByRole("button", { name: /Уведомления.*1/ }),
  ).toBeVisible();
  send();
  await page.evaluate(() =>
    (window as unknown as { emitNotification: () => void }).emitNotification(),
  );
  const toast = page.locator(".notification-toast-stack").getByRole("status");
  await expect(toast).toBeVisible();
  await toast.dispatchEvent("pointerdown", {
    pointerId: 1,
    clientY: 100,
    button: 0,
  });
  await toast.dispatchEvent("pointermove", { pointerId: 1, clientY: 110 });
  await toast.dispatchEvent("pointermove", { pointerId: 1, clientY: 210 });
  await toast.dispatchEvent("pointercancel", { pointerId: 1 });
  await expect(toast).toBeVisible();
  await toast.focus();
  await page.keyboard.press("Escape");
  await expect(toast).toHaveCount(0);
});

test("notification inbox and safe-area toast remain accessible without horizontal overflow on compact and desktop screens", async ({
  page,
}) => {
  const send = await notificationFixture(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#home");
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("button", { name: /Уведомления.*1/ }).click();
    await expect(page.getByRole("dialog")).toContainText("Новый вход");
    expect(
      (await new AxeBuilder({ page }).include(".notification-inbox").analyze())
        .violations,
    ).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `.artifacts/ui/v11/inbox-${width}.png`,
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("button", { name: /Уведомления.*1/ }),
    ).toBeFocused();
  }
  await page.setViewportSize({ width: 320, height: 900 });
  await page.evaluate(() => {
    document.documentElement.style.setProperty(
      "--telegram-device-safe-bottom",
      "20px",
    );
    document.documentElement.style.setProperty(
      "--telegram-content-safe-bottom",
      "30px",
    );
  });
  send();
  await page.evaluate(() =>
    (window as unknown as { emitNotification: () => void }).emitNotification(),
  );
  const toast = page.locator(".notification-toast-stack").getByRole("status");
  await expect(toast).toBeVisible();
  const box = (await toast.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(320);
  expect(box.y + box.height).toBeLessThanOrEqual(830);
  expect(
    (await toast.locator(".swipe-toast__body").boundingBox())!.width,
  ).toBeGreaterThanOrEqual(150);
  await page.screenshot({
    path: ".artifacts/ui/v11/toast-320.png",
    fullPage: true,
  });
});
