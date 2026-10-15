import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AdminUserSummary } from "../../apps/web/dashboard/admin-user-summary";
import { StaffPermissions } from "./staff-permissions";
import type { Session } from "@api/types";

it("shows referral and unlink counters while hiding moderation from other admins", () => {
  const html = renderToStaticMarkup(
    <AdminUserSummary
      id="9007199254740993123"
      data={{
        can_moderate: false,
        referral: { balance: "15.50", earned_total: "30.00", invited_total: 2 },
        moderation: {
          current_warnings: 0,
          total_warnings: 2,
          unlink_month: 3,
          unlink_total: 7,
          unlink_blocked: true,
        },
        active_subscriptions_total: 1,
      }}
      mutate={() => undefined}
      onClose={() => undefined}
    />,
  );
  expect(html).toContain("15,50");
  expect(html).toContain("Кого пригласил");
  expect(html).toContain("Отвязка заблокирована");
  expect(html).not.toContain("Причина действия");
});
it("owner controls retain only unlink scope without a manual reason prompt", () => {
  const session = {
    account: {
      id: "1",
      email: "synthetic-owner@example.test",
      email_verified: true,
      telegram_id: "1",
      unlink_blocked: false,
    },
    csrf_token: "synthetic",
    is_admin: true,
    mfa_verified: true,
    staff_role: "owner",
    capabilities: ["device_unlinks_manage"],
  } as Session;
  const html = renderToStaticMarkup(
    <StaffPermissions session={session}>
      <AdminUserSummary
        id="1"
        data={{ can_moderate: true, moderation: { unlink_blocked: false } }}
        mutate={() => undefined}
        onClose={() => undefined}
      />
    </StaffPermissions>,
  );
  expect(html).not.toContain("Причина действия");
  expect(html).toContain("Заблокировать отвязку");
  expect(html).not.toContain("disabled");
  expect(html).not.toContain("Отключить VPN");
});

it("verified admin with unlink capability sees separate operation counts and removal without security access", () => {
  const session = {
    is_admin: true,
    staff_role: "admin",
    capabilities: ["device_unlinks_manage"],
  } as Session;
  const html = renderToStaticMarkup(
    <StaffPermissions session={session}>
      <AdminUserSummary
        id="1"
        data={{
          can_moderate: true,
          moderation: {
            current_warnings: 1,
            total_warnings: 4,
            unlink_individual_month: 2,
            unlink_individual_total: 8,
            unlink_bulk_month: 1,
            unlink_bulk_total: 3,
            unlink_unknown_month: 0,
            unlink_unknown_total: 2,
          },
        }}
        mutate={() => undefined}
        onClose={() => undefined}
      />
    </StaffPermissions>,
  );
  expect(html).toContain("1/2");
  expect(html).toContain("По одному");
  expect(html).toContain("Все устройства");
  expect(html).toContain("Без сохранённого способа");
  expect(html).toContain("Снять предупреждение");
});
