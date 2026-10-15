import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Customer } from "../../apps/web/dashboard/customer";
import type { Dashboard, Session } from "@api/types";

const dashboardData = {
  account: { id: "synthetic-account" },
  subscriptions: [],
  payments: [],
  bonus_balance: "0.00",
  credit_balance: "0.00",
  credit_available: "0.00",
  credit_reserved: "0.00",
  credit_currency: "RUB",
  referral: {
    referral_url: "https://t.me/example_bot?start=ref_1",
    website_referral_url: "http://127.0.0.1:3100/?ref=ref_1",
  },
} as unknown as Dashboard;

function renderBonuses(hostname: string, websiteReferralUrl: string) {
  const originalWindow = globalThis.window;
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { location: { hostname } },
  });
  try {
    return renderToStaticMarkup(
      <Customer
        page="bonuses"
        session={{ account: { id: "synthetic-account" } } as Session}
        dashboard={
          {
            data: {
              ...dashboardData,
              referral: {
                ...dashboardData.referral,
                website_referral_url: websiteReferralUrl,
              },
            },
            loading: false,
            error: null,
            reload: () => undefined,
            setData: () => undefined,
          } as never
        }
        catalog={
          {
            data: null,
            loading: false,
            error: null,
            reload: () => undefined,
            setData: () => undefined,
          } as never
        }
        config={null}
        refreshSession={() => undefined}
      />,
    );
  } finally {
    if (originalWindow === undefined)
      delete (globalThis as { window?: Window }).window;
    else
      Object.defineProperty(globalThis, "window", {
        configurable: true,
        value: originalWindow,
      });
  }
}

describe("customer referral links", () => {
  it("hides a backend loopback website referral on a remote dashboard while keeping Telegram", () => {
    const html = renderBonuses(
      "dashboard.example.test",
      "http://127.0.0.1:3100/?ref=ref_1",
    );

    expect(html).not.toContain("http://127.0.0.1:3100/");
    expect(html).toContain('href="https://t.me/example_bot?start=ref_1"');
    expect(html.match(/<button\b/g)).toHaveLength(1);
  });

  it("keeps the website referral available in a local browser", () => {
    const html = renderBonuses("localhost", "http://127.0.0.1:3100/?ref=ref_1");

    expect(html).toContain('href="http://127.0.0.1:3100/?ref=ref_1"');
    expect(html.match(/<button\b/g)).toHaveLength(2);
  });

  it("keeps valid HTTPS website referrals available on a remote dashboard", () => {
    const html = renderBonuses(
      "dashboard.example.test",
      "https://landing.example.test/?ref=ref_1",
    );

    expect(html).toContain('href="https://landing.example.test/?ref=ref_1"');
    expect(html.match(/<button\b/g)).toHaveLength(2);
  });

  it("rejects website referral URLs containing credentials", () => {
    const html = renderBonuses(
      "dashboard.example.test",
      "https://name:password@landing.example.test/?ref=ref_1",
    );

    expect(html).not.toContain("name:password@");
    expect(html.match(/<button\b/g)).toHaveLength(1);
  });
});
