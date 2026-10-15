import { expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ServiceFAQ } from "../../apps/web/dashboard/service-faq";
import type { Catalog } from "../api-client/types";
import type { Dashboard } from "../api-client/types";
import { PaymentHistory } from "../../apps/web/dashboard/payment-history";

it("renders a published troubleshooting answer with its existing customer navigation action", () => {
  const catalog = {
    content: {
      help_center: {
        custom_faq: [
          {
            id: "help_payment_access",
            question: "Деньги списались, но доступа нет",
            answer: [
              {
                type: "paragraph",
                children: [
                  {
                    type: "text",
                    text: "Проверьте статус заказа, не платите повторно.",
                  },
                ],
              },
            ],
          },
        ],
      },
    },
    legal: { documents: [] },
  } as unknown as Catalog;
  const html = renderToStaticMarkup(
    createElement(ServiceFAQ, { catalog, config: null }),
  );
  expect(html).toContain("Проверьте статус заказа, не платите повторно.");
  expect(html).toContain('href="#payments"');
  expect(html).toContain("Открыть платежи");
});

it("offers contextual support from a saved payment without including provider IDs or amounts", () => {
  const payments = [
    {
      id: "provider-marker",
      order_id: "order-safe",
      product_name: "Тариф",
      status: "paid",
      amount: "125.00",
      currency: "RUB",
      paid_at: "2026-10-09T08:00:00Z",
    },
  ] as Dashboard["payments"];
  const html = renderToStaticMarkup(
    createElement(PaymentHistory, {
      payments,
      ...{ supportUrl: "https://t.me/synthetic_support" },
    }),
  );
  const href = html.match(/href="(https:\/\/t.me\/[^\"]+)"/)?.[1];
  expect(href).toBeDefined();
  const draft = new URL(href!).searchParams.get("text");
  expect(draft).toContain("order-safe");
  expect(draft).not.toContain("provider-marker");
  expect(draft).not.toContain("125.00");
});
