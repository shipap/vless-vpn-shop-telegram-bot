import { ArrowRight, ArrowUpRight } from "lucide-react";
import { useApi } from "@api/client";
import { useState } from "react";
import { useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";
import type { Catalog } from "@api/types";
import { Brand, ErrorState, Loading } from "@ui/components";
import {
  safeUrl,
  SemanticContent,
  CatalogDescription,
  validatedContentNodes,
  type ContentNode,
} from "@ui/content";
import { landingCopy } from "./copy";
import { TariffCard } from "./TariffCard";
import { CatalogComparison } from "@ui/catalog-comparison";
import { campaignHref, campaignStorage } from "@ui/campaign";
import { useLandingCampaign } from "@ui/campaign-hooks";
import {
  browserReferral,
  browserReferralExpiry,
  referralHref,
} from "@ui/referral";

export function Landing() {
  const instance = useInstance();
  const copy = landingCopy(instanceTerminology(instance));
  const [failedHero, setFailedHero] = useState<string | null>(null);
  useLandingCampaign();
  const catalog = useApi<Catalog>("/catalog", true);
  const dashboard = campaignHref(
    referralHref(
      safeUrl(instance.dashboard_url),
      browserReferral(),
      undefined,
      undefined,
      browserReferralExpiry(),
    ),
    campaignStorage(),
  );
  const bot = instance.telegram_bot_username
    ? `https://t.me/${instance.telegram_bot_username}`
    : undefined;
  const support = safeUrl(instance.support_url);
  const products = (
    catalog.data?.products.filter(
      (product) => product.available && product.visible_landing !== false,
    ) ?? []
  ).sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  const faq = Array.isArray(catalog.data?.content?.faq)
    ? (catalog.data.content.faq as {
        id: string;
        question: string;
        answer: ContentNode[];
      }[])
    : [];
  const configuredSteps = Array.isArray(catalog.data?.content?.how_to_connect)
    ? (catalog.data.content.how_to_connect as {
        id: string;
        title: string;
        body: ContentNode[];
      }[])
    : [];
  const documents = catalog.data?.legal?.documents ?? [];
  const catalogText =
    typeof catalog.data?.content?.catalog_description === "string"
      ? catalog.data.content.catalog_description
      : "";
  const catalogNodes = validatedContentNodes(
    catalog.data?.content?.catalog_description_content,
  );
  const hasCatalogIntroduction = Boolean(catalogText.trim() || catalogNodes);
  return (
    <div className="sunrise-landing">
      <a className="skip-link" href="#main">
        {copy.skip}
      </a>
      <header className="sunrise-header page-width">
        <Brand />
        <nav aria-label={copy.navigation}>
          {copy.nav.map((item) => (
            <a key={item.href} href={item.href}>
              {item.label}
            </a>
          ))}
        </nav>
        {dashboard && (
          <a className="sunrise-account" href={dashboard}>
            {copy.account}
            <ArrowUpRight size={17} aria-hidden="true" />
          </a>
        )}
      </header>
      <main id="main">
        <section
          className="sunrise-hero page-width"
          aria-labelledby="hero-title"
        >
          <div className="sunrise-hero-copy">
            <p className="sunrise-eyebrow">{copy.eyebrow}</p>
            <h1 id="hero-title">
              {copy.title}
              <br />
              <span>
                {instance.visual_profile === "private"
                  ? copy.horizon
                  : "Для ваших устройств."}
              </span>
            </h1>
            <p className="sunrise-lead">{instance.app_description}</p>
            <a href="#tariffs" className="sunrise-cta">
              {copy.choose}
              <ArrowRight size={21} aria-hidden="true" />
            </a>
          </div>
          {instance.hero_url && failedHero !== instance.hero_url ? (
            <div className="sunrise-visual">
              {instance.visual_profile === "private" && (
                <div className="sunrise-horizon" aria-hidden="true" />
              )}
              <img
                className="sunrise-mascot"
                src={instance.hero_url}
                width="1024"
                height="1024"
                alt=""
                onError={() => setFailedHero(instance.hero_url)}
                fetchPriority="high"
                decoding="async"
              />
            </div>
          ) : (
            <div className="neutral-service-summary">
              <span>ЛИЧНЫЙ КАБИНЕТ</span>
              <p>Подписки, устройства и история платежей в одном месте.</p>
              <a href="#setup">
                Как подключиться <ArrowRight size={18} aria-hidden="true" />
              </a>
            </div>
          )}
        </section>
        <section
          id="tariffs"
          className="sunrise-section"
          aria-label={copy.tariffs}
        >
          <div className="page-width sunrise-section-grid">
            <div className="sunrise-section-heading">
              {hasCatalogIntroduction ? (
                <CatalogDescription nodes={catalogNodes} text={catalogText} />
              ) : (
                <>
                  <h2 id="tariffs-title">{copy.tariffs}</h2>
                  <p>{copy.tariffLead}</p>
                </>
              )}
            </div>
            <div aria-live="polite">
              {catalog.loading ? (
                <Loading />
              ) : catalog.error ? (
                <ErrorState error={catalog.error} onRetry={catalog.reload} />
              ) : products.length ? (
                <>
                  <div className="sunrise-tariffs">
                    {products.map((product) => (
                      <TariffCard
                        key={product.id}
                        product={product}
                        dashboard={dashboard}
                      />
                    ))}
                  </div>
                  <CatalogComparison products={products} />
                </>
              ) : (
                <div className="sunrise-empty">
                  <h3>{copy.empty}</h3>
                  <p>{copy.emptyLead}</p>
                  {support && (
                    <a href={support}>
                      {copy.support}
                      <ArrowUpRight size={17} aria-hidden="true" />
                    </a>
                  )}
                </div>
              )}
            </div>
          </div>
        </section>
        <section
          id="setup"
          className="sunrise-section"
          aria-labelledby="setup-title"
        >
          <div className="page-width sunrise-section-grid">
            <div className="sunrise-section-heading">
              <h2 id="setup-title">{copy.setup}</h2>
              <p>{copy.setupLead}</p>
            </div>
            <ol className="sunrise-steps">
              {configuredSteps.length
                ? configuredSteps.map((step, index) => (
                    <li key={step.id}>
                      <span className="sunrise-step-index" aria-hidden="true">
                        {index + 1}
                      </span>
                      <h3>{step.title}</h3>
                      <div className="sunrise-step-body">
                        <SemanticContent nodes={step.body} />
                      </div>
                    </li>
                  ))
                : copy.steps.map((step, index) => (
                    <li key={step.title}>
                      <span className="sunrise-step-index" aria-hidden="true">
                        {index + 1}
                      </span>
                      <h3>{step.title}</h3>
                      <p>{step.body}</p>
                    </li>
                  ))}
            </ol>
          </div>
        </section>
        <section
          id="faq"
          className="sunrise-section"
          aria-labelledby="faq-title"
        >
          <div className="page-width sunrise-section-grid">
            <div className="sunrise-section-heading">
              <h2 id="faq-title">{copy.faq}</h2>
              <p>{copy.faqLead}</p>
              {support && (
                <a className="sunrise-text-link" href={support}>
                  {copy.support}
                  <ArrowUpRight size={17} aria-hidden="true" />
                </a>
              )}
            </div>
            <div className="sunrise-faq">
              {catalog.loading ? (
                <Loading />
              ) : catalog.error ? (
                <ErrorState error={catalog.error} onRetry={catalog.reload} />
              ) : faq.length ? (
                faq.map((item) => (
                  <details key={item.id}>
                    <summary>
                      {item.question}
                      <span aria-hidden="true">+</span>
                    </summary>
                    <div className="sunrise-faq-answer">
                      <SemanticContent nodes={item.answer} />
                    </div>
                  </details>
                ))
              ) : (
                <p>{copy.noFaq}</p>
              )}
            </div>
          </div>
        </section>
        <section className="sunrise-outro" aria-labelledby="outro-title">
          <div className="page-width">
            <div>
              <h2 id="outro-title">{copy.outro}</h2>
              <p>{copy.outroLead}</p>
            </div>
            <a className="sunrise-cta" href="#tariffs">
              {copy.choose}
              <ArrowRight size={21} aria-hidden="true" />
            </a>
          </div>
        </section>
      </main>
      <footer className="sunrise-footer page-width">
        <div className="sunrise-footer-top">
          <Brand />
          <nav aria-label="Навигация в подвале">
            {copy.nav.map((item) => (
              <a key={item.href} href={item.href}>
                {item.label}
              </a>
            ))}
          </nav>
          <span>
            © {new Date().getFullYear()} {instance.app_name}
          </span>
        </div>
        <div className="sunrise-footer-links">
          {documents.map(
            (document) =>
              safeUrl(document.url) && (
                <a key={document.document_type} href={safeUrl(document.url)}>
                  {document.document_type === "offer"
                    ? copy.offer
                    : copy.privacy}
                </a>
              ),
          )}
          {bot && <a href={bot}>{copy.telegram}</a>}
          {support && <a href={support}>{copy.support}</a>}
        </div>
      </footer>
    </div>
  );
}
