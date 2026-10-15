// Уведомления сервиса. Worker не перехватывает запросы и не хранит данные кабинета.
const internalTarget = (value) => {
  if (typeof value !== "string" || value.length > 128) return "/#home";
  if (value === "/?tab=subscriptions") return "/#devices";
  if (value === "/#history") return "/#payments";
  if (value === "/#subscriptions") return "/#home";
  return /^\/admin(?:#[a-z][a-z0-9_-]*)?$/.test(value) ||
    /^\/(?:#(?:home|devices|payments|bonuses|account|help|notifications))?$/.test(
      value,
    )
    ? value
    : "/#home";
};
self.addEventListener("push", (event) => {
  let data;
  try {
    data = event.data?.json();
  } catch {
    data = null;
  }
  if (!data || typeof data !== "object") return;
  const title =
    typeof data.title === "string" ? data.title.slice(0, 120) : "VPN-сервис";
  const body =
    typeof data.body === "string"
      ? data.body.slice(0, 512)
      : "Новое сообщение в кабинете.";
  const tag = typeof data.tag === "string" ? data.tag.slice(0, 64) : undefined;
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag,
      data: { url: internalTarget(data.url ?? data.target) },
    }),
  );
});
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const destination = new URL(
    internalTarget(event.notification.data?.url),
    self.location.origin,
  ).href;
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(async (clients) => {
        const client = clients.find((item) => {
          try {
            return new URL(item.url).origin === self.location.origin;
          } catch {
            return false;
          }
        });
        if (client) {
          await client.navigate(destination);
          await client.focus();
        } else await self.clients.openWindow(destination);
      }),
  );
});
