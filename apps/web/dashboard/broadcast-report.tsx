type Report = Record<string, unknown>;

export function BroadcastReport({ value }: { value: Report }) {
  const fields = [
    ["recipient_count", "Получателей"],
    ["delivered_count", "Доставлено"],
    ["blocked_count", "Заблокировано правилами или согласием"],
    ["provider_rejected_count", "Отклонено Telegram или почтой"],
    ["unknown_count", "Неизвестный результат"],
    ["waiting_count", "Ожидают доставки"],
    ["cancelled_count", "Отменено"],
  ];
  return (
    <section className="stack">
      <p className="small-copy">
        {value.state === "done"
          ? "Подготовка очереди завершена — это не означает доставку."
          : "Подготовка очереди не означает доставку."}{" "}
        Ниже показаны результаты отправки по одному основному каналу на
        пользователя.
      </p>
      <dl className="detail-list">
        {fields.map(([key, label]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>{String(value[key] ?? 0)}</dd>
          </div>
        ))}
      </dl>
      {Number(value.unknown_count ?? 0) > 0 && (
        <p className="notice warning">
          Результат части отправок неизвестен. Автоматический повтор может
          создать дубликат — сначала нужна проверка.
        </p>
      )}
      {Number(value.waiting_count ?? 0) > 0 && (
        <p className="fine-print">
          Отправка продолжается. Обновите отчёт позднее.
        </p>
      )}
    </section>
  );
}
