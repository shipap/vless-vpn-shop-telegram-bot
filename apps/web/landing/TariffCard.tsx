import { ArrowRight } from "lucide-react";
import type { Product } from "@api/types";
import {
  money,
  SemanticContent,
  CatalogDescription,
  plainTelegramText,
  validatedContentNodes,
  type ContentNode,
} from "@ui/content";
import { copy } from "./copy";
import { CustomEmoji } from "@ui/emoji";
import { referralHref } from "@ui/referral";

export function TariffCard({
  product,
  dashboard,
}: {
  product: Product;
  dashboard?: string;
}) {
  const name = validatedContentNodes(product.content?.name);
  const description = product.content?.description;
  return (
    <article
      className={`sunrise-tariff${product.highlighted ? " is-highlighted" : ""}`}
      data-product-id={product.id}
    >
      {product.icon && (
        <div className="sunrise-tariff-icon" aria-hidden="true">
          <CustomEmoji
            id={product.icon.custom_emoji_id ?? undefined}
            fallback={product.icon.fallback}
          />
        </div>
      )}
      <h3>
        {Array.isArray(name) && name.length ? (
          <SemanticContent nodes={name as ContentNode[]} />
        ) : (
          plainTelegramText(product.name)
        )}
      </h3>
      <CatalogDescription
        className="sunrise-tariff-description"
        nodes={description}
        text={product.description}
      />
      <p className="sunrise-devices">
        {copy.devices}: <strong>{product.included_devices}</strong>
      </p>
      <div className="sunrise-tariff-bottom">
        <div className="sunrise-price">
          {money(product.price, product.currency)}
          {product.base_period_days && (
            <small>
              / {product.base_period_days} {copy.days}
            </small>
          )}
        </div>
        {dashboard ? (
          <a
            className="sunrise-tariff-action"
            href={referralHref(dashboard, undefined, product.id)}
            aria-label={`${copy.choose}: ${plainTelegramText(product.name)}`}
          >
            <ArrowRight size={22} aria-hidden="true" />
          </a>
        ) : (
          <span className="sunrise-unavailable">{copy.unavailable}</span>
        )}
      </div>
    </article>
  );
}
