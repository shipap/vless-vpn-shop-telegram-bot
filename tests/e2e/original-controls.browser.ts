import { test, expect } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.clock.install();
  await page.goto("/tests/e2e/original-controls.html");
});
test("native OTP editing keeps selection, sanitizes paste and never submits on completion", async ({
  page,
}) => {
  const input = page.getByLabel("Код", { exact: true });
  await input.fill("12");
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();
  await expect(page.getByLabel("Отправлено")).toHaveText("0");
  await input.evaluate((element) => {
    (element as HTMLInputElement).select();
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
  await expect(page.getByLabel("Отправлено")).toHaveText("0");
  await expect(page.getByLabel("Заполнено")).toHaveText("1");
  await input.press("ControlOrMeta+A");
  await input.press("9");
  await expect(input).toHaveValue("9");
  await input.press("Backspace");
  await expect(input).toHaveValue("");
  await input.fill("654321");
  await page.getByRole("button", { name: "Подтвердить", exact: true }).click();
  await expect(page.getByLabel("Отправлено")).toHaveText("1");
  await page.getByRole("button", { name: "Сбросить код" }).click();
  await input.fill("654321");
  await expect(page.getByLabel("Заполнено")).toHaveText("3");
  await page.getByLabel("Скрывать код").check();
  await expect(input).toHaveAttribute("type", "password");
  await expect(page.locator(".code-slots__row")).not.toContainText("654321");
  await page.getByLabel("Заблокировать").check();
  await expect(input).toBeDisabled();
});

test("hover preserves remaining time and a completed swipe closes once", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  const toast = page.locator(".swipe-toast__card[role=status]");
  await page.mouse.move(1000, 1000);
  await page.clock.fastForward(100);
  await toast.hover();
  await page.clock.fastForward(3000);
  await expect(toast).toBeVisible();
  await page.mouse.move(1000, 1000);
  await page.clock.fastForward(450);
  await expect(toast).toHaveCount(0);
  await expect(page.getByLabel("Закрытия")).toHaveText("timeout");
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  await toast.dispatchEvent("pointerdown", {
    pointerId: 4,
    clientY: 100,
    button: 0,
  });
  await toast.dispatchEvent("pointerup", { pointerId: 4, clientY: 220 });
  await page.clock.fastForward(1000);
  await expect(page.getByLabel("Закрытия")).toHaveText("timeout,swipe");
});

test("unmount cancels the timeout without a spurious dismissal", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  await expect(page.locator(".swipe-toast__card[role=status]")).toBeVisible();
  await page.getByRole("button", { name: "Убрать компонент" }).click();
  await page.clock.fastForward(1000);
  await expect(page.getByLabel("Закрытия")).toBeEmpty();
});

test("reopening the same component recomputes focus instead of retaining a stale pause", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  const toast = page.locator(".swipe-toast__card[role=status]");
  await toast.focus();
  await page
    .getByRole("button", { name: "Скрыть без размонтирования" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(toast).toHaveCount(0);
  await page
    .getByRole("button", { name: "Открыть без размонтирования" })
    .evaluate((button) => (button as HTMLButtonElement).click());
  await expect(toast).toBeVisible();
  await page.mouse.move(1000, 1000);
  await page.clock.fastForward(700);
  await expect(toast).toHaveCount(0);
  await expect(page.getByLabel("Закрытия")).toHaveText("programmatic,timeout");
});
test("notification bell uses the native popover and exposes current expansion", async ({
  page,
}) => {
  const bell = page.getByRole("button", {
    name: "Уведомления, 2 непрочитанных",
  });
  await bell.click();
  await expect(page.getByText("Входящие", { exact: true })).toBeVisible();
  await expect(bell).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Escape");
  await expect(bell).toHaveAttribute("aria-expanded", "false");
});
test("toast pauses for focused children and hidden documents, retaining remaining time", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  const toast = page.locator(".swipe-toast__card[role=status]");
  await expect(toast).toBeVisible();
  await toast.getByRole("button", { name: "Открыть", exact: true }).focus();
  await page.clock.fastForward(3000);
  await expect(toast).toBeVisible();
  await page.getByRole("button", { name: "Вне уведомления" }).focus();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.fastForward(3000);
  await expect(toast).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.fastForward(700);
  await expect(toast).toHaveCount(0);
  await expect(page.getByLabel("Закрытия")).toHaveText("timeout");
});
test("cancelled capture never dismisses; keyboard and action close only once", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  const toast = page.locator(".swipe-toast__card[role=status]");
  for (const cancel of ["pointercancel", "lostpointercapture"]) {
    await toast.dispatchEvent("pointerdown", {
      pointerId: 2,
      clientY: 100,
      button: 0,
    });
    await toast.dispatchEvent("pointermove", { pointerId: 2, clientY: 250 });
    await toast.dispatchEvent(cancel, { pointerId: 2 });
    await toast.dispatchEvent("pointerup", { pointerId: 2, clientY: 250 });
    await expect(toast).toBeVisible();
  }
  await toast.focus();
  await page.keyboard.press("Escape");
  await page.clock.fastForward(1000);
  await expect(page.getByLabel("Закрытия")).toHaveText("escape");
  await page.getByRole("button", { name: "Показать уведомление" }).click();
  await page
    .locator(".swipe-toast__card[role=status]")
    .getByRole("button", { name: "Открыть", exact: true })
    .click();
  await page.clock.fastForward(1000);
  await expect(page.getByLabel("Действия")).toHaveText("1");
  await expect(page.getByLabel("Закрытия")).toHaveText("escape,action");
});
