import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import CodeSlots from "../../packages/web-ui/CodeSlots";
import BellToggle from "../../packages/web-ui/reactbits/BellToggle";
import SwipeToast from "../../packages/web-ui/reactbits/SwipeToast";

function Controls() {
  const [code, setCode] = useState("");
  const [mask, setMask] = useState(false);
  const [disabled, setDisabled] = useState(false);
  const [submissions, setSubmissions] = useState(0);
  const [completions, setCompletions] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [toast, setToast] = useState(0);
  const [toastOpen, setToastOpen] = useState(true);
  const [closed, setClosed] = useState<string[]>([]);
  const [actions, setActions] = useState(0);
  return (
    <main>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setSubmissions((n) => n + 1);
        }}
      >
        <CodeSlots
          value={code}
          onChange={setCode}
          onComplete={() => setCompletions((n) => n + 1)}
          ariaLabel="Код"
          mask={mask}
          disabled={disabled}
          required
        />
        <button type="submit">Подтвердить</button>
      </form>
      <label>
        <input
          type="checkbox"
          checked={mask}
          onChange={(e) => setMask(e.target.checked)}
        />
        Скрывать код
      </label>
      <label>
        <input
          type="checkbox"
          checked={disabled}
          onChange={(e) => setDisabled(e.target.checked)}
        />
        Заблокировать
      </label>
      <button type="button" onClick={() => setCode("")}>
        Сбросить код
      </button>
      <output aria-label="Отправлено">{submissions}</output>
      <output aria-label="Заполнено">{completions}</output>
      <BellToggle
        label="Уведомления, 2 непрочитанных"
        count={2}
        pressed
        expanded={expanded}
        controls="test-inbox"
        popoverTarget="test-inbox"
      />
      <div
        id="test-inbox"
        popover="auto"
        onToggle={(event) => setExpanded(event.newState === "open")}
      >
        Входящие
      </div>
      <button type="button" onClick={() => setToast((n) => n + 1)}>
        Показать уведомление
      </button>
      {toast > 0 && (
        <SwipeToast
          key={toast}
          open={toastOpen}
          inline
          title="Новое сообщение"
          description="Описание"
          duration={500}
          pauseOnHover
          closeButton
          actionLabel="Открыть"
          onAction={() => setActions((n) => n + 1)}
          onClose={(reason) => setClosed((items) => [...items, reason])}
        />
      )}
      <output aria-label="Закрытия">{closed.join(",")}</output>
      <output aria-label="Действия">{actions}</output>
      <button type="button">Вне уведомления</button>
      <button type="button" onClick={() => setToast(0)}>
        Убрать компонент
      </button>
      <button type="button" onClick={() => setToastOpen(false)}>
        Скрыть без размонтирования
      </button>
      <button type="button" onClick={() => setToastOpen(true)}>
        Открыть без размонтирования
      </button>
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Controls />);
