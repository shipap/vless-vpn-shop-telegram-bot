import type { PublicInstance } from "../api-client/types.ts";
import { defaultTerminology, publicTerminology } from "./terminology.ts";
export function accentColors(color: string) {
  const linear = [1, 3, 5].map((index) => {
    const value = parseInt(color.slice(index, index + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  const luminance =
    linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  return {
    onAccent: luminance > 0.179 ? "#101316" : "#ffffff",
    textAccent: luminance > 0.25 ? color : "#B8D0DD",
  };
}
function instanceUrl(value: string): string | null {
  if (!value || /[\u0000-\u0020\\]/.test(value) || value.startsWith("//"))
    return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? value
      : null;
  } catch {
    return null;
  }
}
export const neutralInstance: PublicInstance = {
  app_name: "VLESS VPN Shop",
  app_short_name: "VLESS VPN Shop",
  app_slug: "vpn",
  app_description: "Подписка на VPN для ваших устройств.",
  visual_profile: "neutral",
  terminology: defaultTerminology,
  search_indexing_enabled: false,
  accent_color: "#B8D0DD",
  sender_name: "VLESS VPN Shop",
  public_site_url: null,
  dashboard_url: null,
  telegram_bot_username: null,
  support_url: null,
  support_email: null,
  logo_url: null,
  favicon_url: null,
  hero_url: null,
  og_image_url: null,
};
export function publicInstance(value: unknown): PublicInstance {
  if (!value || typeof value !== "object" || Array.isArray(value))
    return neutralInstance;
  const input = value as Record<string, unknown>;
  if (
    typeof input.app_name !== "string" ||
    !input.app_name.trim() ||
    input.app_name.length > 160 ||
    !["neutral", "private"].includes(String(input.visual_profile))
  )
    return neutralInstance;
  const text = (key: string, max: number, fallback: string) =>
    typeof input[key] === "string" && (input[key] as string).length <= max
      ? (input[key] as string)
      : fallback;
  const link = (key: string) =>
    typeof input[key] === "string" ? instanceUrl(input[key] as string) : null;
  const asset = (key: string, role: string) =>
    input[key] === `/api/v1/instance/assets/${role}`
      ? (input[key] as string)
      : null;
  return {
    app_name: input.app_name.trim(),
    app_short_name: text("app_short_name", 160, input.app_name),
    app_slug: /^[a-z0-9-]{1,48}$/.test(String(input.app_slug))
      ? String(input.app_slug)
      : "vpn",
    app_description: text("app_description", 1000, ""),
    visual_profile: input.visual_profile as "neutral" | "private",
    terminology: publicTerminology(input.terminology),
    search_indexing_enabled: input.search_indexing_enabled === true,
    accent_color: /^#[0-9a-f]{6}$/i.test(String(input.accent_color))
      ? String(input.accent_color)
      : neutralInstance.accent_color,
    sender_name: text("sender_name", 160, input.app_name),
    public_site_url: link("public_site_url"),
    dashboard_url: link("dashboard_url"),
    telegram_bot_username: text("telegram_bot_username", 32, "") || null,
    support_url: link("support_url"),
    support_email: text("support_email", 254, "") || null,
    logo_url: asset("logo_url", "logo"),
    favicon_url: asset("favicon_url", "favicon"),
    hero_url: asset("hero_url", "hero"),
    og_image_url: asset("og_image_url", "og"),
  };
}
