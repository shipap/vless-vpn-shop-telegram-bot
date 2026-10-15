import { analyticsCopy as copy } from "@ui/analytics-copy";
import { factRows } from "@ui/analytics";
import { money, date } from "@ui/content";
import { statusLabel, fieldLabel } from "@ui/ru";
import { AdminValue } from "@ui/admin-values";
type Row = Record<string, unknown>;
function StateBars({ title, rows }: { title: string; rows: Row[] | null }) {
  const max = Math.max(1, ...(rows || []).map((row) => Number(row.count) || 0));
  return (
    <section className="card">
      <h2>{title}</h2>
      {!rows?.length ? (
        <p className="muted">{rows === null ? copy.absent : copy.empty}</p>
      ) : (
        <ul className="fact-bars">
          {rows.map((row, index) => (
            <li key={index}>
              <div className="row">
                <span>{statusLabel(String(row.state))}</span>
                <strong>{String(row.count)}</strong>
              </div>
              <div className="fact-track" aria-hidden="true">
                <i style={{ width: `${(Number(row.count) / max) * 100}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
export function AnalyticsPanels({
  data,
  currency,
  timezone = "Europe/Moscow",
}: {
  data: Row | null;
  currency: string;
  timezone?: string;
}) {
  const sales = factRows(data, "sales_composition", currency);
  const max = Math.max(
    1,
    ...(sales || []).map((row) => Number(row.count) || 0),
  );
  const settlements = Array.isArray(data?.settlement_series)
    ? (data.settlement_series as Row[])
    : null;
  const services = factRows(data, "service_rows", currency);
  const serviceValue = data?.service_value as Row | undefined;
  const creditSpent = data?.credit_spent as Row | undefined;
  return (
    <>
      <section className="card stack">
        <h2>Выданные услуги и внутренние средства</h2>
        <div className="metrics-grid">
          <div className="metric">
            <span>Выдано услуг</span>
            <strong>
              {data?.service_count == null ? "—" : String(data.service_count)}
            </strong>
          </div>
          <div className="metric">
            <span>Стоимость услуг</span>
            <strong>
              {money(
                serviceValue?.[currency] == null
                  ? null
                  : String(serviceValue[currency]),
                currency,
              )}
            </strong>
          </div>
          <div className="metric">
            <span>Использовано денежных средств</span>
            <strong>
              {money(
                creditSpent?.[currency] == null
                  ? null
                  : String(creditSpent[currency]),
                currency,
              )}
            </strong>
          </div>
        </div>
        <p className="fine-print">
          Оплата внутренними средствами и бонусами входит в стоимость выданных
          услуг. Новым поступлением от платёжного сервиса она не считается.
          Показатели рассчитаны по применённым фильтрам.
        </p>
        <details className="chart-details">
          <summary>История выданных услуг</summary>
          {!services?.length ? (
            <p className="muted">
              {services === null ? copy.absent : copy.empty}
            </p>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {[
                      "order_id",
                      "date",
                      "product_id",
                      "service_value",
                      "bonus_amount",
                      "credit_amount",
                      "external_amount",
                      "currency",
                      "action",
                      "source",
                      "time_source",
                      "canonical_account_id",
                    ].map((key) => (
                      <th key={key}>{fieldLabel(key)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {services.map((row, index) => (
                    <tr key={index}>
                      {[
                        "order_id",
                        "date",
                        "product_id",
                        "service_value",
                        "bonus_amount",
                        "credit_amount",
                        "external_amount",
                        "currency",
                        "action",
                        "source",
                        "time_source",
                        "canonical_account_id",
                      ].map((key) => (
                        <td
                          key={key}
                          className={
                            ["order_id", "canonical_account_id"].includes(key)
                              ? "identifier-cell"
                              : undefined
                          }
                        >
                          {key === "date" ? (
                            date(String(row[key]), true, timezone)
                          ) : [
                              "service_value",
                              "bonus_amount",
                              "credit_amount",
                              "external_amount",
                            ].includes(key) ? (
                            money(
                              row[key] == null ? null : String(row[key]),
                              String(row.currency),
                            )
                          ) : (
                            <AdminValue value={row[key]} name={key} />
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </details>
      </section>
      <div className="metrics-grid">
        {[
          ["new_payers", copy.newPayers],
          ["returning_payers", copy.returningPayers],
          ["renewals", copy.renewals],
          ["bonus_spent", copy.bonus],
        ].map(([key, label]) => (
          <div className="metric" key={key}>
            <span>{label}</span>
            <strong>
              {data?.[key] == null
                ? "—"
                : key === "bonus_spent"
                  ? money(String(data[key]), currency)
                  : String(data[key])}
            </strong>
            <small>{data?.[key] == null ? copy.absent : copy.facts}</small>
          </div>
        ))}
      </div>
      <section className="card">
        <div className="card-head">
          <h2>{copy.sales}</h2>
        </div>
        {!sales?.length ? (
          <p className="muted">{sales === null ? copy.absent : copy.empty}</p>
        ) : (
          <>
            <ul className="fact-bars">
              {sales.map((row, index) => (
                <li key={index}>
                  <div className="row">
                    <span>
                      {String(row.label)} · {String(row.period_days)}{" "}
                      {copy.period.toLowerCase()}
                    </span>
                    <strong>{String(row.count)}</strong>
                  </div>
                  <div className="fact-track" aria-hidden="true">
                    <i
                      style={{ width: `${(Number(row.count) / max) * 100}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
            <details className="chart-details">
              <summary>{copy.details}</summary>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      {[
                        copy.productLabel,
                        copy.period,
                        copy.sourceLabel,
                        copy.action,
                        copy.devices,
                        copy.count,
                        copy.amount,
                        copy.service,
                      ].map((label) => (
                        <th key={label}>{label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {sales.map((row, index) => (
                      <tr key={index}>
                        <td>{String(row.label)}</td>
                        <td>{String(row.period_days)}</td>
                        <td>
                          {row.source === "web"
                            ? copy.web
                            : row.source === "telegram"
                              ? copy.telegram
                              : String(row.source)}
                        </td>
                        <td>{statusLabel(String(row.action))}</td>
                        <td>{String(row.devices)}</td>
                        <td>{String(row.count)}</td>
                        <td>{money(String(row.amount), currency)}</td>
                        <td>{money(String(row.service_value), currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </>
        )}
      </section>
      <div className="dashboard-bottom-grid">
        <MoneyBars
          title="Подтверждённые комиссии"
          rows={
            settlements?.filter((row) => row.fee_currency === currency) ?? null
          }
          field="fee"
          currency={currency}
        />
        <MoneyBars
          title="Чистые зачисления USDT"
          rows={settlements?.filter((row) => row.net_usdt != null) ?? null}
          field="net_usdt"
          currency="USDT"
        />
      </div>
      <div className="dashboard-bottom-grid">
        <StateBars
          title={copy.cycles}
          rows={factRows(data, "recurring_cycles")}
        />
        <StateBars
          title={copy.provisioning}
          rows={factRows(data, "provisioning")}
        />
      </div>
      <section className="card">
        <h2>Результат наблюдаемых автоциклов</h2>
        <div className="metrics-grid">
          {[
            ["recurring_due_count", "Наступившие циклы"],
            ["recurring_paid_count", "Оплаченные циклы"],
            ["recurring_attempt_count", "Попытки оплаты"],
            ["recurring_success_percent", "Успешных циклов, %"],
          ].map(([key, title]) => (
            <div className="metric" key={key}>
              <span>{title}</span>
              <strong>{data?.[key] == null ? "—" : String(data[key])}</strong>
            </div>
          ))}
        </div>
        <p className="fine-print">
          Период определяется первой наблюдаемой датой уникального цикла.
          Повторные попытки показаны отдельно и не увеличивают число циклов.
          Ненаблюдаемые банковские события в показатели не включены.
        </p>
        <details className="chart-details">
          <summary>История циклов и попыток</summary>
          {Array.isArray(data?.recurring_rows) && data.recurring_rows.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    {[
                      "agreement_id",
                      "cycle_number",
                      "state",
                      "attempts",
                      "first_observed_at",
                      "latest_observed_at",
                      "time_source",
                    ].map((key) => (
                      <th key={key}>{fieldLabel(key)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(data.recurring_rows as Row[]).map((row, index) => (
                    <tr key={index}>
                      {[
                        "agreement_id",
                        "cycle_number",
                        "state",
                        "attempts",
                        "first_observed_at",
                        "latest_observed_at",
                        "time_source",
                      ].map((key) => (
                        <td key={key}>
                          {key.endsWith("_at") ? (
                            date(String(row[key]), true, timezone)
                          ) : (
                            <AdminValue value={row[key]} name={key} />
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">
              {Array.isArray(data?.recurring_rows) ? copy.empty : copy.absent}
            </p>
          )}
        </details>
      </section>
    </>
  );
}

function MoneyBars({
  title,
  rows,
  field,
  currency,
}: {
  title: string;
  rows: Row[] | null;
  field: string;
  currency: string;
}) {
  const max = Math.max(
    1,
    ...(rows || []).map((row) => Math.abs(Number(row[field]) || 0)),
  );
  return (
    <section className="card">
      <h2>{title}</h2>
      <p className="small-copy">По выбранным фильтрам · {currency}</p>
      {!rows?.length ? (
        <p className="muted">{rows === null ? copy.absent : copy.empty}</p>
      ) : (
        <>
          <ul className="fact-bars">
            {rows.map((row, index) => (
              <li key={index}>
                <div className="row">
                  <span>{String(row.date)}</span>
                  <strong>
                    {money(
                      row[field] == null ? null : String(row[field]),
                      currency,
                    )}
                  </strong>
                </div>
                <div className="fact-track signed" aria-hidden="true">
                  <i
                    style={{
                      width: `${(Math.abs(Number(row[field]) || 0) / max) * 50}%`,
                      left:
                        Number(row[field]) < 0
                          ? `${50 - (Math.abs(Number(row[field]) || 0) / max) * 50}%`
                          : "50%",
                    }}
                  />
                </div>
              </li>
            ))}
          </ul>
          <p className="fine-print">
            Нулевые и отрицательные корректировки показаны по подтверждённым
            фактам. Комиссия повторно из чистого зачисления не вычитается.
          </p>
        </>
      )}
    </section>
  );
}
