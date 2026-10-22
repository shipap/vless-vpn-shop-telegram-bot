import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Button } from "@ui/components";
import {
  Customer,
  RenewalDiscount,
  SubscriptionRetention,
  SubscriptionDeletionNotice,
} from "./customer";
import { Editor } from "./admin-editor";
import { RetentionControls } from "./admin-operations";
import type { Mutation } from "./admin";
import type {
  Catalog,
  Dashboard,
  Quote,
  Session,
  Subscription,
} from "@api/types";

const account = {
  id: "9007199254740993123",
  email: "synthetic@example.test",
  email_verified: true,
  telegram_id: null,
};
const session = {
  account,
  csrf_token: "synthetic",
  is_admin: false,
  mfa_verified: false,
} as Session;
const subscription = (values: Record<string, unknown> = {}) =>
  ({
    id: "synthetic-sub",
    name: "История подписки",
    product_id: "synthetic-product",
    status: "active",
    expires_at: "2026-10-11T00:00:00Z",
    connection_url: "https://example.test/stale-link",
    devices: [],
    devices_limit: 4,
    segments: [],
    recurring: null,
    retention_state: "none",
    cleanup_at: null,
    archived_at: null,
    manual_renewal_discount_eligible: false,
    manual_renewal_discount_percent: null,
    ...values,
  }) as unknown as Subscription;
function resource<T>(data: T) {
  return {
    data,
    loading: false,
    error: null,
    reload: () => {},
    setData: () => {},
  };
}
const catalog = {
  products: [],
  periods: [],
  content: {},
  legal: { configured: false, documents: [], revision: null },
} as unknown as Catalog;
const quote = (values: Partial<Quote> = {}): Quote => ({
  id: "synthetic-quote",
  action: "renew",
  product_id: "synthetic-product",
  subscription_id: "synthetic-sub",
  base_amount: "100.00",
  discount_amount: "10.00",
  period_discount_amount: "0.00",
  secondary_discount_amount: "10.00",
  promo_discount_amount: "0.00",
  payment_method_discount_amount: "0.00",
  minimum_external_required: false,
  bonus_amount: "0.00",
  credit_amount: "0.00",
  total: "90.00",
  currency: "RUB",
  resulting_expires_at: "2026-11-11T00:00:00Z",
  devices_limit: 4,
  expires_at: "2026-10-11T12:00:00Z",
  version: 1,
  breakdown: [],
  ...values,
});
function customer(page: string, values: Record<string, unknown>) {
  return renderToStaticMarkup(
    <Customer
      page={page}
      session={session}
      refreshSession={() => {}}
      dashboard={resource({
        account,
        subscriptions: [subscription(values)],
        payments: [],
        bonus_balance: "0.00",
        credit_balance: "0.00",
        credit_available: "0.00",
        credit_reserved: "0.00",
        bonus_reserved: "0.00",
        recurring_enrollment_enabled: false,
      } as unknown as Dashboard)}
      catalog={resource(catalog)}
      config={null}
    />,
  );
}

describe("server-derived subscription retention", () => {
  for (const panelLimit of [null, 0]) {
    it(`shows an explicitly unknown current device limit without deriving unlimited from panel metadata ${panelLimit}`, () => {
      const values = { devices_limit: null, panel_device_limit: panelLimit };
      for (const page of ["home", "devices"]) {
        const html = customer(page, values);
        expect(html).toContain("Лимит не задан в панели");
        expect(html).not.toContain("Без ограничения");
        expect(html).not.toContain("Включено мест: 4");
      }
    });
  }
  it("shows the seven-day warning only with a server deadline", () => {
    const html = renderToStaticMarkup(
      <SubscriptionRetention
        subscription={subscription({ cleanup_at: "2026-10-18T00:00:00Z" })}
      />,
    );
    expect(html).toContain(
      "Удаление VPN-подписки из панели запланировано через 7 дней после окончания срока при отсутствии препятствий",
    );
    expect(html).toContain("18 октября 2026");
    expect(
      renderToStaticMarkup(
        <SubscriptionRetention subscription={subscription()} />,
      ),
    ).not.toContain("Удаление VPN-подписки из панели запланировано");
  });
  for (const state of [
    "pending",
    "in_flight",
    "unknown",
    "review",
    "archived",
  ]) {
    it(`holds stale target actions in ${state}`, () => {
      const html = customer("home", { retention_state: state });
      expect(html).not.toContain("connect-button");
      expect(html).not.toContain("quick-actions");
      expect(html).not.toContain("stale-link");
      expect(html).toContain("История подписки");
    });
    it(`holds device and rotation actions in ${state}`, () => {
      const html = customer("devices", { retention_state: state });
      expect(html).not.toContain("Отвязать");
      expect(html).not.toContain("Перевыпустить ссылку");
      expect(html).not.toContain("stale-link");
      expect(html).toContain("история платежей");
    });
    it(`keeps an existing agreement cancellable with enrollment off in ${state}`, () => {
      const html = customer("home", {
        retention_state: state,
        recurring: {
          id: "synthetic-agreement",
          state: "active",
          amount: "100.00",
          currency: "RUB",
          next_charge_at: "2026-11-01T00:00:00Z",
        },
      });
      expect(html).toContain("Отменить автопродление");
      expect(html).not.toContain("Перейти к условиям автопродления");
      expect(html).not.toContain("quick-actions");
    });
  }
  it("does not claim an uncertain deletion completed", () => {
    const html = customer("home", { retention_state: "unknown" });
    expect(html).toContain("требует проверки");
    expect(html).not.toContain("VPN-подписка удалена из панели");
  });
  it("keeps archived history and offers a genuinely new purchase", () => {
    const html = customer("home", {
      retention_state: "archived",
      archived_at: "2026-10-18T00:00:00Z",
    });
    expect(html).toContain("VPN-подписка удалена из панели");
    expect(html).toContain("Можно оформить новую подписку");
    expect(html).toContain("Добавить подписку");
  });
  it("keeps normal current target actions when cleanup is inactive", () => {
    expect(customer("home", {})).toContain("connect-button");
    expect(customer("home", {})).toContain("quick-actions");
  });
});

