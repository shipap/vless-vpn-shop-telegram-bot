import { ArrowUpRight, Plus } from "lucide-react";
import type { Catalog, PublicConfig } from "@api/types";
import { safeUrl, ContentOrText } from "@ui/content";
import { faqAction } from "@ui/practical-faq";

export type HelpCenter = {
  connection_instructions?: string;
  faq?: { id: string; title: string; body: string }[];
  custom_faq?: {
    id: string;
    question: string;
    answer: unknown;
    body?: string;
  }[];
  support_url?: string;
  dashboard_url?: string;
  promo_channels?: { title: string; url: string }[];
};

export function helpCenter(catalog: Catalog | null): HelpCenter {
  const content = catalog?.content as unknown as { help_center?: HelpCenter };
  return content?.help_center || {};
}

export function ServiceFAQ({
  catalog,
  config,
}: {
  catalog: Catalog | null;
  config: PublicConfig | null;
}) {
  const help = helpCenter(catalog);
  const documents = catalog?.legal?.documents || [];
  const support = safeUrl(help.support_url || config?.support_url);
  return (
    <div className="stack service-faq">
      <section className="card">
        <p className="eyebrow">ПОЛЕЗНОЕ О ПОДПИСКЕ</p>
        <h2>FAQ — часто задаваемые вопросы</h2>
        <p>
          Не получается оплатить, подключиться или управлять подпиской? Выберите
          ситуацию ниже.
        </p>
        <div className="faq-documents">
          {["offer", "privacy"].map((kind) => {
            const url = safeUrl(
              documents.find((item) => item.document_type === kind)?.url,
            );
            return url ? (
              <a
                key={kind}
                className="button secondary"
                href={url}
                target="_blank"
                rel="noreferrer"
              >
                {kind === "offer"
                  ? "Публичная оферта"
                  : "Политика конфиденциальности"}
                <ArrowUpRight size={16} />
              </a>
            ) : null;
          })}
        </div>
      </section>
      {support && (
        <a
          className="button secondary"
          href={support}
          target="_blank"
          rel="noreferrer"
        >
          Техническая поддержка
          <ArrowUpRight size={16} />
        </a>
      )}
      {(help.faq || []).map((topic) => (
        <details className="help-question" key={topic.id}>
          <summary>
            {topic.title}
            <Plus size={17} />
          </summary>
          <div className="faq-answer">
            <p>{topic.body}</p>
            {topic.id === "purchase" &&
              safeUrl(help.dashboard_url || config?.dashboard_url) && (
                <a
                  className="text-link"
                  href={safeUrl(help.dashboard_url || config?.dashboard_url)}
                >
                  Перейти на сайт
                  <ArrowUpRight size={15} />
                </a>
              )}
            {topic.id === "promos" && (
              <div className="faq-channels">
                {(help.promo_channels || []).map(
                  (channel) =>
                    safeUrl(channel.url) && (
                      <a
                        key={channel.url}
                        className="text-link"
                        href={safeUrl(channel.url)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {channel.title}
                        <ArrowUpRight size={15} />
                      </a>
                    ),
                )}
              </div>
            )}
          </div>
        </details>
      ))}
      {(Array.isArray(help.custom_faq) ? help.custom_faq : [])
        .filter((topic) => topic && typeof topic.question === "string")
        .map((topic, index) => (
          <details
            className="help-question"
            key={`published-${topic.id || index}`}
          >
            <summary>
              {topic.question}
              <Plus size={17} />
            </summary>
            <div className="faq-answer">
              <ContentOrText
                nodes={topic.answer}
                text={
                  typeof topic.answer === "string"
                    ? topic.answer
                    : typeof topic.body === "string"
                      ? topic.body
                      : "Ответ временно недоступен. Обратитесь в поддержку."
                }
              />
              {faqAction(topic.id) && (
                <a className="text-link" href={faqAction(topic.id)!.href}>
                  {faqAction(topic.id)!.label}
                  <ArrowUpRight size={15} />
                </a>
              )}
            </div>
          </details>
        ))}
    </div>
  );
}
