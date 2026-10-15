import { describe, expect, it } from "vitest";
import {
  campaignHref,
  captureCampaignProofs,
  recordCampaignVisit,
  marketingVisitor,
} from "./campaign";

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (k: string) => values.get(k) ?? null,
    setItem: (k: string, v: string) => {
      values.set(k, v);
    },
    removeItem: (k: string) => {
      values.delete(k);
    },
    values,
  };
}
const first = "a".repeat(64);
const last = "b".repeat(64);
describe("bounded marketing attribution", () => {
  it("keeps first and last verified visits separately from referral, without extending first expiry", () => {
    const store = storage();
    recordCampaignVisit(
      {
        campaign_id: "x".repeat(24),
        visit_token: first,
        max_age_seconds: 2592000,
        expires_at: new Date(1000 + 2592000000).toISOString(),
      },
      store,
      1000,
    );
    recordCampaignVisit(
      {
        campaign_id: "y".repeat(24),
        visit_token: last,
        max_age_seconds: 2592000,
        expires_at: new Date(2000 + 2592000000).toISOString(),
      },
      store,
      2000,
    );
    const url = new URL(
      campaignHref("https://dashboard.example.test/?ref=ref_7", store, 2000),
    );
    expect(url.searchParams.get("ref")).toBe("ref_7");
    expect(url.searchParams.get("utm_first")).toBe(first);
    expect(url.searchParams.get("utm_last")).toBe(last);
    expect(captureCampaignProofs(url.href, store, 1000 + 2592000000)).toEqual([
      last,
    ]);
  });
  it("rejects malformed, ambiguous and expired proof queries", () => {
    const store = storage();
    expect(
      captureCampaignProofs(
        `https://dashboard.example.test/?utm_first=${first}&utm_first=${last}`,
        store,
        1000,
      ),
    ).toEqual([]);
    expect(
      captureCampaignProofs(
        `https://dashboard.example.test/?utm_first=${first}&utm_expires=999`,
        store,
        1000,
      ),
    ).toEqual([]);
    expect(
      captureCampaignProofs(
        "https://dashboard.example.test/?utm_first=<script>",
        store,
        1000,
      ),
    ).toEqual([]);
  });
  it("visitor ID remains stable for thirty days, rotates at expiry and stores no identity", () => {
    const store = storage();
    expect(marketingVisitor(store, 1000, () => "synthetic-visitor-0001")).toBe(
      "synthetic-visitor-0001",
    );
    expect(marketingVisitor(store, 1001, () => "synthetic-visitor-0002")).toBe(
      "synthetic-visitor-0001",
    );
    expect(
      marketingVisitor(store, 2592001000, () => "synthetic-visitor-0002"),
    ).toBe("synthetic-visitor-0002");
    expect([...store.values.values()].join("")).not.toContain("email");
  });
});