describe("selected renewal discount", () => {
  it("shows only the actually selected near-expiry percent", () => {
    const html = renderToStaticMarkup(
      <RenewalDiscount
        quote={quote({
          selected_secondary: {
            source: "near_expiry_renewal",
            percent: "10",
          },
        })}
      />,
    );
    expect(html).toContain("Скидка на продление: 10%");
  });
  it("identifies another selected discount without promising the near-expiry candidate", () => {
    const html = renderToStaticMarkup(
      <RenewalDiscount
        quote={quote({
          selected_secondary: { source: "renewal_campaign", percent: "20" },
          discount_candidates: [
            { source: "near_expiry_renewal", percent: "10" },
          ],
        })}
      />,
    );
    expect(html).toContain("Кампания продления");
    expect(html).toContain("20%");
    expect(html).not.toContain("Скидка на продление: 10%");
  });
  it("does not invent a percent from candidate eligibility", () => {
    expect(
      renderToStaticMarkup(
        <RenewalDiscount
          quote={quote({
            selected_secondary: null,
            discount_candidates: [
              { source: "near_expiry_renewal", percent: "10" },
            ],
          })}
        />,
      ),
    ).toBe("");
  });
});

describe("owner retention control", () => {
  it("keeps ordinary staff read-only", () => {
    const html = renderToStaticMarkup(
      <RetentionControls enabled={false} isOwner={false} mutate={() => {}} />,
    );
    expect(html).toContain("7 дней");
    expect(html).toContain("Выключено");
    expect(html).not.toContain("<button");
  });
  for (const enabled of [true, false]) {
    it(`uses the existing fresh-MFA mutation flow when enabled=${enabled}`, () => {
      let mutation: Mutation | undefined;
      const control = RetentionControls({
        enabled,
        isOwner: true,
        mutate: (value) => {
          mutation = value;
        },
      });
      // The owner-only button delegates reason/confirmation to MutationDialog.
      const button = control.props.children
        .at(-1)
        .props.children.find(
          (child: { type?: unknown }) => child?.type === Button,
        );
      button.props.onClick();
      expect(mutation).toMatchObject({
        freshMfa: true,
        method: "PATCH",
        path: "/admin/subscription-retention",
        body: { enabled: !enabled, days_after_expiry: 7 },
      });
    });
  }
});

it("keeps the old global campaign and a separate near-expiry owner setting", () => {
  const html = renderToStaticMarkup(
    <Editor
      section="discounts"
      row={{
        id: "discounts",
        value: {
          near_expiry_renewal: { enabled: true, percent: "10" },
          renewal_campaign: { enabled: true, percent: "20" },
        },
      }}
      onClose={() => {}}
      onSave={() => {}}
    />,
  );
  expect(html).toContain("Кампания продления");
  expect(html).toContain("Продление перед окончанием срока");
  expect(html).toContain('value="10"');
  expect(html).toContain('value="20"');
});

describe("deletion notice reasons", () => {
  for (const reason of [
    "expiry_cleanup",
    "manual_panel",
    "verified_panel_absence",
  ] as const) {
    it(`uses only the supplied deletion cause ${reason}`, () => {
      const html = renderToStaticMarkup(
        <SubscriptionDeletionNotice
          notice={{
            subscription_id: "synthetic-sub",
            name: "Подписка",
            reason,
            deleted_at: "2026-10-22T00:00:00Z",
          }}
          onDismiss={() => {}}
        />,
      );
      expect(
        html.includes(
          "Ваша подписка удалена, поскольку после окончания её срока действия прошло 7 дней.",
        ),
      ).toBe(reason === "expiry_cleanup");
      expect(html.includes("Подписка была удалена. Если это ошибка")).toBe(
        reason !== "expiry_cleanup",
      );
    });
  }
});
