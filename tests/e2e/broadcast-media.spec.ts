import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { ownerStaffFixture } from "./staff-fixture";
import { syntheticInstance } from "./instance-fixture";

// Только синтетические изображения Pillow; metadata удалена в ожидаемом серверном Ref.
const png = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAHnRFWHRmaXh0dXJlAHN5bnRoZXRpYyBvd25lciB1cGxvYWTQm1+zAAAAE0lEQVR4nGMUCJjAAANMcBY6BwAizAD28TNRPgAAAABJRU5ErkJggg==",
  "base64",
);
const jpeg = Buffer.from(
  "/9j/4AAQSkZJRgABAQAAAQABAAD/4QA6RXhpZgAATU0AKgAAAAgAAQEOAAIAAAAXAAAAGgAAAABzeW50aGV0aWMgb3duZXIgdXBsb2FkAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDg6KKK+mPnj//Z",
  "base64",
);
const video = readFileSync(
  new URL("../fixtures/broadcast-chromium-synthetic.mp4", import.meta.url),
);
const digest = (bytes: Buffer) =>
  createHash("sha256").update(bytes).digest("hex");
const mediaRef = (
  mime: string,
  size_bytes: number,
  sha256: string,
  asset = "a",
) => ({
  asset_id: asset.repeat(32),
  sha256,
  mime,
  size_bytes,
  proof: "c".repeat(64),
});
const pngRef = mediaRef(
  "image/png",
  76,
  "abd821b5ab3391f0df42791aa5f1cd68eb77589b328f36d5624d9568f7c31c2d",
);
const jpegRef = mediaRef(
  "image/jpeg",
  287,
  "3df09ae2b38889ff62b565acdad94b71ee45adaa5748cedddc2662b01658f180",
  "b",
);
type Write = { path: string; body: Record<string, unknown> };
type Upload = { bytes: Buffer; mime: string; csrf: string | undefined };

async function setup(
  page: Page,
  respond: (route: Route, index: number) => Promise<void>,
) {
  const writes: Write[] = [],
    uploads: Upload[] = [];
  await page.addInitScript(() =>
    history.replaceState(null, "", "/admin#broadcasts"),
  );
  await page.route("https://telegram.org/**", (route) =>
    route.fulfill({ body: "", contentType: "application/javascript" }),
  );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (path === "/admin/broadcasts/media" && request.method() === "POST") {
      uploads.push({
        bytes: request.postDataBuffer()!,
        mime: request.headers()["content-type"]!,
        csrf: request.headers()["x-csrf-token"],
      });
      return respond(route, uploads.length - 1);
    }
    let json: unknown = {};
    if (path === "/auth/me")
      json = {
        account: {
          id: "9007199254740993123",
          email: "owner@example.test",
          email_verified: true,
          telegram_id: null,
        },
        csrf_token: "synthetic",
        is_admin: true,
        mfa_verified: true,
        ...ownerStaffFixture,
      };
    if (path === "/public-config")
      json = { instance: syntheticInstance, local_test: true };
    if (path === "/catalog") json = { products: [], periods: [], content: {} };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/admin/broadcasts") json = { items: [], total: 0 };
    if (request.method() !== "GET") {
      writes.push({ path, body: request.postDataJSON() });
      json = path.endsWith("/estimate")
        ? {
            eligible_total: 3,
            recipient_count: 3,
            denied_count: 0,
            delivery: "durable_notifications",
          }
        : { id: "synthetic-broadcast", state: "pending", recipient_count: 3 };
    }
    await route.fulfill({ json });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", {
      name: "Предпросмотр получателей",
      exact: true,
    }),
  ).toBeVisible();
  return { writes, uploads };
}
async function confirm(
  page: Page,
  writes: Write[],
  path: string,
  label: string,
) {
  const before = writes.length;
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Причина изменения")
    .fill("Синтетическая проверка вложения");
  await dialog.getByRole("checkbox").check();
  // Обычная форма не должна отправлять операцию без свежего кода.
  await dialog.getByRole("button", { name: label, exact: true }).click();
  expect(writes.filter((write) => write.path === path)).toHaveLength(0);
  await dialog
    .getByLabel("Свежий код аутентификатора", { exact: true })
    .fill("123456");
  await dialog.getByRole("button", { name: label, exact: true }).click();
  await expect
    .poll(() => writes.filter((write) => write.path === path).length)
    .toBe(1);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const current = writes.slice(before);
  expect(
    current.filter((write) => write.path === "/admin/mfa/verify"),
  ).toHaveLength(1);
  expect(
    current.findIndex((write) => write.path === "/admin/mfa/verify"),
  ).toBeLessThan(current.findIndex((write) => write.path === path));
}
async function attach(
  scope: Page | Locator,
  name: string,
  mimeType: string,
  buffer: Buffer,
) {
  await scope
    .getByLabel("Фото или видео", { exact: true })
    .setInputFiles({ name, mimeType, buffer });
  await expect(
    scope.getByText(`${name} · Файл загружен`, { exact: true }),
  ).toBeVisible();
}

