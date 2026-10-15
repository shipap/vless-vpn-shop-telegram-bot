import { expect, test } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import AxeBuilder from "@axe-core/playwright";
import { ownerStaffFixture } from "./staff-fixture";

async function adminPage(
  page: import("@playwright/test").Page,
  enrolled = false,
) {
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}};",
    }),
  );
  await page.route("**/admin", async (route) =>
    route.fulfill({
      response: await route.fetch({ url: "http://127.0.0.1:3200/" }),
    }),
  );
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        json: {
          account: {
            id: "7",
            email: "owner@example.test",
            email_verified: true,
          },
          csrf_token: "synthetic",
          is_admin: true,
          ...ownerStaffFixture,
          mfa_verified: false,
        },
      });
    if (path.endsWith("/admin/mfa/status"))
      return route.fulfill({
        json: {
          enrolled,
          enrollment_required: !enrolled,
          trusted: false,
          trust_expires_at: null,
        },
      });
    if (path.endsWith("/admin/mfa/enroll"))
      return route.fulfill({
        json: {
          secret: "SYNTHETICKEY",
          provisioning_uri:
            "otpauth://totp/sunrise:test?secret=JBSWY3DPEHPK3PXP&issuer=sunrise",
          expires_at: new Date(Date.now() + 600000).toISOString(),
        },
      });
    if (
      path.endsWith("/admin/mfa/enroll/confirm") ||
      path.endsWith("/admin/mfa/verify")
    )
      return route.fulfill({
        status: 400,
        json: {
          error: {
            code: "mfa_invalid",
            message: "Код дополнительной защиты неверен.",
          },
        },
      });
    return route.fulfill({
      json: {
        configured: false,
        documents: [],
        local_test: true,
        turnstile_test_bypass: true,
      },
    });
  });
  await page.goto("/admin");
}

test("first admin entry enrolls authenticator locally before enabling access", async ({
  page,
}) => {
  const requests: string[] = [];
  page.on("request", (r) => requests.push(r.url()));
  await adminPage(page);
  await page.getByRole("button", { name: "Настроить аутентификатор" }).click();
  await expect(
    page.getByRole("img", { name: "QR для приложения-аутентификатора" }),
  ).toBeVisible();
  await expect(page.getByText("SYNTHETICKEY", { exact: true })).toBeVisible();
  await page.getByLabel("Код аутентификатора", { exact: true }).fill("123456");
  await page.getByRole("button", { name: "Включить защиту" }).click();
  await expect(
    page.getByText("Код дополнительной защиты неверен.", { exact: true }),
  ).toBeVisible();
  expect(
    requests.filter((url) => /chart.googleapis|api.qrserver/.test(url)),
  ).toEqual([]);
});

test("enrolled administrator sees verification without enrollment secret", async ({
  page,
}) => {
  await adminPage(page, true);
  await expect(
    page.getByLabel("Код аутентификатора", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Настроить аутентификатор" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("На email этот код не отправляется", { exact: false }),
  ).toBeVisible();
});

test("MFA setup controls and pending QR form stay separated and inside the card at every viewport", async ({
  page,
}) => {
  await mkdir(".artifacts/ui/v10-mfa-layout", { recursive: true });
  const violations: unknown[] = [];
  for (const width of [320, 360, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await adminPage(page);
    await expect(
      page.getByRole("button", {
        name: "Настроить аутентификатор",
        exact: true,
      }),
    ).toBeVisible();
    for (const state of [
      "initial",
      "pending",
      "error",
      "fresh-email",
    ] as const) {
      if (state === "pending") {
        await page
          .getByRole("button", {
            name: "Настроить аутентификатор",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("img", { name: "QR для приложения-аутентификатора" }),
        ).toBeVisible();
      }
      if (state === "error") {
        await page
          .getByLabel("Код аутентификатора", { exact: true })
          .fill("123456");
        await page
          .getByRole("button", { name: "Включить защиту", exact: true })
          .click();
        await expect(
          page.getByText("Код дополнительной защиты неверен.", { exact: true }),
        ).toBeVisible();
      }
      if (state === "fresh-email") {
        await adminPage(page);
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
        await page
          .getByRole("button", {
            name: "Настроить аутентификатор",
            exact: true,
          })
          .click();
        await expect(
          page.getByRole("button", {
            name: "Подтвердить почту повторно",
            exact: true,
          }),
        ).toBeVisible();
      }
      await page.evaluate(() => document.fonts.ready);
      const layout = await page.locator(".mfa-page .card").evaluate((card) => {
        const cardBounds = card.getBoundingClientRect();
        const controls = [
          ...card.querySelectorAll<HTMLElement>(
            "button, a.text-link, .code-slots, .mfa-error",
          ),
        ].map((element) => ({
          label: element.textContent ?? element.getAttribute("aria-label"),
          bounds: element.getBoundingClientRect().toJSON(),
        }));
        const collisions = [];
        for (let first = 0; first < controls.length; first++) {
          for (let second = first + 1; second < controls.length; second++) {
            const a = controls[first].bounds,
              b = controls[second].bounds;
            const horizontalGap = Math.max(b.left - a.right, a.left - b.right);
            const verticalGap = Math.max(b.top - a.bottom, a.top - b.bottom);
            if (horizontalGap < 12 && verticalGap < 12)
              collisions.push([
                controls[first].label,
                controls[second].label,
                { horizontalGap, verticalGap },
              ]);
          }
        }
        return {
          collisions,
          outsideCard: controls.filter(
            ({ bounds }) =>
              bounds.left < cardBounds.left || bounds.right > cardBounds.right,
          ),
          pageOverflow: document.documentElement.scrollWidth > innerWidth,
          controls,
        };
      });
      if (
        layout.collisions.length ||
        layout.outsideCard.length ||
        layout.pageOverflow
      )
        violations.push({ width, state, ...layout });
      if (state !== "error") {
        const accessibility = await new AxeBuilder({ page }).analyze();
        expect(
          accessibility.violations.filter((item) =>
            ["serious", "critical"].includes(item.impact ?? ""),
          ),
        ).toEqual([]);
      }
      await page.screenshot({
        path: `.artifacts/ui/v10-mfa-layout/${state}-${width}.png`,
        fullPage: true,
      });
      if (state === "fresh-email")
        await page.unroute("**/api/v1/admin/mfa/enroll");
    }
  }
  expect(violations).toEqual([]);
});
