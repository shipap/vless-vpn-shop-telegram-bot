import { useState } from "react";
import { useApi } from "@api/client";
import { Button, ErrorState, Loading } from "@ui/components";
import { AdminValue } from "@ui/admin-values";
import { money } from "@ui/content";
import type { Mutation } from "./admin";
import { useCapability } from "@ui/staff-permissions";
import { useInstance } from "@ui/instance";
import { instanceTerminology } from "@ui/terminology";

type Row = Record<string, unknown>;
export function AdminUserSummary({
  data,
  id,
  mutate,
  onClose,
}: {
  data: Row;
  id: string;
  mutate: (value: Mutation) => void;
  onClose: () => void;
}) {
  const canModerate = useCapability("device_unlinks_manage");
  const terms = instanceTerminology(useInstance());
  const [invitees, setInvitees] = useState(false);
  const referral = (data.referral || {}) as Row;
  const moderation = (data.moderation || {}) as Row;
  const act = (action: string, title: string) => {
    onClose();
    mutate({
      title,
      description: `Изменяется только возможность отвязывать устройства. ${terms.service_label}, покупки и продление остаются доступны.`,
      path: `/admin/users/${encodeURIComponent(id)}/device-moderation`,
      method: "POST",
      requireReason: false,
      body: {
        action,
        reason: `Подтверждение действия ${action} в карточке пользователя ${id} через кабинет`,
        confirmed: true,
        idempotency_key: crypto.randomUUID(),
      },
    });
  };
  return (
    <section className="stack user-summary">
      <div className="summary-grid">
        <details className="card admin-disclosure">
          <summary>Подписки и автооплата</summary>
          <div className="admin-disclosure-body">
            <p>
              Активных подписок: {String(data.active_subscriptions_total ?? 0)}
            </p>
            <p>
              Действующих соглашений:{" "}
              {String(data.active_agreements_total ?? 0)}
            </p>
          </div>
        </details>
        <details className="card admin-disclosure">
          <summary>Реферальная система</summary>
          <div className="admin-disclosure-body">
            <p>Баланс: {money(String(referral.balance ?? "0"), "RUB")}</p>
            <p>
              Заработано: {money(String(referral.earned_total ?? "0"), "RUB")}
            </p>
            <p>Приглашено: {String(referral.invited_total ?? 0)}</p>
            <h4>Кто пригласил</h4>
            {data.inviter ? (
              <AdminValue value={data.inviter} />
            ) : (
              <p>Без приглашения</p>
            )}
            <Button tone="secondary" onClick={() => setInvitees(!invitees)}>
              {invitees ? "Скрыть приглашённых" : "Кого пригласил"}
            </Button>
          </div>
        </details>
      </div>
      {invitees && <Invitees id={id} />}
      <details className="card admin-disclosure" open>
        <summary>Отвязка устройств</summary>
        <div className="admin-disclosure-body">
          <p>
            За месяц (МСК): {String(moderation.unlink_month ?? 0)} · Всего:{" "}
            {String(moderation.unlink_total ?? 0)}
          </p>
          <p>
            По одному: за месяц{" "}
            {String(moderation.unlink_individual_month ?? 0)} · Всего{" "}
            {String(moderation.unlink_individual_total ?? 0)}
          </p>
          <p>
            Все устройства: за месяц {String(moderation.unlink_bulk_month ?? 0)}{" "}
            · Всего {String(moderation.unlink_bulk_total ?? 0)}
          </p>
          {Boolean(
            moderation.unlink_unknown_total || moderation.unlink_unknown_month,
          ) && (
            <p>
              Без сохранённого способа: за месяц{" "}
              {String(moderation.unlink_unknown_month ?? 0)} · Всего{" "}
              {String(moderation.unlink_unknown_total ?? 0)}
            </p>
          )}
          <p>
            Предупреждений: {String(moderation.current_warnings ?? 0)}/2 · Всего
            выдано: {String(moderation.total_warnings ?? 0)}
          </p>
          <p>
            {moderation.unlink_blocked
              ? "Отвязка заблокирована"
              : "Отвязка разрешена"}
          </p>
          <p className="small-copy">
            Два предупреждения блокируют только отвязку устройств и обнуляют
            текущие предупреждения.
          </p>
          {canModerate && data.can_moderate === true && (
            <>
              <div className="button-row">
                <Button
                  tone="secondary"
                  onClick={() => act("warn", "Выдать предупреждение")}
                >
                  Предупреждение
                </Button>
                {Number(moderation.current_warnings ?? 0) > 0 && (
                  <Button
                    tone="secondary"
                    onClick={() =>
                      act("remove_warning", "Снять одно предупреждение")
                    }
                  >
                    Снять предупреждение
                  </Button>
                )}
                <Button
                  tone="secondary"
                  onClick={() =>
                    act(
                      moderation.unlink_blocked ? "unblock" : "block",
                      moderation.unlink_blocked
                        ? "Разрешить отвязку"
                        : "Заблокировать отвязку",
                    )
                  }
                >
                  {moderation.unlink_blocked
                    ? "Разрешить отвязку"
                    : "Заблокировать отвязку"}
                </Button>
              </div>
            </>
          )}
        </div>
      </details>
    </section>
  );
}
function Invitees({ id }: { id: string }) {
  const [page, setPage] = useState(1);
  const resource = useApi<{ items: Row[]; total: number }>(
    `/admin/users/${encodeURIComponent(id)}/invitees?page=${page}&page_size=10`,
  );
  return (
    <section className="card stack">
      <h3>Приглашённые пользователи</h3>
      {resource.loading ? (
        <Loading />
      ) : resource.error ? (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      ) : (
        <>
          <AdminValue value={resource.data?.items || []} />
          <div className="button-row">
            <Button
              tone="secondary"
              disabled={page === 1}
              onClick={() => setPage(page - 1)}
            >
              Назад
            </Button>
            <span>Страница {page}</span>
            <Button
              tone="secondary"
              disabled={page * 10 >= (resource.data?.total || 0)}
              onClick={() => setPage(page + 1)}
            >
              Далее
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
