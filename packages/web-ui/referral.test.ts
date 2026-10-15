import { describe, expect, it } from "vitest";
import { captureReferral, referralHref } from "./referral";

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
    values,
  };
}
describe("website referral persistence", () => {
  it("preserves the first valid BIGINT code across visits without refreshing its thirty day TTL", () => {
    const store = storage();
    expect(
      captureReferral(
        "https://landing.example.test/?ref=ref_9007199254740993",
        store,
        1000,
      ),
    ).toBe("ref_9007199254740993");
    expect(
      captureReferral("https://landing.example.test/?ref=ref_7", store, 2000),
    ).toBe("ref_9007199254740993");
    expect(
      captureReferral("https://landing.example.test/", store, 2592000999),
    ).toBe("ref_9007199254740993");
    expect(
      captureReferral("https://landing.example.test/", store, 2592001000),
    ).toBeUndefined();
    expect(
      captureReferral(
        "https://landing.example.test/?ref=ref_7",
        store,
        2592001001,
      ),
    ).toBe("ref_7");
  });
  it.each([
    "ref_0",
    "ref_-1",
    "ref_+1",
    "ref_01",
    "ref_9223372036854775808",
    "ref_1&ref=ref_2",
    "ref_1&ref=ref_1",
    "ref_1%00",
    "x".repeat(5000),
  ])("ignores malformed or ambiguous incoming referral %s", (query) => {
    expect(
      captureReferral(
        "https://landing.example.test/?ref=" + query,
        storage(),
        1000,
      ),
    ).toBeUndefined();
  });
  it("ignores corrupt storage and accepts a bounded current referral", () => {
    const store = storage();
    store.setItem("sunrise.referral.v1", "x".repeat(4096));
    expect(
      captureReferral("https://landing.example.test/?ref=ref_7", store, 1000),
    ).toBe("ref_7");
    expect([...store.values.values()][0].length).toBeLessThan(256);
  });
  it("rejects persisted claims extending expiry beyond thirty days", () => {
    const store = storage();
    store.setItem(
      "sunrise.referral.v1",
      JSON.stringify({ code: "ref_7", expiresAt: 999999999999 }),
    );
    expect(
      captureReferral("https://landing.example.test/", store, 1000),
    ).toBeUndefined();
  });
  it("leaves navigation usable when browser storage throws", () => {
    const store = {
      getItem() {
        throw new Error("denied");
      },
      setItem() {
        throw new Error("denied");
      },
      removeItem() {
        throw new Error("denied");
      },
    };
    expect(
      captureReferral("https://landing.example.test/?ref=ref_7", store, 1000),
    ).toBe("ref_7");
  });
  it("preserves the original landing expiry when handing off to a separately configured dashboard origin", () => {
    const dashboard = storage();
    expect(
      captureReferral(
        "https://dashboard.example.test/?ref=ref_7&ref_expires=2592001000",
        dashboard,
        2592000000,
      ),
    ).toBe("ref_7");
    expect(
      captureReferral("https://dashboard.example.test/", dashboard, 2592001000),
    ).toBeUndefined();
  });
  it.each(["999", "999999999999", "undefined", "2000&ref_expires=3000"])(
    "rejects malformed, expired, or excessive cross-origin expiry %s",
    (expiry) => {
      expect(
        captureReferral(
          "https://dashboard.example.test/?ref=ref_7&ref_expires=" + expiry,
          storage(),
          1000,
        ),
      ).toBeUndefined();
    },
  );
  it("adds the referral and tariff through URL parameters without copying input URL secrets", () => {
    expect(
      referralHref(
        "https://dashboard.example.test/shop?campaign=spring#plans",
        "ref_9007199254740993",
        "tariff /1",
        "https://landing.example.test/?initData=secret&state=secret",
      ),
    ).toBe(
      "https://dashboard.example.test/shop?campaign=spring&ref=ref_9007199254740993&product=tariff+%2F1#plans",
    );
    expect(
      referralHref(
        "/?ref=ref_7",
        undefined,
        "standard",
        "http://127.0.0.1:3100/",
      ),
    ).toBe("/?ref=ref_7&product=standard");
  });
  it.each([
    "javascript:alert(1)",
    "//evil.example.test",
    "https://user:pass@dashboard.example.test/",
  ])(
    "does not turn untrusted navigation into a referral destination %s",
    (destination) => {
      expect(
        referralHref(
          destination,
          "ref_7",
          undefined,
          "https://landing.example.test/",
        ),
      ).toBe("");
    },
  );
});
