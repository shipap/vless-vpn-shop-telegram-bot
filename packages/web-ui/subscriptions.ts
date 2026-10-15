type Subscription = { id: string; name: string; product_id: string | null };
export function subscriptionLabel(
  subscription: Subscription,
  subscriptions: Subscription[],
  products: { id: string; name: string }[],
) {
  const name = subscription.name || "Подписка";
  const product =
    products.find((item) => item.id === subscription.product_id)?.name ||
    "Тариф уточняется";
  const duplicates = subscriptions.filter(
    (item) => item.name === subscription.name,
  );
  const ordinal =
    duplicates.length > 1
      ? ` · ${duplicates.findIndex((item) => item.id === subscription.id) + 1}`
      : "";
  return `${name} · ${product}${ordinal}`;
}
