export function parseTimestamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function absoluteTimestamp(value, locale) {
  const date = parseTimestamp(value);
  if (!date) return "Time unavailable";
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function conversationTimestamp(value, now = new Date(), locale) {
  const date = parseTimestamp(value);
  if (!date) return "";
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const daysAgo = Math.round((today - day) / 86400000);
  const clock = new Intl.DateTimeFormat(locale, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
  if (daysAgo === 0) return `Today, ${clock}`;
  if (daysAgo === 1) return `Yesterday, ${clock}`;
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }),
  }).format(date);
}

export function formattedToolValue(value) {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return JSON.stringify(parsed, null, 2);
  } catch (_) {
    return typeof value === "string" ? value : String(value ?? "");
  }
}

export function startSessionPolling({
  sessionId,
  currentSessionId,
  load,
  render,
  schedule = setInterval,
  cancel = clearInterval,
  interval = 750,
}) {
  let stopped = false;
  let inFlight = false;
  const refresh = async () => {
    if (stopped || inFlight || currentSessionId() !== sessionId) return;
    inFlight = true;
    try {
      const detail = await load(sessionId);
      if (!stopped && currentSessionId() === sessionId) render(detail);
    } catch (_) {
      // The primary request reports failures; polling is best-effort UI only.
    } finally {
      inFlight = false;
    }
  };
  const timer = schedule(refresh, interval);
  return () => {
    stopped = true;
    cancel(timer);
  };
}
