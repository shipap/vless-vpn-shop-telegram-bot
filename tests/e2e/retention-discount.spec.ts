import { expect, test, type Page } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";
import { ownerStaffFixture } from "./staff-fixture";

async function fixture(
  page: Page,
  options: {
    state?: string;
    selected?: string;
    owner?: boolean;
    agreement?: boolean;
    profile?: "private" | "neutral";
  } = {},
) {
  let state = options.state ?? "none";
  let enabled = true;
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const account = {
    id: "9007199254740993123",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  const legal = {
    configured: true,
    accepted: true,
    revision: "synthetic",
    missing: [],
    history: [],
    legacy_evidence: {},
    documents: [
      {
        document_type: "offer",
        version: "1",
        url: "https://example.test/offer",
      },
      {
        document_type: "privacy",
        version: "1",
        url: "https://example.test/privacy",
      },
    ],
  };
  const policy = {
    cap: "50",
    bonus_cap: "50",
    referral_percent: "5",
    promos: [],
    loyalty_period_days: 30,
    loyalty_milestones: [],
    renewal_campaign: { enabled: true, percent: "20" },
    near_expiry_renewal: { enabled: true, percent: "10" },
  };
  const product = {
    id: "synthetic-product",
    name: "Синтетический тариф",
    available: true,
    tier_level: 1,
    currency: "RUB",
    included_devices: 4,
    periods: [{ days: 30, price: "100.00" }],
    presentation: {},
  };
  if (options.owner !== undefined)
    await page.addInitScript(() =>
      history.replaceState(null, "", "/admin#operations"),
    );
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic",
        is_admin: options.owner !== undefined,
        mfa_verified: true,
        ...(options.owner !== undefined
          ? {
              ...ownerStaffFixture,
              staff_role: options.owner ? "owner" : "admin",
            }
          : { capabilities: [] }),
      };
    if (path === "/public-config")
      json = {
        instance: {
          ...syntheticInstance,
          visual_profile: options.profile ?? "private",
        },
        local_test: true,
      };
    if (path === "/catalog")
      json = {
        products: [product],
        periods: product.periods,
        content: {},
        legal,
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [
          {
            id: "synthetic-sub",
            name: "История подписки",
            product_id: product.id,
            status: "active",
            expires_at: "2026-10-11T00:00:00Z",
            connection_url: "https://example.test/stale-connection",
            devices: [],
            devices_limit: 4,
            segments: [],
            recurring: options.agreement
              ? {
                  id: "synthetic-agreement",
                  state: "active",
                  amount: "100.00",
                  currency: "RUB",
                  next_charge_at: "2026-11-01T00:00:00Z",
                }
              : null,
            retention_state: state,
            cleanup_at: "2026-10-18T00:00:00Z",
            archived_at: state === "archived" ? "2026-10-18T00:00:00Z" : null,
            manual_renewal_discount_eligible: true,
            manual_renewal_discount_percent: "10",
          },
        ],
        payments: [
          {
            id: "synthetic-payment",
            amount: "100.00",
            currency: "RUB",
            status: "paid",
            paid_at: "2026-10-01T00:00:00Z",
          },
        ],
        bonus_balance: "0",
        credit_balance: "0",
        credit_available: "0",
        recurring_enrollment_enabled: false,
      };
    if (path.startsWith("/legal")) json = legal;
    if (path === "/promos/active" && request.method() === "GET") json = null;
    if (path === "/quotes")
      json = {
        id: "synthetic-quote",
        action: "renew",
        product_id: product.id,
        subscription_id: "synthetic-sub",
        base_amount: "100.00",
        total: options.selected === "renewal_campaign" ? "80.00" : "90.00",
        discount_amount:
          options.selected === "renewal_campaign" ? "20.00" : "10.00",
        bonus_amount: "0",
        credit_amount: "0",
        currency: "RUB",
        devices_limit: 4,
        version: 1,
        expires_at: "2027-01-01T00:00:00Z",
        resulting_expires_at: "2027-02-01T00:00:00Z",
        breakdown: [],
        selected_secondary: {
          source: options.selected ?? "near_expiry_renewal",
          percent: options.selected === "renewal_campaign" ? "20" : "10",
        },
        discount_candidates: [{ source: "near_expiry_renewal", percent: "10" }],
      };
    if (path === "/admin/operations/health")
      json = { subscription_retention: { enabled, days_after_expiry: 7 } };
    if (path === "/admin/operations") json = { items: [], total: 0 };
    if (path === "/admin/discounts")
      json = { items: [{ id: "discounts", value: policy }], total: 1 };
    if (path === "/subscriptions/synthetic-sub/recurring/status")
      json = {
        loyalty: {
          percent: "0",
          next_milestone_at: null,
          applied_to_external_amount: false,
        },
        dynamic_amount_supported: false,
      };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/notifications/stream")
      return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (request.method() !== "GET") {
      const body = request.postDataJSON();
      writes.push({ path, body });
      expect([
        "/legal/accept",
        "/quotes",
        "/admin/mfa/verify",
        "/admin/subscription-retention",
        "/admin/discounts",
        "/subscriptions/synthetic-sub/recurring/cancel",
      ]).toContain(path);
      if (path === "/legal/accept") {
        expect(request.method()).toBe("POST");
        expect(body).toEqual({
          documents: legal.documents,
          context: "checkout",
        });
      }
      if (path === "/admin/subscription-retention") {
        enabled = body.enabled;
        json = { enabled, days_after_expiry: 7 };
      }
    }
    await route.fulfill({ json });
  });
  return {
    writes,
    legal,
    hold: () => {
      state = "unknown";
    },
  };
}

