import { expect, test, type Page, type Locator } from "@playwright/test";
import { ownerStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

async function setup(page: Page, section: string) {
  const writes: string[] = [];
  await page.addInitScript((section) => {
    history.replaceState(null, "", `/admin#${section}`);
  }, section);
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET") {
      writes.push(path);
      return route.fulfill({
        status: 403,
        json: {
          error: { code: "synthetic_read_only", message: "Только просмотр" },
        },
      });
    }
    let json: unknown = { items: [], total: 0 };
    if (path === "/auth/me")
      json = {
        account: {
          id: "synthetic-owner",
          email: "owner@example.test",
          email_verified: true,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        ...ownerStaffFixture,
        capabilities: [
          ...ownerStaffFixture.capabilities,
          "device_unlinks_manage",
        ],
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/overview")
      json = {
        currency: "RUB",
        timezone: "Europe/Moscow",
        service_count: 125,
        service_value: { RUB: "1234567.89" },
        credit_spent: { RUB: "987654.32" },
        currencies: { RUB: "2345678.90" },
        refunds: { RUB: "19.00" },
        confirmed_fees: { RUB: "99.00" },
        active_paid_subscriptions: 125,
        sales_composition: [],
        service_rows: [],
        settlement_series: [],
        recurring_cycles: [],
        provisioning: [],
        recurring_rows: [],
        series: [],
        rows: [],
      };
    if (path === "/admin/users")
      json = {
        items: [
          {
            id: "synthetic-user",
            email: "user@example.test",
            telegram_id: "9007199254740993123",
            email_verified: true,
            created_at: "2026-10-14T00:00:00Z",
          },
        ],
        total: 1,
      };
    if (path === "/admin/subscriptions")
      json = {
        items: [
          {
            id: "synthetic-subscription",
            account_id: "synthetic-user",
            name: "Длинное название услуги для проверки горизонтальной таблицы подписок",
            status: "active",
            desired_panel_status: "ACTIVE",
            provisioning: { state: "done" },
            expires_at: "2026-11-14T00:00:00Z",
            devices_limit: 3,
          },
        ],
        total: 1,
      };
    if (path === "/admin/promos")
      json = {
        items: [
          {
            code: "SYNTHETIC21",
            enabled: true,
            percent: "10",
            reward_type: "percent",
            total_limit: null,
            audience: "all",
          },
        ],
      };
    if (path === "/admin/users/synthetic-user")
      json = {
        account: {
          id: "synthetic-user",
          email: "very-long-synthetic-user-for-layout@example.test",
          bonus_balance: "1234567.89",
          cash_balance: "987654.32",
        },
        identities: [{ provider: "telegram", subject: "9007199254740993123" }],
        subscriptions: [
          {
            id: "synthetic-subscription",
            name: "Длинное название выданной услуги для проверки переноса текста",
            status: "active",
            rights: {
              entitled_until: "2026-11-14T00:00:00Z",
              source: "paid_order",
            },
          },
        ],
        orders: [],
        payments: [],
        agreements: [],
        ledger: [],
        devices: [],
        sessions: [],
        blocks: [],
        audit: [],
        active_subscriptions_total: 1,
        active_agreements_total: 0,
        can_moderate: true,
        moderation: { current_warnings: 1, unlink_blocked: false },
        referral: {
          balance: "12345.00",
          earned_total: "23456.00",
          invited_total: 12,
        },
        pagination: { totals: { orders: 0, payments: 0, sessions: 0 } },
      };
    await route.fulfill({ json });
  });
  return writes;
}

async function geometry(locator: Locator) {
  return locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return {
      text: element.textContent,
      top: rect.top,
      bottom: rect.bottom,
      left: rect.left,
      right: rect.right,
      display: style.display,
      gap: style.gap,
      margin: style.margin,
      position: style.position,
      transform: style.transform,
      zoom: style.zoom,
      lineHeight: style.lineHeight,
      ancestors: Array.from(
        (function* () {
          let node = element.parentElement;
          while (node) {
            yield `${node.tagName}.${node.className}`;
            node = node.parentElement;
          }
        })(),
      ),
    };
  });
}

