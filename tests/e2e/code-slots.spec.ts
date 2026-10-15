import { syntheticInstance } from "./instance-fixture";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdir } from "node:fs/promises";

const documents = [
  { document_type: "offer", version: "v1", url: "https://example.test/offer" },
  {
    document_type: "privacy",
    version: "v1",
    url: "https://example.test/privacy",
  },
];

async function codeScreen(page: Page, smtp = false, address?: string) {
  const verified: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        status: 401,
        json: { error: { code: "unauthorized" } },
      });
    if (path.endsWith("/public-config"))
      return route.fulfill({
        json: {
          instance: syntheticInstance,
          local_test: true,
          turnstile_test_bypass: true,
          dashboard_url: "/",
          smtp_email_enabled: smtp,
        },
      });
    if (path.endsWith("/auth/email/request"))
      return route.fulfill({
        json: { challenge_id: "synthetic-challenge", resend_after: 120 },
      });
    if (path.endsWith("/auth/email/verify")) {
      verified.push(route.request().postDataJSON());
      return route.fulfill({
        status: 400,
        json: {
          error: {
            code: "invalid_code",
            message: "Код неверный. Попробуйте ещё раз.",
          },
        },
      });
    }
    if (path.endsWith("/legal"))
      return route.fulfill({ json: { configured: true, documents } });
    return route.fulfill({ json: { products: [], periods: [], content: {} } });
  });
  await page.goto("/");
  await page
    .getByLabel("Email", { exact: true })
    .fill(
      address ?? (smtp ? "test-recipient@example.com" : "code-ui@example.test"),
    );
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Получить код" }).click();
  await expect(page.getByLabel("Код из письма", { exact: true })).toBeVisible();
  return verified;
}