test("uses the server cleanup deadline and selected near-expiry renewal discount", async ({
  page,
}) => {
  const { writes, legal } = await fixture(page);
  await page.goto("/");
  await expect(
    page.getByText("Плановая дата удаления из панели:", { exact: false }),
  ).toContainText("18 октября 2026");
  await page
    .getByRole("button", { name: "Продлить подписку", exact: false })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Принимаю оферту", exact: false })
    .check();
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await expect(dialog).toContainText("Скидка на продление: 10%");
  await expect(dialog.locator(".quote-total strong").first()).toHaveText(
    /90\s*₽/,
  );
  expect(writes).toEqual([
    {
      path: "/legal/accept",
      body: { documents: legal.documents, context: "checkout" },
    },
    {
      path: "/quotes",
      body: {
        product_id: "synthetic-product",
        period_days: 30,
        action: "renew",
        subscription_id: "synthetic-sub",
        bonus_amount: "0.00",
        credit_amount: "0.00",
        extra_devices: 0,
      },
    },
  ]);
});

test("a stronger selected campaign replaces the near-expiry candidate", async ({
  page,
}) => {
  const { writes, legal } = await fixture(page, {
    selected: "renewal_campaign",
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Продлить подписку", exact: false })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Принимаю оферту", exact: false })
    .check();
  await dialog
    .getByRole("button", { name: "Рассчитать стоимость", exact: true })
    .click();
  await expect(dialog).toContainText("Кампания продления: 20%");
  await expect(dialog.locator(".quote-total strong").first()).toHaveText(
    /80\s*₽/,
  );
  await expect(
    dialog.getByText("Скидка на продление: 10%", { exact: true }),
  ).toHaveCount(0);
  expect(writes).toEqual([
    {
      path: "/legal/accept",
      body: { documents: legal.documents, context: "checkout" },
    },
    {
      path: "/quotes",
      body: {
        product_id: "synthetic-product",
        period_days: 30,
        action: "renew",
        subscription_id: "synthetic-sub",
        bonus_amount: "0.00",
        credit_amount: "0.00",
        extra_devices: 0,
      },
    },
  ]);
});

