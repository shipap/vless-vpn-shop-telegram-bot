export function notificationTarget(value: string | null | undefined) {
  const aliases: Record<string, string> = {
    "/?tab=subscriptions": "/#devices",
    "/#history": "/#payments",
    "/#subscriptions": "/#home",
  };
  if (!value || value.length > 128) return undefined;
  if (Object.hasOwn(aliases, value)) return aliases[value];
  if (/^\/admin(?:#[a-z][a-z0-9_-]*)?$/.test(value)) return value;
  return /^\/(?:#(?:home|devices|payments|bonuses|account|help|notifications))?$/.test(
    value,
  )
    ? value
    : undefined;
}
