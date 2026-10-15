import { ArrowUpRight } from "lucide-react";
import { supportDraftUrl, type SupportContext } from "@ui/support-link";

type Props = { url: string | null | undefined; context: SupportContext };
export function SupportLink({ url, context }: Props) {
  const href = supportDraftUrl(url, context);
  if (!href) return null;
  return (
    <a
      className="button secondary"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
    >
      {context.kind === "order"
        ? "Поддержка по заказу"
        : "Поддержка по подписке"}
      <ArrowUpRight size={16} />
    </a>
  );
}
