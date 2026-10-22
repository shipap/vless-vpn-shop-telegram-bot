import { useEffect, useRef, useState } from "react";
import { api, useApi } from "@api/client";
import type { Subscription } from "@api/types";
import { Button, Dialog, Empty, ErrorState, Loading } from "@ui/components";
import { date, safeUrl } from "@ui/content";

type Device = {
  id: string;
  name: string;
  platform: string | null;
  generation: number;
  revoked: boolean;
  observed_at: string | null;
};
type Inventory = {
  subscription_id: string;
  source: "remnawave" | "local" | "unavailable";
  status: string | null;
  observed_at: string | null;
  error: string | null;
  traffic_bytes: string | null;
  traffic_limit_bytes: string | null;
  devices: Device[];
  paid_expires_at: string | null;
};
type Action = {
  devices: { id: string; generation: number }[];
  baseline: string | null;
  key: string;
} & ({ kind: "revoke"; operationMode: "single" | "bulk" } | { kind: "rotate" });
type Pending = {
  kind: Action["kind"];
  review: boolean;
  baseline: string | null;
  devices: { id: string; generation: number }[];
  previousUrl: string | undefined;
};

function traffic(value: string | null | undefined) {
  if (value === null || value === undefined || !/^\d+$/.test(value))
    return null;
  const bytes = BigInt(value);
  for (const [divisor, unit] of [
    [1073741824n, "ГБ"],
    [1048576n, "МБ"],
    [1024n, "КБ"],
  ] as const) {
    if (bytes >= divisor) {
      const whole = bytes / divisor;
      const part = ((bytes % divisor) * 100n) / divisor;
      return `${whole.toLocaleString("ru-RU")}${part ? `,${part.toString().padStart(2, "0")}` : ""} ${unit}`;
    }
  }
  return `${bytes.toLocaleString("ru-RU")} Б`;
}
const panelStatus = (value: string | null) =>
  ({
    ACTIVE: "Активна",
    DISABLED: "Отключена",
    EXPIRED: "Истекла",
    LIMITED: "Достигнут лимит",
  })[value?.toUpperCase() ?? ""] || "Пока неизвестно";

