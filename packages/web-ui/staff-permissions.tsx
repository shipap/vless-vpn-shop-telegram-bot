import { createContext, useContext, type ReactNode } from "react";
import type { Session } from "@api/types";

const StaffContext = createContext<Session | null>(null);
export function staffRole(session: Session) {
  const role = session.staff_role;
  return role === "owner" || role === "admin" ? role : null;
}
export function hasCapability(session: Session, capability: string) {
  return Boolean(
    session.is_admin &&
    staffRole(session) &&
    session.capabilities?.includes(capability),
  );
}
export function StaffPermissions({
  session,
  children,
}: {
  session: Session;
  children: ReactNode;
}) {
  return (
    <StaffContext.Provider value={session}>{children}</StaffContext.Provider>
  );
}
export function useCapability(capability: string) {
  const session = useContext(StaffContext);
  return session ? hasCapability(session, capability) : false;
}
export const sectionCapability: Record<string, string> = {
  overview: "statistics_read",
  users: "users_read",
  catalog: "catalog_read",
  orders: "payments_read",
  referrals: "referrals_read",
  promos: "promos_write",
  messages: "messages_write",
  broadcasts: "broadcasts_write",
  campaigns: "campaigns_read",
  subscriptions: "subscriptions_manage",
  recurring: "recurring_manage",
  discounts: "settings_write",
  content: "content_write",
  security: "security_manage",
  operations: "operations_manage",
  legal: "settings_write",
  refunds: "refunds_manage",
  telegram: "settings_write",
  backups: "backups_manage",
  identities: "identity_review",
  nodes: "settings_write",
};