for (const width of [390, 1280]) {
  for (const section of ["users", "subscriptions"]) {
    test(`${section} header covers absolute horizontal end and all actions share one row at ${width}px`, async ({
      page,
    }, testInfo) => {
      const writes = await setup(page, section);
      await page.setViewportSize({ width, height: 900 });
      await page.goto("/");
      const table = page.locator(".table-scroll").first();
      await expect(table.locator("thead")).toBeVisible();
      await table.scrollIntoViewIfNeeded();
      const snapshots = [];
      for (const end of [false, true]) {
        await table.evaluate((node, end) => {
          node.scrollLeft = end ? node.scrollWidth : 0;
        }, end);
        const snapshot = await table.evaluate((node) => {
          const head = node.querySelector("thead")!,
            row = node.querySelector("tbody tr")!;
          const cell = row.querySelector("td:last-child")!,
            actions = cell.querySelector(".table-actions")!;
          const box = (element: Element) => {
            const r = element.getBoundingClientRect();
            return {
              left: r.left,
              right: r.right,
              top: r.top,
              bottom: r.bottom,
              width: r.width,
              height: r.height,
            };
          };
          const headStyle = getComputedStyle(head),
            cellStyle = getComputedStyle(cell);
          return {
            scrollLeft: node.scrollLeft,
            scrollWidth: node.scrollWidth,
            clientWidth: node.clientWidth,
            viewport: box(node),
            head: box(head),
            row: box(row),
            cell: box(cell),
            actions: box(actions),
            headerColor: headStyle.backgroundColor,
            bodyColor: cellStyle.backgroundColor,
            buttons: [...actions.querySelectorAll("button")].map((button) => ({
              text: button.textContent,
              ...box(button),
            })),
          };
        });
        await testInfo.attach(`table-${end ? "end" : "start"}`, {
          body: JSON.stringify(snapshot, null, 2),
          contentType: "application/json",
        });
        await page.screenshot({
          path: testInfo.outputPath(
            `${section}-${width}-${end ? "end" : "start"}.png`,
          ),
          fullPage: true,
        });
        snapshots.push({ end, snapshot });
      }
      for (const { end, snapshot } of snapshots) {
        if (end)
          expect(snapshot.scrollLeft).toBeCloseTo(
            snapshot.scrollWidth - snapshot.clientWidth,
            0,
          );
        expect(
          snapshot.head.width + 1,
          JSON.stringify(snapshot),
        ).toBeGreaterThanOrEqual(snapshot.scrollWidth);
        expect(snapshot.headerColor).not.toBe("rgba(0, 0, 0, 0)");
        expect(snapshot.headerColor).not.toBe(snapshot.bodyColor);
        expect(
          snapshot.actions.right,
          JSON.stringify(snapshot),
        ).toBeLessThanOrEqual(snapshot.cell.right + 1);
        if (section === "users") expect(snapshot.buttons.length).toBe(6);
        const centers = snapshot.buttons.map(
          (button) => (button.top + button.bottom) / 2,
        );
        expect(
          Math.max(...centers) - Math.min(...centers),
          JSON.stringify(snapshot),
        ).toBeLessThanOrEqual(2);
        for (let i = 1; i < snapshot.buttons.length; i++)
          expect(
            snapshot.buttons[i].left - snapshot.buttons[i - 1].right,
            JSON.stringify(snapshot),
          ).toBeGreaterThanOrEqual(8);
      }
      expect(writes).toEqual([]);
    });
  }

  test(`promo create and edit align exact once-per-account caption at ${width}px`, async ({
    page,
  }, testInfo) => {
    const writes = await setup(page, "promos");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    for (const name of ["Добавить промокод", "Изменить промокод SYNTHETIC21"]) {
      await page.getByRole("button", { name, exact: true }).click();
      const dialog = page.getByRole("dialog");
      const footer = dialog.locator(".promo-editor-footer");
      const caption = footer.getByText("Один раз на аккаунт", { exact: true });
      await expect(caption).toBeVisible();
      const boxes = await Promise.all([
        geometry(caption),
        geometry(footer.locator(".checkbox-label")),
      ]);
      await testInfo.attach(`promo-${name}`, {
        body: JSON.stringify(boxes, null, 2),
        contentType: "application/json",
      });
      await page.screenshot({
        path: testInfo.outputPath(
          `promo-${name.startsWith("Добавить") ? "create" : "edit"}-${width}.png`,
        ),
        fullPage: true,
      });
      expect(
        Math.abs(
          (boxes[0].top + boxes[0].bottom) / 2 -
            (boxes[1].top + boxes[1].bottom) / 2,
        ),
        JSON.stringify(boxes),
      ).toBeLessThanOrEqual(4);
      expect(
        boxes[0].left - boxes[1].right,
        JSON.stringify(boxes),
      ).toBeGreaterThanOrEqual(8);
      await dialog
        .getByRole("button", { name: "Закрыть", exact: true })
        .click();
    }
    expect(writes).toEqual([]);
  });

  test(`recipient preview heading, audience and input form remain separated at ${width}px`, async ({
    page,
  }, testInfo) => {
    const writes = await setup(page, "broadcasts");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    const heading = page.getByRole("heading", {
      name: "Предпросмотр получателей",
      exact: true,
    });
    await expect(heading).toBeVisible();
    const card = heading.locator("..");
    const boxes = await Promise.all([
      geometry(heading),
      geometry(card.locator(":scope > .notice")),
      geometry(card.locator(":scope > form")),
    ]);
    await testInfo.attach("recipient-preview-geometry", {
      body: JSON.stringify(boxes, null, 2),
      contentType: "application/json",
    });
    await page.screenshot({
      path: testInfo.outputPath(`recipient-preview-${width}.png`),
      fullPage: true,
    });
    expect(
      boxes[1].top - boxes[0].bottom,
      JSON.stringify(boxes),
    ).toBeGreaterThanOrEqual(12);
    expect(
      boxes[2].top - boxes[1].bottom,
      JSON.stringify(boxes),
    ).toBeGreaterThanOrEqual(12);
    expect(writes).toEqual([]);
  });

  test(`analytics headings, metrics and notes have independent vertical space at ${width}px`, async ({
    page,
  }, testInfo) => {
    const writes = await setup(page, "overview");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    const heading = page.getByRole("heading", {
      name: "Выданные услуги и внутренние средства",
      exact: true,
    });
    await expect(heading).toBeVisible();
    const card = heading.locator("..");
    const boxes = await Promise.all([
      geometry(heading),
      geometry(card.locator(".metrics-grid")),
      geometry(card.locator("p.fine-print")),
    ]);
    await testInfo.attach("analytics-geometry", {
      body: JSON.stringify(boxes, null, 2),
      contentType: "application/json",
    });
    await page.screenshot({
      path: testInfo.outputPath(`analytics-${width}.png`),
      fullPage: true,
    });
    expect(
      boxes[1].top - boxes[0].bottom,
      JSON.stringify(boxes),
    ).toBeGreaterThanOrEqual(12);
    expect(
      boxes[2].top - boxes[1].bottom,
      JSON.stringify(boxes),
    ).toBeGreaterThanOrEqual(12);
    expect(
      await card.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    expect(writes).toEqual([]);
  });

  test(`user dialog retains admin ancestry and separates prose from moderation actions at ${width}px`, async ({
    page,
  }, testInfo) => {
    const writes = await setup(page, "users");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await page.locator(".table-actions .button").first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    expect(
      await dialog.evaluate((node) => Boolean(node.closest(".admin-layout"))),
    ).toBe(true);
    const body = dialog
      .locator("details")
      .filter({
        has: page.locator("summary", { hasText: /^Отвязка устройств$/ }),
      })
      .locator(".admin-disclosure-body");
    const boxes = await Promise.all([
      geometry(dialog),
      geometry(body.locator("p.small-copy")),
      geometry(body.locator(".button-row")),
    ]);
    await testInfo.attach("dialog-geometry", {
      body: JSON.stringify(boxes, null, 2),
      contentType: "application/json",
    });
    expect(
      boxes[2].top - boxes[1].bottom,
      JSON.stringify(boxes),
    ).toBeGreaterThanOrEqual(8);
    const actions = body.locator(".button-row > button");
    const buttonBoxes = await actions.evaluateAll((nodes) =>
      nodes.map((node) => {
        const r = node.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      }),
    );
    for (let i = 0; i < buttonBoxes.length; i++)
      for (let j = i + 1; j < buttonBoxes.length; j++) {
        const a = buttonBoxes[i],
          b = buttonBoxes[j];
        expect(
          Math.max(
            Math.max(a.left, b.left) - Math.min(a.right, b.right),
            Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom),
          ),
          JSON.stringify({ a, b }),
        ).toBeGreaterThanOrEqual(8);
      }
    await page.screenshot({
      path: testInfo.outputPath(`dialog-${width}.png`),
      fullPage: true,
    });
    expect(writes).toEqual([]);
  });
}
