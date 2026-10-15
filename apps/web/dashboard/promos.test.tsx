import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Editor } from "./admin-editor";
import { Customer } from "./customer";
import type { Catalog, Dashboard, Session } from "@api/types";
import { AccountPromo } from "./account-promo";
import { promoPayload } from "./promo-editor";

describe("promo reward editor", () => {
  it("normalizes a fixed RUB reward as a decimal string for the API", () => {
    expect(
      promoPayload({
        code: "save",
        enabled: true,
        reward_type: "fixed_rub",
        amount_rub: "50,2",
        percent: "15",
        reward_days: 7,
        product_id: "old",
        per_account_limit: 99,
      }).amount_rub,
    ).toBe("50.20");
  });
  it("exposes separate reward types and an accessible global unlimited control", () => {
    const html = renderToStaticMarkup(
      <Editor
        section="discounts"
        row={{
          id: "discounts",
          value: {
            promos: [
              {
                code: "GIFT",
                percent: "0",
                enabled: true,
                reward_type: "free_days",
                reward_days: 7,
                product_id: "tariff-id",
                total_limit: null,
                per_account_limit: 1,
              },
            ],
          },
        }}
        onClose={() => {}}
        onSave={() => {}}
      />,
    );
    expect(html).toContain('value="free_days"');
    expect(html).toContain(
      'aria-label="Без ограничения общего числа активаций"',
    );
    expect(html).toContain("Подарочных дней");
    expect(html).toContain("Тариф новой подписки");
  });
});

const session = {
  account: {
    id: "9007199254740993123",
    email: "test@example.test",
    email_verified: true,
    telegram_id: null,
  },
  is_admin: false,
  mfa_verified: false,
  csrf_token: "synthetic",
} as Session;
const dashboard = {
  account: session.account,
  subscriptions: [],
  payments: [],
  bonus_balance: "0.00",
  bonus_reserved: "0.00",
  credit_balance: "0.00",
  credit_reserved: "0.00",
  credit_available: "0.00",
  credit_currency: "RUB",
  credit_provenance: "synthetic",
  referrer_id: null,
  recurring_enrollment_enabled: false,
};
function resource<T>(data: T) {
  return {
    data,
    error: null,
    loading: false,
    reload: () => {},
    setData: () => {},
  };
}
describe("account promo", () => {
  it("explains unavailable pending rewards without offering replacement activation", () => {
    const html = renderToStaticMarkup(
      <AccountPromo
        initial={{
          code: "ENDED",
          state: "pending",
          reward_type: "free_days",
          percent: "0",
          reward_days: 7,
          product_id: "test",
          available: false,
          unavailable_reason: "Срок действия промокода закончился.",
        }}
        subscriptions={[]}
        catalog={null}
        onComplete={() => {}}
        onLegalStale={() => {}}
      />,
    );
    expect(html).toContain("Срок действия промокода закончился.");
    expect(html).not.toContain("Активировать промокод</button>");
  });
  it("offers activation from the account profile", () => {
    const html = renderToStaticMarkup(
      <Customer
        page="account"
        session={session}
        refreshSession={() => {}}
        dashboard={resource(dashboard as Dashboard)}
        catalog={resource({
          products: [],
          legal: { configured: false, documents: [], revision: null },
        } as unknown as Catalog)}
        config={null}
      />,
    );
    expect(html).toContain("Активировать промокод");
  });
  it("shows the pending reward and prevents entering another code", () => {
    const html = renderToStaticMarkup(
      <Customer
        page="account"
        session={session}
        refreshSession={() => {}}
        dashboard={resource({
          ...dashboard,
          active_promo: {
            code: "SAVED",
            state: "pending",
            reward_type: "percent",
            percent: "15",
            amount_rub: null,
            reward_days: null,
            product_id: null,
            available: true,
          },
        } as Dashboard)}
        catalog={resource({
          products: [],
          legal: { configured: false, documents: [], revision: null },
        } as unknown as Catalog)}
        config={null}
      />,
    );
    expect(html).toContain("Активный промокод");
    expect(html).toContain("SAVED");
    expect(html).not.toContain("Активировать промокод</button>");
  });
});
