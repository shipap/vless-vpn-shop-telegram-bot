import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PaymentHistory } from "../../apps/web/dashboard/payment-history";

it("renders immutable paid product, action, period and device metadata rather than current catalog conditions", () => {
  const html = renderToStaticMarkup(
    <PaymentHistory
      payments={[
        {
          id: "synthetic-payment",
          order_id: "synthetic-order",
          amount: "105.50",
          currency: "RUB",
          status: "paid",
          paid_at: "2026-10-07T21:10:00Z",
          fee: null,
          environment: "local_test",
          product_name: "Исторический тариф",
          action: "renew",
          payment_method: "external",
          period_days: 90,
          devices: 4,
          provisioning_status: "pending",
        },
      ]}
    />,
  );
  expect(html).toContain("Исторический тариф");
  expect(html).toContain("Продление");
  expect(html).toContain("90 дней");
  expect(html).toContain("4 устройства");
});