test("canonical PNG/JPEG references remain separate from text in preview, self-test and creation", async ({
  page,
}) => {
  const { writes, uploads } = await setup(page, async (route, index) =>
    route.fulfill({ json: index === 0 ? pngRef : jpegRef }),
  );
  await page
    .getByLabel("Текст сообщения для предпросмотра")
    .fill("Фото для предпросмотра");
  await attach(page, "photo.png", "image/png", png);
  expect(uploads[0]).toEqual({
    bytes: png,
    mime: "image/png",
    csrf: "synthetic",
  });
  expect(pngRef.size_bytes).not.toBe(png.length);
  await page
    .getByRole("button", { name: "Рассчитать получателей", exact: true })
    .click();
  await expect
    .poll(
      () => writes.filter((write) => write.path.endsWith("/estimate")).length,
    )
    .toBe(1);
  expect(writes[0]!.body).toMatchObject({
    media: pngRef,
    nodes: [
      {
        type: "paragraph",
        children: [{ type: "text", text: "Фото для предпросмотра" }],
      },
    ],
  });
  await page
    .getByRole("button", { name: "Отправить тест себе", exact: true })
    .click();
  await confirm(
    page,
    writes,
    "/admin/broadcasts/test-self",
    "Подтвердить изменение",
  );
  expect(
    writes.find((write) => write.path.endsWith("/test-self"))!.body.media,
  ).toEqual(pngRef);

  await page
    .getByRole("button", { name: "Новая рассылка", exact: true })
    .click();
  const editor = page.getByRole("dialog");
  await editor
    .getByLabel("Текст сообщения", { exact: true })
    .fill("Фото общей аудитории");
  await attach(editor, "photo.jpg", "image/jpeg", jpeg);
  expect(uploads[1]).toEqual({
    bytes: jpeg,
    mime: "image/jpeg",
    csrf: "synthetic",
  });
  expect(jpegRef.size_bytes).not.toBe(jpeg.length);
  await editor
    .getByRole("button", { name: "Создать рассылку", exact: true })
    .click();
  await confirm(page, writes, "/admin/broadcasts", "Подтвердить и создать");
  const body = writes.find((write) => write.path === "/admin/broadcasts")!.body;
  expect(body.media).toEqual(jpegRef);
  expect(body.nodes).toEqual([
    {
      type: "paragraph",
      children: [{ type: "text", text: "Фото общей аудитории" }],
    },
  ]);
  expect(body).not.toHaveProperty("recipients");
  expect(JSON.stringify(body)).not.toContain("photo.jpg");
});

test("the real synthetic MP4 is uploaded as binary and submitted as a signed reference", async ({
  page,
}) => {
  expect(digest(video)).toBe(
    "a4b549ecf373a99d33df297713bf5b3d02968851f6089e25cbf1a3ce64ac07f0",
  );
  const reference = mediaRef("video/mp4", video.length, digest(video));
  const { writes, uploads } = await setup(page, async (route) =>
    route.fulfill({ json: reference }),
  );
  await page
    .getByRole("button", { name: "Новая рассылка", exact: true })
    .click();
  const editor = page.getByRole("dialog");
  await editor
    .getByLabel("Текст сообщения", { exact: true })
    .fill("Видео общей аудитории");
  await attach(editor, "synthetic.mp4", "video/mp4", video);
  expect(uploads).toEqual([
    { bytes: video, mime: "video/mp4", csrf: "synthetic" },
  ]);
  await expect(editor).toContainText("В email файл придёт вложением");
  await editor
    .getByRole("button", { name: "Создать рассылку", exact: true })
    .click();
  await confirm(page, writes, "/admin/broadcasts", "Подтвердить и создать");
  expect(
    writes.find((write) => write.path === "/admin/broadcasts")!.body.media,
  ).toEqual(reference);
});

test("upload failure and a removed in-flight response cannot submit or restore an attachment", async ({
  page,
}) => {
  let release!: () => void;
  let lateHandled = false;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const { writes, uploads } = await setup(page, async (route, index) => {
    if (index === 0) {
      await held;
      // Снятый input прерывает запрос; Playwright может уже закрыть перехваченный route.
      await route.fulfill({ json: pngRef }).catch(() => {});
      lateHandled = true;
    } else
      await route.fulfill({
        status: 409,
        json: {
          error: {
            code: "media_unavailable",
            message: "Файл недоступен. Выберите его снова.",
          },
        },
      });
  });
  await page
    .getByLabel("Текст сообщения для предпросмотра")
    .fill("Текст без неподтверждённого файла");
  const input = page.getByLabel("Фото или видео", { exact: true });
  await input.setInputFiles({
    name: "delayed.png",
    mimeType: "image/png",
    buffer: png,
  });
  await expect.poll(() => uploads.length).toBe(1);
  await expect(
    page.getByRole("button", { name: "Рассчитать получателей", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Отправить тест себе", exact: true }),
  ).toBeDisabled();
  expect(writes).toEqual([]);
  await page.getByRole("button", { name: "Убрать файл", exact: true }).click();
  release();
  await expect.poll(() => lateHandled).toBe(true);
  await expect(
    page.getByRole("button", { name: "Рассчитать получателей", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Рассчитать получателей", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]!.body.media).toBeNull();
  await expect(page.getByText(/delayed\.png|Файл загружен/)).toHaveCount(0);
  await input.setInputFiles({
    name: "rejected.png",
    mimeType: "image/png",
    buffer: png,
  });
  await expect(
    page.getByText("Файл недоступен. Выберите его снова.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Рассчитать получателей", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Отправить тест себе", exact: true }),
  ).toBeDisabled();
  expect(
    writes.filter(
      (write) =>
        write.path.endsWith("/test-self") || write.path === "/admin/broadcasts",
    ),
  ).toEqual([]);
  await page.getByRole("button", { name: "Убрать файл", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Отправить тест себе", exact: true }),
  ).toBeEnabled();
});
