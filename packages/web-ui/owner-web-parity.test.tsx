import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CatalogDescription, plainTelegramText } from "./content";
import { promoPayload } from "../../apps/web/dashboard/promo-editor";
import { QuoteDiscounts } from "../../apps/web/dashboard/customer";
import type { Quote } from "@api/types";

it("uses readable legacy Telegram fallbacks without exposing formatting tags", () => {
  expect(
    plainTelegramText('<b>Тариф</b> <tg-emoji emoji-id="123">🌍</tg-emoji>'),
  ).toBe("Тариф 🌍");
  const html = renderToStaticMarkup(
    <CatalogDescription
      nodes={[]}
      text={
        '<b>Описание</b> <tg-emoji emoji-id="123">🌍</tg-emoji>\n<script>unsafe</script>'
      }
    />,
  );
  expect(html).toContain("Описание ");
  expect(html).not.toContain("🌍");
  expect(html).not.toContain("tg-emoji");
  expect(html).not.toContain("&lt;b&gt;");
  expect(html).toContain("&lt;script&gt;");
});

it("does not send read-only promo counters back as editable conditions", () => {
  const body = promoPayload({
    code: "SAVE",
    enabled: true,
    percent: "0",
    reward_type: "fixed_rub",
    amount_rub: "100",
    activated_users: 3,
    redeemed_count: 2,
    remaining_uses: 8,
  });
  expect(body).not.toHaveProperty("activated_users");
  expect(body).not.toHaveProperty("redeemed_count");
  expect(body).not.toHaveProperty("remaining_uses");
  expect(body.amount_rub).toBe("100.00");
});

it("renders separate server discount amounts and an applied minimum without recalculating money", () => {
  const html = renderToStaticMarkup(
    <QuoteDiscounts
      quote={
        {
          currency: "RUB",
          promo_code: "SAVE",
          promo_discount_amount: "98.00",
          period_discount_amount: "10.00",
          secondary_discount_amount: "0.00",
          payment_method_discount_amount: "2.00",
          minimum_subscription_amount: "1.00",
          minimum_external_required: true,
        } as unknown as Quote
      }
    />,
  );
  expect(html).toContain("Промокод SAVE");
  expect(html).toContain("98 ₽");
  expect(html).toContain("Скидка за период");
  expect(html).toContain("10 ₽");
  expect(html).toContain("Другие скидки");
  expect(html).toContain("Скидка за способ оплаты");
  expect(html).toContain("Минимальная сумма к оплате");
  expect(html).toContain("1 ₽");
});
