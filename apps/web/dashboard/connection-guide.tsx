import { useId, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { useApi } from "@api/client";
import type { components } from "@api/schema";
import { Button } from "@ui/components";
import { safeUrl } from "@ui/content";

type ConnectionInstructions =
  components["schemas"]["ConnectionInstructionsDto"];

export function ConnectionGuide({
  subscriptionId,
  fallback,
}: {
  subscriptionId: string;
  fallback?: string;
}) {
  const titleId = useId();
  const resource = useApi<ConnectionInstructions>(
    `/subscriptions/${encodeURIComponent(subscriptionId)}/connection-instructions`,
  );
  const [platformId, setPlatformId] = useState("");
  const [appIndex, setAppIndex] = useState(0);
  const platforms =
    resource.data?.source === "remnawave"
      ? resource.data.platforms.filter((platform) => platform.apps.length > 0)
      : [];
  const platform =
    platforms.find((entry) => entry.id === platformId) || platforms[0];
  const app = platform?.apps[appIndex] || platform?.apps[0];

  return (
    <section className="connection-guide" aria-labelledby={titleId}>
      <h3 id={titleId}>Инструкция подключения</h3>
      <div className="connection-guide-status" aria-live="polite">
        {resource.loading && (
          <p className="fine-print">Загружаем инструкции…</p>
        )}
        {Boolean(resource.error) && (
          <div className="connection-guide-retry">
            <p className="fine-print">
              Подробная инструкция временно недоступна.
            </p>
            <Button type="button" tone="ghost" onClick={resource.reload}>
              Обновить инструкцию
            </Button>
          </div>
        )}
      </div>
      {platform && app ? (
        <>
          <div
            className="connection-guide-platforms"
            role="group"
            aria-label="Устройство"
          >
            {platforms.map((entry) => (
              <Button
                key={entry.id}
                type="button"
                tone={platform.id === entry.id ? "primary" : "secondary"}
                aria-pressed={platform.id === entry.id}
                onClick={() => {
                  setPlatformId(entry.id);
                  setAppIndex(0);
                }}
              >
                {entry.name}
              </Button>
            ))}
          </div>
          {platform.apps.length > 1 ? (
            <label>
              Приложение
              <select
                value={appIndex}
                onChange={(event) => setAppIndex(Number(event.target.value))}
              >
                {platform.apps.map((entry, index) => (
                  <option key={index} value={index}>
                    {entry.name}
                  </option>
                ))}
              </select>
            </label>
          ) : (
            <p className="small-copy">
              Приложение: <strong>{app.name}</strong>
            </p>
          )}
          <ol className="connection-guide-steps">
            {app.steps.map((step, index) => (
              <li key={`${platform.id}:${app.name}:${index}`}>
                <h4>{step.title}</h4>
                {step.description && <p>{step.description}</p>}
                <div className="connection-guide-links">
                  {step.links.map((link, linkIndex) => {
                    const url = safeUrl(link.url);
                    if (!url?.startsWith("https://")) return null;
                    return (
                      <a
                        key={linkIndex}
                        className="text-link"
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        {link.text}
                        <ArrowUpRight size={15} aria-hidden="true" />
                      </a>
                    );
                  })}
                </div>
              </li>
            ))}
          </ol>
        </>
      ) : fallback ? (
        <p className="connection-instructions">{fallback}</p>
      ) : null}
    </section>
  );
}
