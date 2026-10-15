import starters from "./practical-faq.example.json";
import type { ContentNode } from "./content";

export type PublishedFaq = {
  id: string;
  question: string;
  answer: ContentNode[];
};
export function addPracticalFaq(current: PublishedFaq[]): PublishedFaq[] {
  const ids = new Set(current.map((item) => item.id));
  const missing = starters
    .filter((item) => !ids.has(item.id))
    .slice(0, Math.max(0, 30 - current.length));
  return [...current, ...structuredClone(missing)];
}

export function faqAction(id: string): { href: string; label: string } | null {
  return (
    (
      {
        help_payment_access: { href: "#payments", label: "Открыть платежи" },
        help_vpn_connection: { href: "#home", label: "Открыть подписку" },
        help_device_limit: { href: "#devices", label: "Открыть устройства" },
        help_email_code: { href: "#account", label: "Открыть профиль" },
        help_cancel_recurring: {
          href: "#home",
          label: "Открыть автопродление подписки",
        },
        help_refund_request: { href: "#payments", label: "Открыть платежи" },
      } as Record<string, { href: string; label: string }>
    )[id] ?? null
  );
}
