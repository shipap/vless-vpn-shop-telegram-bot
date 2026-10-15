import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AdminValue } from "./admin-values";
it("renders nested audit evidence as labelled readable data without raw JSON", () => {
  const html = renderToStaticMarkup(
    <AdminValue
      value={{
        scope: "context",
        rule: "wrong_code",
        before: { email_verified: false },
        nodes: [{ type: "text", text: "<script>unsafe</script>" }],
      }}
    />,
  );
  expect(html).toContain("Контекст входа");
  expect(html).toContain("Неверный код");
  expect(html).toContain("Email подтверждён");
  expect(html).not.toContain("<pre>");
  expect(html).not.toContain("<script>");
});
it("explains a full refund that lacks attributable usage without claiming automatic revocation", () => {
  const html = renderToStaticMarkup(
    <AdminValue
      value={{
        entitlement_action: "manual_review",
        reason: "attributable_usage_unavailable",
        full_refund: true,
        usage_source: null,
        dispute: { enabled: false },
      }}
    />,
  );
  expect(html).toContain("Ручная проверка");
  expect(html).toContain("Нет достоверного использования конкретной покупки");
  expect(html).toContain("Источник проверки использования");
  expect(html).not.toContain("Отзыв возвращённой покупки");
  expect(html).not.toContain("manual_review");
});
it("keeps refund review amounts in their actual currency and leaves unknown remaining value uncalculated", () => {
  const html = renderToStaticMarkup(
    <AdminValue
      value={{
        external_refundable_amount: "9007199254740993.99",
        external_currency: "EUR",
        provider_fee: "0.03",
        fee_currency: "USDT",
        segments: [
          {
            remaining_service_value: null,
            currency: "EUR",
            unavailable_reason: "paid_basis_missing_or_changed",
          },
        ],
        automatic_compensation: false,
      }}
    />,
  );
  expect(html).toContain("9 007 199 254 740 993,99 EUR");
  expect(html).toContain("0,03 USDT");
  expect(html).toContain("требуется ручная сверка");
  expect(html).toContain("Нет данных");
  expect(html).not.toContain("₽");
});
