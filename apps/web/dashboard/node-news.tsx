import { useEffect, useState } from "react";
import { ApiError, useApi } from "@api/client";
import { Button, Empty, ErrorState, Loading } from "@ui/components";
import { date } from "@ui/content";

type News = {
  items: {
    id: string;
    title: string;
    state: "outage" | "recovered";
    occurred_at: string;
  }[];
  next_cursor: string | null;
};

export function NodeNews({ hasSubscription }: { hasSubscription?: boolean }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [historyUpdated, setHistoryUpdated] = useState(false);
  const cursor = cursors.at(-1) ?? null;
  return (
    <NodeNewsPage
      key={cursor ?? ""}
      cursor={cursor}
      hasSubscription={hasSubscription}
      historyUpdated={historyUpdated}
      onBack={() =>
        setCursors((current) =>
          current.at(-1) === cursor && current.length > 1
            ? current.slice(0, -1)
            : current,
        )
      }
      onEarlier={(nextCursor) =>
        setCursors((current) =>
          current.at(-1) === cursor && !current.includes(nextCursor)
            ? [...current, nextCursor]
            : current,
        )
      }
      onHistoryExpired={() => {
        setCursors([null]);
        setHistoryUpdated(true);
      }}
    />
  );
}

function NodeNewsPage({
  cursor,
  hasSubscription,
  historyUpdated,
  onBack,
  onEarlier,
  onHistoryExpired,
}: {
  cursor: string | null;
  hasSubscription?: boolean;
  historyUpdated: boolean;
  onBack: () => void;
  onEarlier: (cursor: string) => void;
  onHistoryExpired: () => void;
}) {
  const resource = useApi<News>(
    `/node-news?limit=20${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`,
  );
  const historyExpired =
    !!cursor &&
    !resource.loading &&
    resource.error instanceof ApiError &&
    resource.error.status === 404;
  useEffect(() => {
    if (historyExpired) onHistoryExpired();
  }, [historyExpired, onHistoryExpired]);
  const groups = new Map<string, News["items"]>();
  for (const item of resource.data?.items ?? []) {
    const events = groups.get(item.title) ?? [];
    events.push(item);
    groups.set(item.title, events);
  }
  const stateLabel = (state: News["items"][number]["state"]) =>
    state === "recovered" ? "Работа восстановлена" : "Перебои в работе";
  return (
    <section className="card stack node-news">
      <div className="card-head">
        <h2>Новости серверов</h2>
      </div>
      <p>
        Перебои и восстановление работы серверов вашего тарифа. Время указано по
        Москве.
      </p>
      {historyUpdated && !cursor && (
        <p className="fine-print" role="status">
          История обновилась. Показаны последние события.
        </p>
      )}
      {cursor && (
        <p className="fine-print">
          Более ранние события. Состояние указано на момент последнего события
          этой страницы.
        </p>
      )}
      {historyExpired ? (
        <Loading />
      ) : resource.error ? (
        <ErrorState error={resource.error} onRetry={resource.reload} />
      ) : !resource.data ? (
        <Loading />
      ) : !resource.data.items.length ? (
        <Empty
          title={
            hasSubscription === false
              ? "Для получения информации о серверах нужна подписка"
              : "За последние 24 часа событий нет"
          }
        />
      ) : (
        <div className="node-news-groups">
          {Array.from(groups, ([title, events]) => {
            const latest = events[0];
            return (
              <article className="node-news-server" key={title}>
                <h3>{title}</h3>
                <div className="node-news-latest">
                  <span
                    className={`badge ${latest.state === "recovered" ? "good" : "bad"}`}
                  >
                    {stateLabel(latest.state)}
                  </span>
                  <time className="fine-print" dateTime={latest.occurred_at}>
                    {date(latest.occurred_at, true)} · МСК
                  </time>
                </div>
                <details className="node-news-history">
                  <summary>История событий · {events.length}</summary>
                  <ol>
                    {events.map((item) => (
                      <li key={item.id}>
                        <time dateTime={item.occurred_at}>
                          {date(item.occurred_at, true)} · МСК
                        </time>
                        <span>{stateLabel(item.state)}</span>
                      </li>
                    ))}
                  </ol>
                </details>
              </article>
            );
          })}
        </div>
      )}
      <div className="dialog-actions">
        {cursor && (
          <Button
            tone="secondary"
            disabled={resource.loading || historyExpired}
            onClick={onBack}
          >
            Назад
          </Button>
        )}
        {!resource.error && resource.data?.next_cursor && (
          <Button
            tone="secondary"
            disabled={resource.loading}
            onClick={() => onEarlier(resource.data!.next_cursor!)}
          >
            Более ранние события
          </Button>
        )}
      </div>
    </section>
  );
}
