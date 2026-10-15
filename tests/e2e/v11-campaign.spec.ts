import { syntheticInstance } from "./instance-fixture";
import { expect, test } from "@playwright/test";

test("landing visit propagates bounded proof and independent referral, attaches only after auth, and cleans query on success", async ({
  page,
}) => {
  const writes: { path: string; body: unknown; csrf: string | undefined }[] =
    [];
  const proof = "a".repeat(64);
  const account = {
    id: "synthetic-account",
    email: "synthetic@example.test",
    email_verified: true,
    telegram_id: null,
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    let json: unknown = {};
    if (path === "/campaigns/visit")
      json = {
        campaign_id: "x".repeat(24),
        visit_token: proof,
        expires_at: new Date(Date.now() + 2592000000).toISOString(),
        max_age_seconds: 2592000,
      };
    if (path === "/public-config")
      json = {
        instance: syntheticInstance,
        dashboard_url: "http://127.0.0.1:3200/",
        local_test: true,
      };
    if (path === "/catalog")
      json = {
        products: [],
        periods: [],
        content: {},
        legal: { configured: false, documents: [] },
      };
    if (path === "/auth/me")
      json = {
        account,
        csrf_token: "synthetic-csrf",
        is_admin: false,
        mfa_verified: false,
        staff_role: null,
        capabilities: [],
      };
    if (path === "/dashboard")
      json = {
        account,
        subscriptions: [],
        payments: [],
        bonus_balance: "0",
        bonus_reserved: "0",
        credit_balance: "0",
        credit_available: "0",
        credit_reserved: "0",
      };
    if (path === "/legal/status")
      json = { accepted: true, configured: false, documents: [], missing: [] };
    if (path === "/notifications") json = { items: [], unread_count: 0 };
    if (path === "/campaigns/attach") json = { status: "attached" };
    if (route.request().method() === "POST")
      writes.push({
        path,
        body: route.request().postDataJSON(),
        csrf: route.request().headers()["x-csrf-token"],
      });
    await route.fulfill({ json });
  });
  await page.goto(`http://127.0.0.1:3100/?utm_id=${"x".repeat(24)}&ref=ref_7`);
  const link = page.getByRole("link", { name: "Личный кабинет", exact: true });
  await expect(link).toHaveAttribute("href", new RegExp(`utm_first=${proof}`));
  expect(writes.map((w) => w.path)).toEqual(["/campaigns/visit"]);
  expect(writes[0].body).toEqual({
    campaign_id: "x".repeat(24),
    visitor_id: expect.stringMatching(/^[A-Za-z0-9_.:-]{16,128}$/),
  });
  const href = new URL((await link.getAttribute("href"))!);
  expect(href.searchParams.get("ref")).toBe("ref_7");
  expect(href.searchParams.get("utm_first_expires")).toMatch(/^[0-9]+$/);
  await page.goto(href.href);
  await expect
    .poll(() => writes.filter((w) => w.path === "/campaigns/attach").length)
    .toBe(1);
  await expect(page).not.toHaveURL(/utm_first=/);
  expect(writes[1]).toEqual({
    path: "/campaigns/attach",
    body: { visit_tokens: [proof] },
    csrf: "synthetic-csrf",
  });
  expect(new URL(page.url()).searchParams.get("ref")).toBe("ref_7");
  expect(JSON.stringify(writes[0].body)).not.toContain(account.email);
});
