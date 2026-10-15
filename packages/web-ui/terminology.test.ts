import { expect, it } from "vitest";
import { publicInstance, neutralInstance } from "./instance-value";
import { instanceHtml } from "../../apps/web/instance-meta";

it("keeps complete VPN terminology for legacy public instance payloads", () => {
  const instance = publicInstance({
    ...neutralInstance,
    terminology: undefined,
  });
  expect(instance.terminology).toMatchObject({
    service_label: "VPN",
    service_summary: "VPN для ваших устройств",
    connections_label: "Мой VPN",
    connect_label: "Подключить VPN",
    client_label: "VPN-приложение",
    connection_label: "VPN-подключение",
    hero_title: "Ваш VPN.",
  });
});

it("reads terminology from config irrespective of visual profile and rejects invalid phrases", () => {
  const instance = publicInstance({
    ...neutralInstance,
    visual_profile: "neutral",
    terminology: {
      service_label: "Защищённое подключение",
      service_summary: "Защищённое подключение для ваших устройств",
      connect_label: "Настроить подключение",
      connections_label: "Мои подключения",
      client_label: "совместимое приложение",
      connection_label: "защищённое подключение",
      hero_title: "  Ваше защищённое подключение.  ",
      unexpected: "not public",
    },
  });
  expect(instance.terminology).toMatchObject({
    service_label: "Защищённое подключение",
    connect_label: "Настроить подключение",
    connections_label: "Мои подключения",
    hero_title: "Ваше защищённое подключение.",
  });
  expect(instance.terminology).not.toHaveProperty("unexpected");
  const invalid = publicInstance({
    ...neutralInstance,
    terminology: {
      service_label: "",
      connect_label: "bad\u0000label",
      connections_label: "x".repeat(241),
      hero_title: ["invalid"],
    },
  });
  expect(invalid.terminology).toMatchObject({
    service_label: "VPN",
    connect_label: "Подключить VPN",
    connections_label: "Мой VPN",
    hero_title: "Ваш VPN.",
  });
});

it("accepts the same 240 character phrase limit as the typed instance contract", () => {
  expect(
    publicInstance({
      ...neutralInstance,
      terminology: { hero_title: "x".repeat(240) },
    }).terminology?.hero_title,
  ).toBe("x".repeat(240));
});

it.each([120, 240, 241])(
  "counts %i supplementary Unicode characters like Python",
  (count) => {
    const phrase = "🌐".repeat(count);
    expect(
      publicInstance({
        ...neutralInstance,
        terminology: { hero_title: phrase },
      }).terminology?.hero_title,
    ).toBe(count <= 240 ? phrase : "Ваш VPN.");
  },
);

it("uses configured service summary in served HTML metadata before hydration", () => {
  const instance = publicInstance({
    ...neutralInstance,
    app_name: "Тестовый сервис",
    terminology: {
      service_summary: "Защищённое подключение для ваших устройств",
    },
  });
  const html = instanceHtml(
    "<html><head><title>Old</title></head><body></body></html>",
    instance,
    true,
  );
  expect(html).toContain(
    "<title>Защищённое подключение для ваших устройств — Тестовый сервис</title>",
  );
  expect(html).toContain('content="noindex,nofollow,noarchive"');
  expect(html).toContain("window.__PUBLIC_INSTANCE__=");
});
