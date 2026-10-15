import { syntheticInstance } from "./instance-fixture";
import { test, expect } from "@playwright/test";
import { mkdir } from "node:fs/promises";

test("login labels, responsive Cloudflare widget and decorative sun remain aligned across viewport widths", async ({
  page,
}) => {
  await mkdir(".artifacts/ui/v10-auth-labels", { recursive: true });
  await page.route("https://telegram.org/js/telegram-web-app.js?64", (route) =>
    route.fulfill({
      contentType: "application/javascript",
      body: "window.Telegram={WebApp:{initData:''}}",
    }),
  );
  await page.route(
    "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    (route) =>
      route.fulfill({
        contentType: "application/javascript",
        body: `window.turnstile={render(el,options){const frame=document.createElement('iframe');frame.title='Проверка безопасности Cloudflare';frame.width=options.size==='compact'?'150':'300';frame.height=options.size==='compact'?'140':'65';frame.style.border='0';frame.srcdoc='<body style="margin:0;background:#222;color:white;display:grid;place-items:center;font:14px sans-serif;height:100vh">Синтетическая проверка Cloudflare</body>';el.append(frame);options.callback('synthetic-token');return 'widget'},reset(){},remove(){document.querySelectorAll('.turnstile-widget iframe').forEach(frame=>frame.remove())}};`,
      }),
  );
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
    if (path.endsWith("/auth/email/request"))
      return route.fulfill({
        json: { challenge_id: "synthetic", resend_after: 120 },
      });
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
  const violations: unknown[] = [];
  for (const width of [320, 360, 390, 768, 1024, 1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.locator(".turnstile-field iframe")).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const initialWidget = await page
      .locator(".login-form .form")
      .evaluate((form) => {
        const formRect = form.getBoundingClientRect();
        const widgetRect = form
          .querySelector(".turnstile-field iframe")!
          .getBoundingClientRect();
        return {
          centerDifference: Math.abs(
            widgetRect.left +
              widgetRect.width / 2 -
              formRect.left -
              formRect.width / 2,
          ),
          left: widgetRect.left,
          right: widgetRect.right,
          width: widgetRect.width,
          formLeft: formRect.left,
          formRight: formRect.right,
          formWidth: formRect.width,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
    if (
      initialWidget.centerDifference > 1 ||
      initialWidget.left < 0 ||
      initialWidget.right > width ||
      initialWidget.overflow
    )
      violations.push({ width, initialWidget });
    expect(initialWidget.width).toBe(initialWidget.formWidth < 300 ? 150 : 300);
    expect(initialWidget.left).toBeGreaterThanOrEqual(initialWidget.formLeft);
    expect(initialWidget.right).toBeLessThanOrEqual(initialWidget.formRight);
    const footer = await page.locator(".login-bottom").boundingBox();
    expect(footer!.x).toBeGreaterThanOrEqual(initialWidget.formLeft);
    expect(footer!.x + footer!.width).toBeLessThanOrEqual(
      initialWidget.formRight,
    );
    if (width < 700) {
      await expect(page.locator(".login-art")).toBeHidden();
    } else {
      const sun = await page.locator(".login-sun").evaluate((element) => {
        const pseudo = getComputedStyle(element, "::before");
        const probe = document.createElement("div");
        for (const property of [
          "position",
          "top",
          "left",
          "width",
          "height",
          "transform",
          "box-sizing",
        ])
          probe.style.setProperty(property, pseudo.getPropertyValue(property));
        element.append(probe);
        const fill = probe.getBoundingClientRect();
        const core = element
          .querySelector("svg circle")!
          .getBoundingClientRect();
        const headline = element
          .parentElement!.querySelector("h1")!
          .getBoundingClientRect();
        const result = {
          horizontalDifference: Math.abs(
            fill.left + fill.width / 2 - core.left - core.width / 2,
          ),
          verticalDifference: Math.abs(
            fill.top + fill.height / 2 - core.top - core.height / 2,
          ),
          diameterDifference: Math.abs(fill.width - core.width),
          headlineGap: fill.top - headline.bottom,
        };
        probe.remove();
        return result;
      });
      if (
        sun.horizontalDifference > 1 ||
        sun.verticalDifference > 1 ||
        sun.diameterDifference > 1 ||
        sun.headlineGap < 10
      )
        violations.push({ width, sun });
    }
    await page.screenshot({
      path: `.artifacts/ui/v10-auth-labels/login-${width}.png`,
      fullPage: true,
    });
    await page
      .getByLabel("Email", { exact: true })
      .fill("synthetic@example.test");
    await page.getByRole("checkbox").check();
    await page
      .getByRole("button", { name: "Получить код", exact: true })
      .click();
    await expect(
      page.getByLabel("Код из письма", { exact: true }),
    ).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const geometry = await page
      .locator(".login-form .form")
      .evaluate((form) => {
        const center = (rect: DOMRect) => rect.left + rect.width / 2;
        const label = form.querySelector("label:has(.code-slots)")!;
        const caption =
          [...label.childNodes].find(
            (node) =>
              node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
          ) ?? label.querySelector(".login-code-label")!;
        const range = document.createRange();
        range.selectNodeContents(caption);
        const formCenter = center(form.getBoundingClientRect());
        const code = form.querySelector(".code-slots")!.getBoundingClientRect();
        const widget = form
          .querySelector(".turnstile-field iframe")!
          .getBoundingClientRect();
        return {
          codeLabelDifference: Math.abs(
            center(range.getBoundingClientRect()) - center(code),
          ),
          widgetDifference: Math.abs(center(widget) - formCenter),
          widgetWidth: widget.width,
          widgetLeft: widget.left,
          widgetRight: widget.right,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
    if (
      geometry.codeLabelDifference > 1 ||
      geometry.widgetDifference > 1 ||
      geometry.widgetLeft < 0 ||
      geometry.widgetRight > width ||
      geometry.overflow
    )
      violations.push({ width, ...geometry });
    expect(geometry.widgetWidth).toBe(
      initialWidget.formWidth < 300 ? 150 : 300,
    );
    await page.screenshot({
      path: `.artifacts/ui/v10-auth-labels/code-${width}.png`,
      fullPage: true,
    });
  }
  expect(violations).toEqual([]);
});
