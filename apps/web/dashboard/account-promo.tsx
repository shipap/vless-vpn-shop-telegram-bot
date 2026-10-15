import { useEffect, useState, type FormEvent } from "react";
import { Gift } from "lucide-react";
import { ApiError, api, useApi } from "@api/client";
import type { components } from "@api/schema";
import type { Catalog, Subscription } from "@api/types";
import { Button, ErrorState, Loading } from "@ui/components";
import { LegalCheckbox } from "@ui/legal";
import { date, money } from "@ui/content";
import { subscriptionLabel } from "@ui/subscriptions";
import { promoCopy as copy } from "./promos-copy";

type PendingPromo = components["schemas"]["PendingPromoDto"];
type PromoClaim = components["schemas"]["PromoClaimDto"];

export function PromoReward({ promo }: { promo: PendingPromo }) {
  return (
    <span>
      {promo.reward_type === "free_days"
        ? `${copy.free}: ${promo.reward_days}`
        : promo.reward_type === "fixed_rub"
          ? `${copy.fixed}: ${money(promo.amount_rub, "RUB")}`
          : `${copy.percent}: ${promo.percent}%`}
    </span>
  );
}

export function AccountPromo({
  initial,
  subscriptions,
  catalog,
  onComplete,
  onLegalStale,
}: {
  initial: PendingPromo | null;
  subscriptions: Subscription[];
  catalog: Catalog | null;
  onComplete: () => void;
  onLegalStale: () => void;
}) {
  const resource = useApi<PendingPromo | null>("/promos/active");
  const active = resource.loading || resource.error ? initial : resource.data;
  const [code, setCode] = useState("");
  const [target, setTarget] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [claim, setClaim] = useState<PromoClaim | null>(null);
  useEffect(() => setAccepted(false), [catalog?.legal.revision]);
  const available = subscriptions.filter(
    (subscription) =>
      !["revoked", "expired"].includes(subscription.status) &&
      subscription.expires_at != null &&
      Date.parse(subscription.expires_at) > Date.now(),
  );
  const selected =
    available.length === 1
      ? available[0].id
      : available.some((subscription) => subscription.id === target)
        ? target
        : "";
  async function activate(event: FormEvent) {
    event.preventDefault();
    if (active || resource.loading || resource.error) return;
    setBusy(true);
    setError(null);
    setClaim(null);
    try {
      resource.setData(
        await api<PendingPromo>("/promos/activate", {
          method: "POST",
          body: { code: code.trim() },
        }),
      );
      setCode("");
      onComplete();
    } catch (failure) {
      setError(failure);
      onComplete();
      resource.reload();
    } finally {
      setBusy(false);
    }
  }
  async function receive(event: FormEvent) {
    event.preventDefault();
    if (!active?.available || !accepted || !catalog?.legal.configured) return;
    if (available.length > 1 && !selected) {
      setError(new Error(copy.chooseFirst));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api("/legal/accept", {
        method: "POST",
        body: { documents: catalog.legal.documents, context: "checkout" },
      });
      const result = await api<PromoClaim>("/promos/claim", {
        method: "POST",
        body: { code: active.code, subscription_id: selected || null },
      });
      setClaim(result);
      resource.setData(null);
      setTarget("");
      setAccepted(false);
      onComplete();
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && failure.code.startsWith("legal_")) {
        setAccepted(false);
        onLegalStale();
      }
      onComplete();
      resource.reload();
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="card account-promo"
      aria-labelledby="account-promo-title"
    >
      <div className="card-head">
        <h2 id="account-promo-title">{copy.title}</h2>
        <Gift aria-hidden="true" />
      </div>
      {resource.loading && <Loading />}
      {Boolean(resource.error) && (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      )}
      {Boolean(error) && <ErrorState error={error} />}
      {claim && (
        <div className="notice" role="status">
          <strong>{copy.giftReceived}</strong>
          <p>
            {copy.free}: {claim.reward_days} · действует до{" "}
            {date(claim.expires_at, true)}
          </p>
        </div>
      )}
      {active ? (
        <>
          <div className="promo-active" role="status">
            <span className="eyebrow">{copy.active}</span>
            <strong>{active.code}</strong>
            <PromoReward promo={active} />
          </div>
          <p className="small-copy">
            {active.reward_type === "free_days"
              ? copy.giftPending
              : copy.pending}
          </p>
          {!active.available && active.unavailable_reason && (
            <p className="notice" role="status">
              {active.unavailable_reason}
            </p>
          )}
          {active.reward_type === "free_days" && (
            <form className="form" onSubmit={receive}>
              {available.length > 1 ? (
                <label>
                  {copy.target}
                  <select
                    required
                    value={target}
                    onChange={(event) => setTarget(event.target.value)}
                  >
                    <option value="">{copy.targetPlaceholder}</option>
                    {available.map((subscription) => (
                      <option key={subscription.id} value={subscription.id}>
                        {subscriptionLabel(
                          subscription,
                          available,
                          catalog?.products || [],
                        )}
                      </option>
                    ))}
                  </select>
                </label>
              ) : available.length === 1 ? (
                <p>
                  {copy.target}:{" "}
                  {subscriptionLabel(
                    available[0],
                    available,
                    catalog?.products || [],
                  )}
                </p>
              ) : (
                <p>
                  {copy.newSubscription}{" "}
                  {
                    catalog?.products.find(
                      (product) => product.id === active.product_id,
                    )?.name
                  }
                </p>
              )}
              <LegalCheckbox
                legal={catalog?.legal || null}
                checked={accepted}
                onChange={setAccepted}
              />
              <Button
                type="submit"
                busy={busy}
                disabled={
                  resource.loading ||
                  !active.available ||
                  Boolean(resource.error) ||
                  !accepted ||
                  !catalog?.legal.configured ||
                  (available.length > 1 && !selected)
                }
              >
                {copy.claim}
              </Button>
            </form>
          )}
        </>
      ) : (
        <form className="form" onSubmit={activate}>
          <p className="small-copy">{copy.empty}</p>
          <label>
            {copy.title}
            <input
              required
              autoComplete="off"
              maxLength={40}
              pattern={"[A-Za-z0-9_\\-]+"}
              value={code}
              onChange={(event) => setCode(event.target.value.trim())}
              disabled={busy || resource.loading || Boolean(resource.error)}
            />
          </label>
          <Button
            type="submit"
            busy={busy}
            disabled={
              resource.loading || Boolean(resource.error) || !code.trim()
            }
          >
            {copy.activate}
          </Button>
        </form>
      )}
    </section>
  );
}

export function CheckoutPromo({ onProfile }: { onProfile: () => void }) {
  const resource = useApi<PendingPromo | null>("/promos/active");
  if (resource.loading) return <Loading />;
  if (resource.error)
    return <ErrorState error={resource.error} onRetry={resource.reload} />;
  const active = resource.data;
  return active && active.reward_type !== "free_days" ? (
    <div className="notice">
      <strong>
        {copy.active}: {active.code}
      </strong>
      <p>
        <PromoReward promo={active} />
      </p>
      <p className="small-copy">
        {active.available ? copy.checkout : active.unavailable_reason}
      </p>
    </div>
  ) : (
    <a className="text-link" href="#account" onClick={onProfile}>
      {active ? copy.claim : copy.profileLink}
    </a>
  );
}
