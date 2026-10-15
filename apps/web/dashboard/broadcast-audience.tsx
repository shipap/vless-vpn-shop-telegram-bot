export const broadcastCopy = {
  audience:
    "Аудитория определяется автоматически перед созданием рассылки и проверяется перед отправкой.",
  channel:
    "Если привязан Telegram, сообщение отправляется в Telegram. Email используется только у пользователей без Telegram с подтверждённой почтой.",
  local:
    "Доставка выполняется с учётом серверных ограничений. Перед созданием проверьте количество получателей и исключённых пользователей.",
  confirmation:
    "Будет создана рассылка для автоматически рассчитанной аудитории. Проверьте текст и количество получателей. Доставка использует один выбранный канал на пользователя.",
};

export function BroadcastAudience() {
  return (
    <div className="notice">
      <p>{broadcastCopy.audience}</p>
      <p>{broadcastCopy.channel}</p>
    </div>
  );
}

export function BroadcastCounts({ value }: { value: Record<string, unknown> }) {
  return (
    <div aria-live="polite">
      <p>
        Аудитория до ограничений доставки: {String(value.eligible_total ?? "—")}
      </p>
      <p>Получателей рассылки: {String(value.recipient_count ?? "—")}</p>
      <p>
        Исключено ограничениями доставки: {String(value.denied_count ?? "—")}
      </p>
    </div>
  );
}