for (const state of ["pending", "in_flight", "unknown", "review", "archived"]) {
  test(`${state} preserves history and blocks stale target actions`, async ({
    page,
  }) => {
    const { writes } = await fixture(page, { state });
    await page.goto("/");
    await expect(page.locator(".subscription-card")).toContainText(
      "История подписки",
    );
    await expect(page.locator(".connect-button, .quick-actions")).toHaveCount(
      0,
    );
    await page.goto("/#devices");
    await expect(
      page.getByRole("button", { name: "Перевыпустить ссылку", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Отвязать", exact: true }),
    ).toHaveCount(0);
    await expect(page.locator("a[href*='stale-connection']")).toHaveCount(0);
    await page.goto("/#payments");
    await expect(page.locator("main")).toContainText("Оплачен");
    expect(writes).toEqual([]);
    if (state === "archived") {
      await page.goto("/");
      await expect(page.locator("main")).toContainText(
        "VPN-подписка удалена из панели",
      );
      await page
        .getByRole("button", { name: "Добавить подписку", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Новая подписка", exact: true }),
      ).toBeVisible();
    }
  });
}

for (const action of ["connection", "renewal"]) {
  test(`fresh retention state fences an already-open ${action} dialog`, async ({
    page,
  }) => {
    const scenario = await fixture(page);
    await page.goto("/");
    await page
      .getByRole("button", {
        name: action === "connection" ? "Подключить VPN" : "Продлить подписку",
        exact: action === "connection",
      })
      .click();
    await expect(page.getByRole("dialog")).toBeVisible();
    scenario.hold();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.getByRole("dialog")).toContainText("требует проверки");
    await expect(
      page
        .getByRole("dialog")
        .getByRole("button", { name: "Рассчитать стоимость", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("dialog").locator("a[href*='stale-connection']"),
    ).toHaveCount(0);
    expect(scenario.writes).toEqual([]);
  });
}

test("owner retention change uses automatic audit, confirmation and fresh MFA", async ({
  page,
}) => {
  const { writes } = await fixture(page, { owner: true });
  await page.goto("/");
  await page.locator("summary", { hasText: "Состояние операций" }).click();
  await page
    .locator("summary", { hasText: "Удаление истёкших VPN-подписок" })
    .click();
  await page
    .getByRole("button", {
      name: "Выключить удаление истёкших подписок",
      exact: true,
    })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить изменение", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(writes.map((write) => write.path)).toEqual([
    "/admin/mfa/verify",
    "/admin/subscription-retention",
  ]);
  expect(writes[1].body).toEqual({
    enabled: false,
    days_after_expiry: 7,
    confirmed: true,
  });
});

test("ordinary staff with operations capability sees no owner policy mutation", async ({
  page,
}) => {
  const { writes } = await fixture(page, { owner: false });
  await page.goto("/");
  await page.locator("summary", { hasText: "Состояние операций" }).click();
  await expect(
    page.locator("summary", { hasText: "Удаление истёкших VPN-подписок" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Выключить удаление истёкших подписок",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("near-expiry setting edits preserve the existing global campaign", async ({
  page,
}) => {
  const { writes } = await fixture(page, { owner: true });
  await page.goto("/");
  await page
    .getByRole("link", { name: "Скидки и рефералы", exact: true })
    .click();
  await page.getByRole("button", { name: "Изменить", exact: true }).click();
  const editor = page.getByRole("dialog");
  const near = editor.locator("fieldset").filter({
    has: page.locator("legend", {
      hasText: "Продление перед окончанием срока",
    }),
  });
  await near.getByLabel("Скидка кампании, %").fill("15");
  await editor
    .getByRole("button", { name: "Сохранить изменения", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("textbox", { name: /Причина/ })).toHaveCount(0);
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog.getByRole("checkbox").check();
  await dialog
    .getByRole("button", { name: "Подтвердить и сохранить", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  const saved = writes.find((write) => write.path === "/admin/discounts")?.body;
  expect(saved).toMatchObject({
    near_expiry_renewal: { enabled: true, percent: "15" },
    renewal_campaign: { enabled: true, percent: "20" },
    confirmed: true,
  });
});

test("a held subscription still allows explicit cancellation of an existing mandate with enrollment off", async ({
  page,
}) => {
  const { writes } = await fixture(page, { state: "review", agreement: true });
  await page.goto("/");
  await expect(page.locator(".quick-actions")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Отменить автопродление", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Отменить автопродление?",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(writes.map((write) => write.path)).toEqual([
    "/subscriptions/synthetic-sub/recurring/cancel",
  ]);
});

for (const profile of ["private", "neutral"] as const) {
  for (const width of [1366, 390]) {
    test(`archived history and new purchase remain usable in ${profile} at ${width}px`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await fixture(page, { state: "archived", profile });
      await page.goto("/");
      await expect(page.locator("html")).toHaveAttribute(
        "data-visual-profile",
        profile,
      );
      await expect(page.locator(".subscription-card")).toContainText(
        "история платежей сохранены",
      );
      await expect(page.locator(".connect-button, .quick-actions")).toHaveCount(
        0,
      );
      await page
        .getByRole("button", { name: "Добавить подписку", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Новая подписка", exact: true }),
      ).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
    });
  }
}
