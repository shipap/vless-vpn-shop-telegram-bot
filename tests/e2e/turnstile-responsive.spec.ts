import { expect, test } from "@playwright/test";
import { syntheticInstance } from "./instance-fixture";

test("container resize recreates Turnstile safely and preserves expiry, retry and request reset", async ({
  page,
}) => {
  await page.route("**/*", (route) => {
    const url = new URL(route.request().url());
    return ["127.0.0.1", "localhost"].includes(url.hostname)
      ? route.continue()
      : route.fulfill({ body: "", contentType: "application/javascript" });
  });
  const writes: unknown[] = [];
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me"))
      return route.fulfill({
        status: 401,
        json: { error: { code: "auth_required" } },
      });
    if (path.endsWith("/public-config"))
      return route.fulfill({
        json: {
          instance: syntheticInstance,
          local_test: true,
          turnstile_test_bypass: false,
          turnstile_site_key: "synthetic-site-key",
        },
      });
    if (path.endsWith("/auth/email/request")) {
      writes.push(route.request().postDataJSON());
      return route.fulfill({
        json: { challenge_id: "synthetic", resend_after: 120 },
      });
    }
    return route.fulfill({
      json: {
        products: [],
        periods: [],
        content: {},
        configured: true,
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
      },
    });
  });
  await page.addInitScript(() => {
    const probe = {
      renders: [] as { id: string; options: Record<string, any> }[],
      removes: [] as string[],
      resets: [] as string[],
    };
    (window as any).turnstileProbe = probe;
    window.turnstile = {
      render(element, options) {
        const id = `synthetic-widget-${probe.renders.length}`;
        const frame = document.createElement("iframe");
        frame.dataset.widgetId = id;
        frame.title = "Синтетическая проверка безопасности";
        frame.width = options.size === "compact" ? "150" : "300";
        frame.height = options.size === "compact" ? "140" : "65";
        frame.style.border = "0";
        element.append(frame);
        probe.renders.push({ id, options });
        return id;
      },
      remove(id) {
        probe.removes.push(id);
        document.querySelector(`[data-widget-id="${id}"]`)?.remove();
      },
      reset(id) {
        probe.resets.push(id);
      },
    };
  });
  await page.setViewportSize({ width: 320, height: 850 });
  await page.goto("/");
  const frame = page.locator(".turnstile-widget iframe");
  await expect(frame).toHaveAttribute("width", "150");
  await page
    .getByLabel("Email", { exact: true })
    .fill("synthetic@example.test");
  await page.getByRole("checkbox").check();
  const request = page.getByRole("button", {
    name: "Получить код",
    exact: true,
  });
  await expect(request).toBeDisabled();
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders
      .at(-1)
      .options.callback("synthetic-before-resize"),
  );
  await expect(request).toBeEnabled();
  await page.setViewportSize({ width: 360, height: 850 });
  await expect(frame).toHaveAttribute("width", "300");
  await expect(frame).toHaveCount(1);
  await expect(request).toBeDisabled();
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders[0].options.callback("stale-token"),
  );
  await expect(request).toBeDisabled();
  const renders = await page.evaluate(
    () => (window as any).turnstileProbe.renders.length,
  );
  await page.setViewportSize({ width: 390, height: 850 });
  await expect
    .poll(() =>
      page.evaluate(() => (window as any).turnstileProbe.renders.length),
    )
    .toBe(renders);
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders
      .at(-1)
      .options.callback("synthetic-current"),
  );
  await expect(request).toBeEnabled();
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders.at(-1).options["expired-callback"](),
  );
  await expect(request).toBeDisabled();
  await expect
    .poll(() =>
      page.evaluate(() => (window as any).turnstileProbe.resets.length),
    )
    .toBe(1);
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders.at(-1).options["error-callback"](),
  );
  await expect(page.getByRole("status")).toContainText("Повторите попытку");
  await page
    .getByRole("button", { name: "Повторить проверку", exact: true })
    .click();
  await expect(frame).toHaveCount(1);
  await expect(request).toBeDisabled();
  await page.setViewportSize({ width: 320, height: 850 });
  await expect(frame).toHaveAttribute("width", "150");
  await page.evaluate(() =>
    (window as any).turnstileProbe.renders
      .at(-1)
      .options.callback("synthetic-final"),
  );
  await request.click();
  await expect(page.getByLabel("Код из письма", { exact: true })).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => (window as any).turnstileProbe.resets.length),
    )
    .toBe(2);
  expect(writes).toEqual([
    { email: "synthetic@example.test", turnstile_token: "synthetic-final" },
  ]);
  await expect(page.locator(".login-resend > .button")).toBeDisabled();
  await page.goto("about:blank");
});
