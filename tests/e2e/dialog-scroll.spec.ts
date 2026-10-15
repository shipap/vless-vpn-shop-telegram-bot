import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

const stylesheet = await readFile(
  new URL("../../packages/web-ui/styles.css", import.meta.url),
  "utf8",
);

for (const width of [390, 1440]) {
  test(`modal contains scrolling and preserves the page position at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.setContent(`<!doctype html>
      <style>${stylesheet}
        main {height:4000px} .long-content {height:2000px}
      </style>
      <main>Синтетическая страница</main>
      <dialog class="dialog" id="first"><div class="long-content">Содержимое окна</div></dialog>
      <dialog class="dialog" id="second"><div>Вложенное окно</div></dialog>
    `);
    await page.evaluate(() => window.scrollTo(0, 600));
    const before = await page.evaluate(() => window.scrollY);
    const settledPosition = () =>
      page.evaluate(async () => {
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => resolve()),
        );
        return window.scrollY;
      });
    await page.evaluate(() =>
      (document.querySelector("#first") as HTMLDialogElement).showModal(),
    );
    await page.mouse.move(3, 3);
    await page.mouse.wheel(0, 500);
    await expect.poll(settledPosition).toBe(before);
    const dialog = page.locator("#first");
    await dialog.hover();
    await page.mouse.wheel(0, 400);
    await expect
      .poll(() => dialog.evaluate((node) => node.scrollTop))
      .toBeGreaterThan(0);
    await page.evaluate(() =>
      (document.querySelector("#second") as HTMLDialogElement).showModal(),
    );
    await page.evaluate(() =>
      (document.querySelector("#second") as HTMLDialogElement).close(),
    );
    await page.mouse.move(3, 3);
    await page.mouse.wheel(0, 500);
    await expect.poll(settledPosition).toBe(before);
    await page.evaluate(() =>
      (document.querySelector("#first") as HTMLDialogElement).close(),
    );
    await expect.poll(settledPosition).toBe(before);
    await page.mouse.move(width / 2, 200);
    await page.mouse.wheel(0, 400);
    await expect.poll(settledPosition).toBeGreaterThan(before);
  });
}
