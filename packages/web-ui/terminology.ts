import type { InstanceTerminology, PublicInstance } from "@api/types";

export const defaultTerminology: InstanceTerminology = {
  service_label: "VPN",
  service_summary: "VPN для ваших устройств",
  connections_label: "Мой VPN",
  connect_label: "Подключить VPN",
  client_label: "VPN-приложение",
  connection_label: "VPN-подключение",
  hero_title: "Ваш VPN.",
};

export function publicTerminology(value: unknown): InstanceTerminology {
  const input =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  return Object.fromEntries(
    Object.entries(defaultTerminology).map(([key, fallback]) => {
      const candidate = input[key];
      return [
        key,
        typeof candidate === "string" &&
        candidate.trim() &&
        Array.from(candidate).length <= 240 &&
        !/[\u0000-\u001f\u007f]/.test(candidate)
          ? candidate.trim()
          : fallback,
      ];
    }),
  ) as InstanceTerminology;
}

export function instanceTerminology(
  instance: PublicInstance,
): InstanceTerminology {
  return publicTerminology(instance.terminology);
}
