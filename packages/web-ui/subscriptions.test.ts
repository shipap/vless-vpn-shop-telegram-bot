import { expect, it } from "vitest";
import { subscriptionLabel } from "./subscriptions";
it("distinguishes duplicate names without showing technical identities", () => {
  const subscriptions = [
    {
      id: "742e9916-19a8-492e-8a12-a505f806faf3",
      name: "Моя подписка",
      product_id: "standard",
    },
    { id: "9007199254740993", name: "Моя подписка", product_id: "standard" },
  ];
  const products = [{ id: "standard", name: "Стандартный" }];
  expect(subscriptionLabel(subscriptions[0], subscriptions, products)).toBe(
    "Моя подписка · Стандартный · 1",
  );
  expect(subscriptionLabel(subscriptions[1], subscriptions, products)).toBe(
    "Моя подписка · Стандартный · 2",
  );
});