export function CustomerDevices({
  subscription,
  blocked,
  onAdd,
  adding = false,
  onConnect,
  onRefresh,
  onRotate,
}: {
  subscription: Subscription;
  blocked: boolean;
  onAdd: () => void;
  adding?: boolean;
  onConnect: () => void;
  onRefresh: () => void;
  onRotate: () => void;
}) {
  const path = `/subscriptions/${encodeURIComponent(subscription.id)}/inventory`;
  const inventory = useApi<Inventory>(`${path}?refresh=true`);
  const [action, setAction] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [completed, setCompleted] = useState<string | null>(null);
  const working = useRef(false);
  const alive = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const data = inventory.data;
  const rotating =
    Boolean(subscription.credential_rotation_state) ||
    pending?.kind === "rotate";
  const devices = (data?.devices ?? []).filter((device) => !device.revoked);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
  }, []);
  const generationChanged = Boolean(
    pending?.kind === "revoke" &&
    pending.devices.some((expected) =>
      data?.devices.some(
        (actual) =>
          actual.id === expected.id &&
          actual.generation !== expected.generation,
      ),
    ),
  );
  useEffect(() => {
    if (!pending || pending.review) return;
    if (pending.kind === "revoke") {
      const baseline = Date.parse(pending.baseline ?? "");
      const observed = Date.parse(data?.observed_at ?? "");
      if (
        data?.subscription_id === subscription.id &&
        data.source === "remnawave" &&
        !data.error &&
        Number.isFinite(baseline) &&
        observed > baseline &&
        pending.devices.length &&
        pending.devices.every((expected) =>
          data.devices.some(
            (actual) =>
              actual.id === expected.id &&
              actual.generation === expected.generation &&
              actual.revoked,
          ),
        )
      ) {
        setPending(null);
        setCompleted("Отвязка подтверждена панелью.");
      }
    } else {
      const nextUrl = safeUrl(subscription.connection_url);
      if (
        !subscription.credential_rotation_state &&
        nextUrl &&
        nextUrl !== pending.previousUrl
      ) {
        setPending(null);
        setCompleted("Новая ссылка подтверждена сервером.");
      }
    }
  }, [
    pending,
    data,
    subscription.id,
    subscription.credential_rotation_state,
    subscription.connection_url,
  ]);
  useEffect(() => {
    if (!pending) return;
    const deadline = Date.now() + 180000;
    const timer = window.setInterval(() => {
      if (Date.now() >= deadline) {
        window.clearInterval(timer);
        return;
      }
      if (document.visibilityState === "visible") {
        inventory.reload();
        onRefresh();
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [pending, inventory.reload, onRefresh]);
  async function refresh() {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<Inventory>(`${path}/refresh`, {
        method: "POST",
        signal: request.signal,
      });
      if (alive.current && !request.signal.aborted) {
        inventory.setData(result);
        onRefresh();
      }
    } catch (failure) {
      if (alive.current && !request.signal.aborted) setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function confirm() {
    if (!action || working.current) return;
    working.current = true;
    setBusy(true);
    setError(null);
    const request = new AbortController();
    controller.current = request;
    try {
      const result = await api<{
        status:
          "review_required" | "revoke_pending" | "revoked" | "rotate_pending";
      }>(
        `/subscriptions/${encodeURIComponent(subscription.id)}/${action.kind === "revoke" ? "devices/revoke-bulk" : "credentials/rotate"}`,
        {
          method: "POST",
          body:
            action.kind === "revoke"
              ? {
                  device_ids: action.devices.map((device) => device.id),
                  expected_generations: Object.fromEntries(
                    action.devices.map((device) => [
                      device.id,
                      device.generation,
                    ]),
                  ),
                  idempotency_key: action.key,
                  confirmed: true,
                  operation_mode: action.operationMode,
                }
              : {
                  idempotency_key: action.key,
                  confirmed: true,
                  revoke_only_passwords: false,
                },
          signal: request.signal,
        },
      );
      if (!alive.current || request.signal.aborted) return;
      setCompleted(null);
      if (action.kind === "revoke" && result.status === "revoked") {
        setPending(null);
        setCompleted("Отвязка подтверждена сервером.");
      } else {
        if (
          (action.kind === "rotate" && result.status !== "rotate_pending") ||
          (action.kind === "revoke" &&
            !["revoke_pending", "review_required"].includes(result.status))
        )
          throw new Error(
            "Состояние операции не подтверждено. Обновите сведения и обратитесь в поддержку.",
          );
        setPending({
          kind: action.kind,
          review: result.status === "review_required",
          baseline: action.baseline,
          devices: action.devices.map((device) => ({ ...device })),
          previousUrl: safeUrl(subscription.connection_url),
        });
      }
      if (action.kind === "rotate") onRotate();
      setAction(null);
      inventory.reload();
      onRefresh();
    } catch (failure) {
      if (alive.current && !request.signal.aborted) setError(failure);
    } finally {
      working.current = false;
      if (alive.current) setBusy(false);
    }
  }
  return (
    <section className="card stack customer-inventory">
      <div className="card-head">
        <div>
          <h2>Устройства и подключение</h2>
          <p className="fine-print">
            {subscription.devices_limit === null ? (
              "Лимит не задан в панели"
            ) : (
              <>Включено мест: {subscription.devices_limit}</>
            )}
          </p>
        </div>
        <Button tone="secondary" busy={adding} onClick={onAdd}>
          Добавить устройства
        </Button>
      </div>
      <p className="fine-print">
        Оплаченный доступ: {date(subscription.expires_at)}. Наблюдение панели не
        изменяет оплаченный срок.
      </p>
      {Boolean(inventory.error) && (
        <ErrorState error={inventory.error} onRetry={inventory.reload} />
      )}
      {!data && !inventory.error && <Loading />}
      {data && (
        <>
          <dl className="definition">
            <dt>Состояние панели</dt>
            <dd>{panelStatus(data.status)}</dd>
            <dt>Последняя проверка · МСК</dt>
            <dd>{date(data.observed_at, true)}</dd>
            <dt>Трафик</dt>
            <dd>
              {traffic(data.traffic_bytes) ?? "Трафик пока неизвестен"}
              {traffic(data.traffic_limit_bytes)
                ? ` из ${traffic(data.traffic_limit_bytes)}`
                : ""}
            </dd>
          </dl>
          {(data.error || data.source !== "remnawave") && (
            <p className="notice warning">
              Актуальные данные панели недоступны. Показаны сохранённые
              сведения; оплаченный доступ сохраняется.
            </p>
          )}
          {!devices.length ? (
            <Empty
              title={
                data.error
                  ? "Список устройств пока недоступен"
                  : "Подключённых устройств нет"
              }
            >
              <Button tone="secondary" onClick={onConnect}>
                Как подключить устройство
              </Button>
            </Empty>
          ) : (
            <div className="inventory-device-list">
              {devices.map((device) => (
                <article key={device.id} className="inventory-device">
                  <div>
                    <h3>{device.name || "Устройство"}</h3>
                    <p className="fine-print">
                      {device.platform || "Платформа не указана"} ·{" "}
                      {date(device.observed_at, true)} · МСК
                    </p>
                  </div>
                  <Button
                    tone="secondary"
                    disabled={blocked || Boolean(pending) || rotating}
                    onClick={() => {
                      setError(null);
                      setCompleted(null);
                      setAction({
                        kind: "revoke",
                        operationMode: "single",
                        devices: [
                          { id: device.id, generation: device.generation },
                        ],
                        baseline: data?.observed_at ?? null,
                        key: crypto.randomUUID(),
                      });
                    }}
                  >
                    Отвязать
                  </Button>
                </article>
              ))}
            </div>
          )}
        </>
      )}
      {(pending || rotating) && (
        <p className="notice" role="status">
          {["review", "blocked"].includes(
            subscription.credential_rotation_state || "",
          )
            ? "Перевыпуск ссылки требует сверки. Новое подключение появится после подтверждения панели."
            : pending?.review
              ? "Отвязка требует сверки. Повторная отправка недоступна до подтверждения результата."
              : generationChanged
                ? "Поколение устройства изменилось. Результат прежней отвязки требует сверки; обновите сведения или обратитесь в поддержку."
                : pending?.kind === "revoke"
                  ? "Отвязка поставлена в очередь. Результат будет показан после проверки панели."
                  : "Перевыпуск ссылки поставлен в очередь. Новое подключение появится после проверки панели."}
        </p>
      )}
      {completed && (
        <p className="notice" role="status">
          {completed}
        </p>
      )}
      <div className="inventory-actions">
        <Button
          tone="secondary"
          busy={busy && !action}
          onClick={() => {
            void refresh();
          }}
        >
          Проверить панель
        </Button>
        <Button
          tone="danger"
          disabled={
            blocked ||
            !devices.length ||
            devices.length > 100 ||
            Boolean(pending) ||
            rotating
          }
          onClick={() => {
            setError(null);
            setCompleted(null);
            setAction({
              kind: "revoke",
              operationMode: "bulk",
              devices: devices.map(({ id, generation }) => ({
                id,
                generation,
              })),
              baseline: data?.observed_at ?? null,
              key: crypto.randomUUID(),
            });
          }}
        >
          Отвязать все устройства
        </Button>
        <Button
          tone="secondary"
          disabled={Boolean(pending) || rotating}
          onClick={() => {
            setError(null);
            setCompleted(null);
            setAction({
              kind: "rotate",
              devices: [],
              baseline: data?.observed_at ?? null,
              key: crypto.randomUUID(),
            });
          }}
        >
          Перевыпустить ссылку
        </Button>
      </div>
      {blocked && (
        <p className="notice warning">
          Отвязка устройств временно ограничена. Покупки, продление и оплаченный
          доступ остаются доступны.
        </p>
      )}
      {Boolean(error) && !action && <ErrorState error={error} />}
      {action && (
        <Dialog
          title={
            action.kind === "revoke"
              ? "Подтверждение отвязки"
              : "Перевыпуск ссылки"
          }
          onClose={() => setAction(null)}
        >
          <div className="stack">
            <p>
              {action.kind === "revoke"
                ? `${action.devices.length} устройства потеряют текущее подключение. Счётчик отвязок учитывает каждое устройство отдельно. Оплаченный срок сохраняется.`
                : "Старая ссылка и подключения на устройствах перестанут работать. Новую ссылку потребуется добавить в приложение заново. Тариф и оплаченный срок сохраняются."}
            </p>
            {Boolean(error) && <ErrorState error={error} />}
            <Button
              tone="danger"
              busy={busy}
              onClick={() => {
                void confirm();
              }}
            >
              {action.kind === "revoke"
                ? "Подтвердить отвязку"
                : "Подтвердить перевыпуск"}
            </Button>
          </div>
        </Dialog>
      )}
    </section>
  );
}
