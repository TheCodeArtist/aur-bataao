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
    return typeof value === "string" ? value : String(value);
  }
}

export function connectionStatus(profiles, selectedProfileId) {
  const profile = profiles.find((item) => String(item.id) === selectedProfileId);
  if (!profile) return "Configure an endpoint to begin.";
  const keyState = profile.api_key_env && !profile.api_key_configured
    ? " · key variable missing"
    : "";
  return `${profile.name} · ${profile.model}${keyState}`;
}

export function filterModels(models, filter = "") {
  const query = filter.trim().toLocaleLowerCase();
  return query
    ? models.filter((model) => model.toLocaleLowerCase().includes(query))
    : models;
}

export function modelDiscoveryState(profile, models) {
  if (!profile) {
    return {
      disabled: true,
      buttonLabel: "Discover",
      status: "Save the endpoint to discover its models.",
    };
  }
  if (!models) {
    return {
      disabled: false,
      buttonLabel: "Discover",
      status: "Discover models from this endpoint, or enter a model ID manually.",
    };
  }
  return {
    disabled: false,
    buttonLabel: "Refresh",
    status: `${models.length} model${models.length === 1 ? "" : "s"} available. Type to filter or choose one.`,
  };
}

export function normalizeDiscoveredModels(models) {
  if (!Array.isArray(models)) throw new Error("The endpoint returned an invalid model list");
  return [...new Set(models.filter((model) => typeof model === "string" && model.trim()))];
}

export function selectProfile(profiles, preferredId = null) {
  return profiles.find((profile) => profile.id === preferredId)
    || profiles.find((profile) => profile.is_default)
    || profiles[0]
    || null;
}

export function profileFormValues(profile, isFirstProfile = false) {
  return {
    id: profile?.id || "",
    name: profile?.name || "",
    baseUrl: profile?.base_url || "http://127.0.0.1:1234/v1",
    model: profile?.model || "",
    apiKeyEnv: profile?.api_key_env || "",
    timeoutSeconds: profile?.timeout_seconds || 60,
    supportsTools: profile?.supports_tools ?? true,
    isDefault: profile?.is_default ?? isFirstProfile,
  };
}

export function agentBadgeState({ warningCount, normalUpdate, currentPage }) {
  const warning = warningCount > 0;
  const visible = warning || normalUpdate;
  const baseLabel = currentPage ? "Agent" : "Switch to Agent";
  const status = warning ? ", needs attention" : (normalUpdate ? ", has updates" : "");
  return {
    visible,
    kind: visible ? (warning ? "warning" : "normal") : "",
    label: `${baseLabel}${status}`,
  };
}

export function boundedOptionIndex(optionCount, requestedIndex) {
  if (optionCount === 0) return null;
  return Math.max(0, Math.min(requestedIndex, optionCount - 1));
}

export function toolCallState(call, runStatus) {
  const name = call?.function?.name || "tool";
  let summary = `Running ${name}…`;
  if (runStatus === "waiting_approval") summary = `Waiting for approval to run ${name}…`;
  else if (runStatus === "failed" || runStatus === "cancelled") summary = `Did not finish ${name}`;
  return {
    id: call?.id || "",
    name,
    arguments: call?.function?.arguments ?? {},
    summary,
  };
}

export function approvalState(approvals, runId = null) {
  const pending = approvals.filter((approval) => approval.status === "pending");
  const hasDecision = approvals.some((approval) => (
    approval.status === "approved" || approval.status === "rejected"
  ));
  return {
    pending,
    canResume: pending.length === 0 && hasDecision && Boolean(runId),
  };
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
