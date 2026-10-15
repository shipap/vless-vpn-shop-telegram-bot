// Храним только публичный код приглашения; данные входа сюда не попадают.
const key = "sunrise.referral.v1";
const lifetime = 30 * 24 * 60 * 60 * 1000;
type ReferralStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type SavedReferral = { code: string; expiresAt: number };

function validCode(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^ref_[1-9][0-9]{0,18}$/.test(value) &&
    BigInt(value.slice(4)) < 9223372036854775808n
  );
}

function savedReferral(
  storage: ReferralStorage,
  moment: number,
): SavedReferral | undefined {
  try {
    const raw = storage.getItem(key);
    if (raw && raw.length <= 256) {
      const saved: unknown = JSON.parse(raw);
      if (
        saved &&
        typeof saved === "object" &&
        "code" in saved &&
        "expiresAt" in saved &&
        validCode(saved.code) &&
        typeof saved.expiresAt === "number" &&
        Number.isSafeInteger(saved.expiresAt) &&
        saved.expiresAt > moment &&
        saved.expiresAt <= moment + lifetime
      )
        return { code: saved.code, expiresAt: saved.expiresAt };
    }
  } catch {
    /* Недоступное хранилище не блокирует вход. */
  }
}

export function captureReferral(
  href: string,
  storage: ReferralStorage,
  moment = Date.now(),
): string | undefined {
  const saved = savedReferral(storage, moment);
  if (saved) return saved.code;
  try {
    storage.removeItem(key);
  } catch {
    /* Недоступное хранилище не блокирует вход. */
  }
  let incoming: string | undefined;
  let expiresAt = moment + lifetime;
  try {
    const url = new URL(href);
    if (url.search.length > 4096) return;
    const codes = url.searchParams.getAll("ref");
    if (codes.length === 1 && validCode(codes[0])) incoming = codes[0];
    const expiry = url.searchParams.getAll("ref_expires");
    if (expiry.length) {
      if (expiry.length !== 1 || !/^[1-9][0-9]{0,15}$/.test(expiry[0])) return;
      expiresAt = Number(expiry[0]);
      if (
        !Number.isSafeInteger(expiresAt) ||
        expiresAt <= moment ||
        expiresAt > moment + lifetime
      )
        return;
    }
  } catch {
    return;
  }
  if (incoming) {
    try {
      storage.setItem(key, JSON.stringify({ code: incoming, expiresAt }));
    } catch {
      /* Код текущего перехода остаётся доступен без localStorage. */
    }
  }
  return incoming;
}

let ephemeral: string | null = null;
function browserStorage(): ReferralStorage {
  try {
    return window.localStorage;
  } catch {
    return {
      getItem: () => ephemeral,
      setItem: (_key, value) => {
        ephemeral = value;
      },
      removeItem: () => {
        ephemeral = null;
      },
    };
  }
}
export function browserReferral(): string | undefined {
  if (typeof window === "undefined") return;
  return captureReferral(window.location.href, browserStorage());
}
export function browserReferralExpiry(): number | undefined {
  if (typeof window === "undefined") return;
  return savedReferral(browserStorage(), Date.now())?.expiresAt;
}

export function referralPayload() {
  const code = browserReferral();
  return code ? { referral_code: code } : {};
}

export function referralHref(
  destination: string | undefined,
  code?: string,
  product?: string,
  baseHref = typeof location === "undefined"
    ? "http://localhost/"
    : location.href,
  expiresAt?: number,
): string {
  if (
    !destination ||
    destination.length > 4096 ||
    destination.startsWith("//") ||
    /[\\\s\u0000-\u001f]/.test(destination)
  )
    return "";
  try {
    const url = new URL(destination, baseHref);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      return "";
    if (validCode(code)) {
      url.searchParams.set("ref", code);
      if (
        typeof expiresAt === "number" &&
        Number.isSafeInteger(expiresAt) &&
        expiresAt > 0
      )
        url.searchParams.set("ref_expires", String(expiresAt));
    }
    if (product) url.searchParams.set("product", product);
    return destination.startsWith("/")
      ? url.pathname + url.search + url.hash
      : url.href;
  } catch {
    return "";
  }
}
