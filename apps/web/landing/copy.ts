export const copy = {
  skip: "Перейти к содержимому",
  navigation: "Основная навигация",
  account: "Личный кабинет",
  nav: [
    { label: "Тарифы", href: "#tariffs" },
    { label: "Как подключиться", href: "#setup" },
    { label: "Вопросы", href: "#faq" },
  ],
  eyebrow: "VPN ДЛЯ ВАШИХ ПОВСЕДНЕВНЫХ ДЕЛ",
  title: "Ваш VPN.",
  horizon: "Ваш горизонт.",
  lead: "Подписка на VPN для ваших устройств.",
  choose: "Выбрать тариф",
  tariffs: "Тарифы",
  tariffLead: "Выберите вариант, который подходит именно вам.",
  empty: "Сейчас нет доступных тарифов",
  emptyLead: "Загляните позже или обратитесь в поддержку.",
  setup: "Как подключиться",
  setupLead: "От выбора тарифа до подключения.",
  steps: [
    {
      title: "Выберите тариф",
      body: "Откройте личный кабинет и выберите подходящие условия.",
    },
    { title: "Оплатите", body: "Проверьте период и сумму перед оплатой." },
    { title: "Подключите VPN", body: "Следуйте инструкции в личном кабинете." },
  ],
  faq: "Вопросы",
  faqLead: "Короткие ответы на главное.",
  noFaq: "Нужна помощь с подключением? Обратитесь в поддержку.",
  support: "Поддержка",
  telegram: "Открыть Telegram",
  outro: "Готовы начать?",
  outroLead: "Выберите тариф и перейдите к подключению.",
  days: "дн.",
  devices: "Устройств в подписке",
  unavailable: "Покупки временно недоступны",
  offer: "Публичная оферта",
  privacy: "Политика конфиденциальности",
  mascot: "Иллюстрация сервиса",
} as const;
import type { InstanceTerminology } from "@api/types";

export function landingCopy(terms: InstanceTerminology) {
  return {
    ...copy,
    eyebrow: `${terms.service_label.toUpperCase()} ДЛЯ ВАШИХ ПОВСЕДНЕВНЫХ ДЕЛ`,
    title: terms.hero_title,
    steps: copy.steps.map((step, index) =>
      index === 2 ? { ...step, title: terms.connect_label } : step,
    ),
  };
}
