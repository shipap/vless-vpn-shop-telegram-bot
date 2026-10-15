import { useEffect, useState } from "react";
import { api } from "@api/client";
import {
  campaignStorage,
  captureCampaignProofs,
  clearCampaignQuery,
  marketingVisitor,
  recordCampaignVisit,
  type CampaignVisit,
} from "./campaign";
const visits = new Map<string, Promise<CampaignVisit>>();
export function useLandingCampaign() {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const url = new URL(location.href);
    const ids = url.searchParams.getAll("utm_id");
    if (
      ids.length !== 1 ||
      !/^[A-Za-z0-9_-]{24}$/.test(ids[0]) ||
      url.search.length > 8192
    )
      return;
    let active = true;
    const key = url.origin + url.pathname + url.search;
    let request = visits.get(key);
    if (!request) {
      request = api<CampaignVisit>("/campaigns/visit", {
        method: "POST",
        public: true,
        body: {
          campaign_id: ids[0],
          visitor_id: marketingVisitor(campaignStorage()),
        },
      });
      if (visits.size >= 20) visits.delete(visits.keys().next().value!);
      visits.set(key, request);
    }
    void request
      .then((response) => {
        if (active) {
          recordCampaignVisit(response, campaignStorage());
          setVersion((n) => n + 1);
        }
      })
      .catch(() => {
        visits.delete(key);
      });
    return () => {
      active = false;
    };
  }, []);
  return version;
}
export function useAttachCampaign(accountId?: string) {
  useEffect(() => {
    if (!accountId) return;
    const tokens = captureCampaignProofs(location.href, campaignStorage());
    if (!tokens.length) return;
    const abort = new AbortController();
    void api<{ status: string }>("/campaigns/attach", {
      method: "POST",
      body: { visit_tokens: tokens },
      signal: abort.signal,
    })
      .then((response) => {
        if (!abort.signal.aborted && response.status === "attached")
          clearCampaignQuery();
      })
      .catch(() => {
        /* Сбой рекламной атрибуции не влияет на аккаунт и оплаченный доступ. */
      });
    return () => abort.abort();
  }, [accountId]);
}
