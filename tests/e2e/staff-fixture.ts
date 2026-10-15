// Явные серверные права синтетического владельца; старый is_admin не выдаёт доступ.
export const ownerStaffFixture = {
  staff_role: "owner" as const,
  capabilities: [
    "statistics_read",
    "users_read",
    "catalog_read",
    "payments_read",
    "referrals_read",
    "promos_write",
    "messages_write",
    "broadcasts_write",
    "campaigns_read",
    "campaigns_write",
    "catalog_write",
    "settings_write",
    "security_manage",
    "wallet_write",
    "subscriptions_manage",
    "identity_review",
    "backups_manage",
    "operations_manage",
    "staff_manage",
    "content_write",
    "refunds_manage",
    "recurring_manage",
  ],
};

export const adminStaffFixture = {
  staff_role: "admin" as const,
  capabilities: [
    "statistics_read",
    "users_read",
    "catalog_read",
    "payments_read",
    "referrals_read",
    "promos_write",
    "messages_write",
    "broadcasts_write",
    "campaigns_read",
    "campaigns_write",
  ],
};
