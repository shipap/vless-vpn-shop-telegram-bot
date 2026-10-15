import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const backend = "http://127.0.0.1:8150";
test.use({ baseURL: "http://127.0.0.1:3210" });
test.beforeEach(async ({ request }) => {
  const runId = process.env.SUNRISE_E2E_AUTH_RUN_ID;
  expect(runId).toMatch(/^[a-f0-9-]{36}$/);
  const result = await request.post(`${backend}/__e2e__/ui/setup?mode=auth`, {
    headers: { "X-E2E-Run-ID": runId! },
  });
  expect(result.status()).toBe(200);
  expect(await result.json()).toMatchObject({
    run_id: runId,
    mode: "auth",
    accounts: 0,
  });
});

test("CodeSlots выполняет регистрацию через настоящий изолированный backend", async ({
  page,
}) => {
  const identity = await (
    await page.request.get(`${backend}/__e2e__/status`)
  ).json();
  expect(identity).toMatchObject({
    environment: "isolated-e2e-auth",
    run_id: process.env.SUNRISE_E2E_AUTH_RUN_ID,
    redis_mode: "emulated",
    accounts: 0,
    sessions: 0,
  });
  const config = await (
    await page.request.get(`${backend}/api/v1/public-config`)
  ).json();
  expect(config).toMatchObject({
    local_test: true,
    turnstile_test_bypass: true,
    telegram_miniapp_enabled: false,
    telegram_login_enabled: false,
  });
  await page.route("https://telegram.org/**", (route) => route.abort());
  await page.route("https://oauth.telegram.org/**", (route) => route.abort());
  await page.goto("/");
  await page.getByLabel("Email", { exact: true }).fill("customer@sunrise.test");
  await expect(
    page.getByRole("button", { name: "Получить код" }),
  ).toBeDisabled();
  await page.getByRole("checkbox", { name: /Принимаю оферту/ }).check();
  const requested = page.waitForResponse(
    (r) =>
      r.url().endsWith("/auth/email/request") &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Получить код" }).click();
  const response = await requested;
  expect(response.status()).toBe(200);
  const { challenge_id } = await response.json();
  if (!/^[a-f0-9-]{36}$/i.test(challenge_id))
    throw new Error("Invalid isolated challenge identifier");
  const mail = JSON.parse(
    await readFile(
      `.private/e2e-ui-fixtures/${process.env.SUNRISE_E2E_AUTH_RUN_ID}/mail/${challenge_id}.json`,
      "utf8",
    ),
  );
  if (mail.to !== "customer@sunrise.test" || !/^\d{6}$/.test(mail.code))
    throw new Error("Invalid isolated mail fixture");
  const cooldown = await page.request.post(
    `${backend}/api/v1/auth/email/request`,
    { data: { email: mail.to, turnstile_token: "local-test" } },
  );
  expect(cooldown.status()).toBe(429);
  expect((await cooldown.json()).error.code).toBe("resend_cooldown");
  const missingConsent = await page.request.post(
    `${backend}/api/v1/auth/email/verify`,
    { data: { challenge_id, code: mail.code } },
  );
  expect(missingConsent.status()).toBe(409);
  expect((await missingConsent.json()).error.code).toBe(
    "legal_consent_required",
  );
  const unchanged = await (
    await page.request.get(`${backend}/__e2e__/status`)
  ).json();
  expect(unchanged).toMatchObject({
    accounts: 0,
    sessions: 0,
    legal_consents: 0,
  });
  const input = page.getByLabel("Код из письма", { exact: true });
  await expect(page.locator(".code-slots__slot")).toHaveCount(6);
  await input.fill(mail.code === "000000" ? "111111" : "000000");
  const rejected = page.waitForResponse(
    (r) =>
      r.url().endsWith("/auth/email/verify") && r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  const invalid = await rejected;
  expect(invalid.status()).toBe(400);
  expect((await invalid.json()).error.code).toBe("code_invalid");
  await expect(
    page.getByText("Код неверен или истёк.", { exact: true }),
  ).toBeVisible();
  await expect(input).toBeEditable();
  await input.fill(mail.code);
  await page.getByRole("button", { name: "Войти в кабинет" }).click();
  await expect(
    page.getByRole("heading", { name: "Мой VPN", exact: true }),
  ).toBeVisible();
  const session = await page.request.get(`${backend}/api/v1/auth/me`);
  expect(session.status()).toBe(200);
  expect((await session.json()).account.email_verified).toBe(true);
  const registered = await (
    await page.request.get(`${backend}/__e2e__/status`)
  ).json();
  expect(registered).toMatchObject({
    accounts: 1,
    sessions: 1,
    legal_consents: 2,
  });
  const foreignReset = await page.request.post(
    `${backend}/__e2e__/ui/setup?mode=auth`,
    { headers: { "X-E2E-Run-ID": "00000000-0000-0000-0000-000000000000" } },
  );
  expect(foreignReset.status()).toBe(404);
  expect(
    (await (await page.request.get(`${backend}/__e2e__/status`)).json())
      .accounts,
  ).toBe(1);
  const noCsrf = await page.request.post(
    `${backend}/api/v1/account/link/email/request`,
    { data: {} },
  );
  expect(noCsrf.status()).toBe(403);
  expect((await noCsrf.json()).error.code).toBe("csrf_invalid");
});
