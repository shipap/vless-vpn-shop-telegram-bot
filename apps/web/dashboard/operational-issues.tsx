import { Button, Empty } from "@ui/components";
import { statusLabel } from "@ui/ru";
import type { Mutation } from "./admin";

type Issue = {
  key: string;
  kind: string;
  reference: string;
  order_id?: string | null;
  state: string;
  age_seconds: number;
  reason: string;
  retry_kind?: string | null;
  can_acknowledge?: boolean;
  ack_token?: string | null;
  target: string;
};
export type IssueQueue = {
  items?: Issue[];
  signal_count?: number;
  truncated?: boolean;
};

export function OperationalIssues({
  value,
  canManage,
  mutate,
}: {
  value?: IssueQueue;
  canManage: boolean;
  mutate: (value: Mutation) => void;
}) {
  const items = value?.items ?? [];
  return (
    <section className="card stack">
      <div className="card-head">
        <h2>Проблемные операции</h2>
        <span className="fine-print">Сигналов: {value?.signal_count ?? 0}</span>
      </div>
      <p className="small-copy">
        Задержки от 15 минут и операции, требующие проверки. Повтор выдачи
        восстанавливает доступ и не списывает деньги. Неизвестный результат
        оплаты сначала требует сверки.
      </p>
      {items.length === 0 ? (
        <Empty title="Проблемных операций нет" />
      ) : (
        <div className="table-scroll">
          <table className="queue-health-table">
            <thead>
              <tr>
                {[
                  "Операция",
                  "Состояние",
                  "Ожидание",
                  "Причина",
                  "Действия",
                ].map((label) => (
                  <th key={label}>{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.key}>
                  <td>
                    <code>{item.order_id || item.reference}</code>
                  </td>
                  <td>{statusLabel(item.state)}</td>
                  <td>{Math.floor(item.age_seconds / 60)} мин</td>
                  <td>{item.reason}</td>
                  <td>
                    <div className="button-row">
                      <a
                        className="button secondary small"
                        href={
                          item.target === "/admin#orders"
                            ? "/admin#orders"
                            : "/admin#operations"
                        }
                      >
                        Открыть раздел
                      </a>
                      {canManage &&
                        item.can_acknowledge &&
                        item.ack_token &&
                        (item.kind !== "delivery" ||
                          item.state === "review") && (
                          <Button
                            className="small"
                            tone="secondary"
                            onClick={() =>
                              mutate({
                                title:
                                  item.kind === "delivery"
                                    ? "Отметить проблему доставки как решённую?"
                                    : "Отметить операцию как разобранную?",
                                confirmLabel:
                                  item.kind === "delivery"
                                    ? "Решено"
                                    : "Разобрано",
                                description:
                                  item.kind === "delivery"
                                    ? "Текущий сигнал исчезнет из очереди и предупреждений. История и неизвестный результат доставки сохранятся. Повторной отправки не будет; изменившаяся проблема появится снова."
                                    : "Операция исчезнет из очереди и предупреждений. Результат оплаты останется неизвестным, сверка продолжится. Новый счёт создан не будет.",
                                path: `/admin/operations/${item.kind === "delivery" ? "deliveries" : "provider"}/${encodeURIComponent(item.reference)}/acknowledge`,
                                method: "POST",
                                body: { ack_token: item.ack_token },
                              })
                            }
                          >
                            {item.kind === "delivery" ? "Решено" : "Разобрано"}
                          </Button>
                        )}
                      {canManage &&
                        item.retry_kind === "provisioning" &&
                        item.order_id && (
                          <Button
                            className="small"
                            tone="secondary"
                            onClick={() =>
                              mutate({
                                title: "Повторить выдачу доступа?",
                                description:
                                  "Восстановим выдачу по уже принятой оплате. Нового списания не будет.",
                                path: `/admin/orders/${encodeURIComponent(item.order_id!)}/retry-provisioning`,
                                method: "POST",
                                body: {},
                              })
                            }
                          >
                            Повторить выдачу
                          </Button>
                        )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {value?.truncated && (
        <p className="fine-print">
          Показаны самые старые сигналы. Остальные операции доступны в разделах
          «Заказы» и «Операции».
        </p>
      )}
    </section>
  );
}