test("настроенная SMTP-почта не показывается как отключённая", async ({
  page,
}) => {
  await codeScreen(page, true);
  await expect(
    page.getByText(
      "Коды отправляются на разрешённые тестовые адреса электронной почты.",
      { exact: false },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("внешняя отправка отключена", { exact: false }),
  ).toHaveCount(0);
});

for (const width of [320, 390, 768, 1440]) {
  test(`email code row is centered in its form at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 960 });
    await codeScreen(page);
    const geometry = await page.locator(".code-slots__row").evaluate((row) => {
      const slots = row.getBoundingClientRect();
      const form = row.closest("form")!.getBoundingClientRect();
      return {
        offset: slots.x + slots.width / 2 - (form.x + form.width / 2),
        rowLeft: slots.left,
        rowRight: slots.right,
        formLeft: form.left,
        formRight: form.right,
      };
    });
    await test.info().attach("code-row-geometry", {
      body: JSON.stringify(geometry),
      contentType: "application/json",
    });
    await mkdir(".artifacts/ui/code-slots", { recursive: true });
    await page.screenshot({
      path: `.artifacts/ui/code-slots/login-centered-${width}.png`,
    });
    expect(geometry.rowLeft).toBeGreaterThanOrEqual(geometry.formLeft);
    expect(geometry.rowRight).toBeLessThanOrEqual(geometry.formRight);
    expect(Math.abs(geometry.offset)).toBeLessThanOrEqual(1);
  });
}

test("шесть слотов: клавиатура, выбор текста и явная проверка без дубликатов", async ({
  page,
}) => {
  const verified = await codeScreen(page);
  await expect(page.locator(".code-slots__slot")).toHaveCount(6);
  const input = page.getByLabel("Код из письма", { exact: true });
  await input.fill("123456");
  await expect(input).toHaveValue("123456");
  await expect(page.locator(".code-slots__slot[data-filled]")).toHaveCount(6);
  expect(verified).toHaveLength(0);
  await input.press("ControlOrMeta+A");
  await input.press("7");
  await expect(input).toHaveValue("7");
  await input.press("Backspace");
  await expect(input).toHaveValue("");
  await input.fill("654321");
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  await expect.poll(() => verified.length).toBe(1);
  expect(verified[0]).toEqual({
    challenge_id: "synthetic-challenge",
    code: "654321",
    legal_accepted: true,
    documents,
  });
  await expect(
    page.getByText("Код неверный. Попробуйте ещё раз."),
  ).toBeVisible();
  await expect(input).toHaveValue("");
  await input.fill("123456");
  await expect(input).toHaveValue("123456");
  expect(verified).toHaveLength(1);
});

test("email verification requires checked legal consent and shows Spam advice", async ({
  page,
}) => {
  const verified = await codeScreen(page);
  await expect(
    page.getByText("Проверьте папку «Спам»", { exact: false }),
  ).toBeVisible();
  await page.getByLabel("Код из письма", { exact: true }).fill("123456");
  await page.getByRole("checkbox").uncheck();
  await expect(
    page.getByRole("button", { name: "Войти в кабинет" }),
  ).toBeDisabled();
  expect(verified).toHaveLength(0);
});

test("вставка и автозаполнение сохраняют шесть цифр; неполный код не отправляется", async ({
  page,
}) => {
  const verified = await codeScreen(page);
  await expect(page.locator(".code-slots__slot")).toHaveCount(6);
  const input = page.getByLabel("Код из письма", { exact: true });
  await input.fill("12");
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  expect(verified).toHaveLength(0);
  await input.fill("");
  await input.evaluate((element) => {
    const clipboardData = new DataTransfer();
    clipboardData.setData("text/plain", "12 34-56");
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData,
      }),
    );
  });
  await expect(input).toHaveValue("123456");
  expect(verified).toHaveLength(0);
  await input.fill("987654");
  await expect(page.locator(".code-slots__slot[data-filled]")).toHaveCount(6);
  await expect(input).toHaveAttribute("autocomplete", "one-time-code");
  await expect(input).toHaveAttribute("inputmode", "numeric");
});

test("слоты помещаются на мобильном экране и доступны с уменьшением движения", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 760 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await codeScreen(page);
  await expect(page.locator(".code-slots__slot")).toHaveCount(6);
  await page.getByLabel("Код из письма", { exact: true }).fill("123456");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const results = await new AxeBuilder({ page }).analyze();
  expect(
    results.violations.filter((v) =>
      ["serious", "critical"].includes(v.impact ?? ""),
    ),
  ).toEqual([]);
  await mkdir(".artifacts/ui/code-slots", { recursive: true });
  await page.screenshot({ path: ".artifacts/ui/code-slots/login-mobile.png" });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({ path: ".artifacts/ui/code-slots/login-desktop.png" });
});

test("очистка неверного кода не забирает фокус после перехода клавишей Tab", async ({
  page,
}) => {
  await codeScreen(page);
  const input = page.getByLabel("Код из письма", { exact: true });
  await input.fill("123456");
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  await expect(
    page.getByText("Код неверный. Попробуйте ещё раз."),
  ).toBeVisible();
  await page.keyboard.press("Tab");
  const next = page.getByRole("button", { name: "Другой email" });
  await expect(next).toBeFocused();
  await expect(input).toHaveValue("");
  await expect(next).toBeFocused();
});

test("synthetic uppercase email uses local delivery notice", async ({
  page,
}) => {
  await codeScreen(page, true, "code-ui@example.TEST");
  await expect(
    page.getByText("внешняя отправка отключена", { exact: false }),
  ).toBeVisible();
});

test("updated legal documents require fresh consent without losing entered code", async ({
  page,
}) => {
  await codeScreen(page);
  await page.route("**/api/v1/auth/email/verify", (route) =>
    route.fulfill({
      status: 409,
      json: {
        error: {
          code: "legal_versions_stale",
          message: "Документы обновились. Прочитайте текущие версии.",
        },
      },
    }),
  );
  await page.getByLabel("Код из письма", { exact: true }).fill("123456");
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  await expect(page.getByRole("checkbox")).not.toBeChecked();
  await expect(page.getByLabel("Код из письма", { exact: true })).toHaveValue(
    "123456",
  );
  await expect(
    page.getByRole("button", { name: "Войти в кабинет" }),
  ).toBeDisabled();
});
