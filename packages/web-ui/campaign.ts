// Только случайный ID посетителя и рекламные доказательства; срок хранения — 30 дней.
const proofKey = "sunrise.campaign.proofs.v1";
const visitorKey = "sunrise.campaign.visitor.v1";
const lifetime = 2592000000;
type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type Proof = { token: string; expiresAt: number };
type Proofs = { first?: Proof; last?: Proof };
export type CampaignVisit = {
  campaign_id: string;
  visit_token: string;
  expires_at: string;
  max_age_seconds: number;
};
const validProof = (v: unknown): v is string =>
  typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
function read<T>(store: StorageLike, key: string): T | null {
  try {
    const text = store.getItem(key);
    return text && text.length <= 2048 ? (JSON.parse(text) as T) : null;
  } catch {
    return null;
  }
}
function write(store: StorageLike, key: string, value: unknown) {
  try {
    store.setItem(key, JSON.stringify(value));
  } catch {
    /* Хранилище не блокирует переход или вход. */
  }
}
function saved(store: StorageLike, moment: number): Proofs {
  const raw = read<Proofs>(store, proofKey);
  const clean: Proofs = {};
  for (const name of ["first", "last"] as const) {
    const proof = raw?.[name];
    if (
      proof &&
      validProof(proof.token) &&
      Number.isSafeInteger(proof.expiresAt) &&
      proof.expiresAt > moment &&
      proof.expiresAt <= moment + lifetime
    )
      clean[name] = proof;
  }
  write(store, proofKey, clean);
  return clean;
}
export function recordCampaignVisit(
  response: CampaignVisit,
  store: StorageLike,
  moment = Date.now(),
) {
  const expiration = Date.parse(response.expires_at);
  if (
    !validProof(response.visit_token) ||
    !Number.isFinite(expiration) ||
    expiration <= moment ||
    !Number.isFinite(response.max_age_seconds) ||
    response.max_age_seconds <= 0
  )
    return;
  const state = saved(store, moment);
  const proof = {
    token: response.visit_token,
    expiresAt: Math.min(
      expiration,
      moment + Math.min(response.max_age_seconds * 1000, lifetime),
    ),
  };
  state.first ??= proof;
  state.last = state.last?.token === proof.token ? state.last : proof;
  write(store, proofKey, state);
}
export function marketingVisitor(
  store: StorageLike,
  moment = Date.now(),
  create: () => string = () => crypto.randomUUID(),
) {
  const value = read<{ id: string; expiresAt: number }>(store, visitorKey);
  if (
    value &&
    /^[A-Za-z0-9_.:-]{16,128}$/.test(value.id) &&
    Number.isSafeInteger(value.expiresAt) &&
    value.expiresAt > moment &&
    value.expiresAt <= moment + lifetime
  )
    return value.id;
  const id = create();
  write(store, visitorKey, { id, expiresAt: moment + lifetime });
  return id;
}
export function captureCampaignProofs(
  href: string,
  store: StorageLike,
  moment = Date.now(),
): string[] {
  const state = saved(store, moment);
  try {
    const url = new URL(href);
    if (url.search.length > 8192) return [];
    for (const name of ["first", "last"] as const) {
      const tokens = url.searchParams.getAll(`utm_${name}`);
      const dates = url.searchParams.getAll(`utm_${name}_expires`);
      const commonDates = url.searchParams.getAll("utm_expires");
      if (!tokens.length) continue;
      if (
        tokens.length !== 1 ||
        !validProof(tokens[0]) ||
        dates.length > 1 ||
        commonDates.length > 1
      )
        return [];
      const incomingDate = dates[0] ?? commonDates[0];
      const expiration =
        incomingDate === undefined
          ? moment + lifetime
          : /^[0-9]{1,16}$/.test(incomingDate)
            ? Number(incomingDate)
            : NaN;
      if (
        !Number.isSafeInteger(expiration) ||
        expiration <= moment ||
        expiration > moment + lifetime
      )
        continue;
      if (state[name]?.token !== tokens[0])
        state[name] = { token: tokens[0], expiresAt: expiration };
    }
  } catch {
    return [];
  }
  write(store, proofKey, state);
  return [
    ...new Set([state.first?.token, state.last?.token].filter(validProof)),
  ];
}
export function campaignHref(
  destination: string,
  store: StorageLike,
  moment = Date.now(),
) {
  if (!destination) return "";
  try {
    const url = new URL(
      destination,
      typeof location === "undefined" ? "https://localhost/" : location.href,
    );
    if (
      !/^https?:$/.test(url.protocol) ||
      url.username ||
      url.password ||
      destination.startsWith("//")
    )
      return "";
    const state = saved(store, moment);
    for (const name of ["first", "last"] as const) {
      if (state[name]) {
        url.searchParams.set(`utm_${name}`, state[name].token);
        url.searchParams.set(
          `utm_${name}_expires`,
          String(state[name].expiresAt),
        );
      }
    }
    return destination.startsWith("/")
      ? url.pathname + url.search + url.hash
      : url.href;
  } catch {
    return "";
  }
}
const ephemeral = new Map<string, string>();
export function campaignStorage(): StorageLike {
  try {
    return localStorage;
  } catch {
    return {
      getItem: (k) => ephemeral.get(k) ?? null,
      setItem: (k, v) => {
        ephemeral.set(k, v);
      },
      removeItem: (k) => {
        ephemeral.delete(k);
      },
    };
  }
}
export function clearCampaignQuery() {
  const url = new URL(location.href);
  for (const name of [
    "utm_id",
    "utm_first",
    "utm_last",
    "utm_first_expires",
    "utm_last_expires",
    "utm_expires",
  ])
    url.searchParams.delete(name);
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}
