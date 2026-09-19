const state = { profiles: [], sessions: [], sessionId: null, busy: false };
const profileForm = document.querySelector("#profile-form");
const profileSelect = document.querySelector("#profile-select");
const sessionList = document.querySelector("#session-list");
const noSessions = document.querySelector("#no-sessions");
const messageList = document.querySelector("#message-list");
const approvalList = document.querySelector("#approval-list");
const composer = document.querySelector("#composer");
const messageInput = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-message");
const connectionStatus = document.querySelector("#connection-status");
const conversationTitle = document.querySelector("#conversation-title");
const errorBox = document.querySelector("#agent-error");
const toast = document.querySelector("#toast");
const themeToggle = document.querySelector("#theme-toggle");
const darkModePreference = window.matchMedia("(prefers-color-scheme: dark)");
let toastTimer;

function savedTheme() {
  try {
    const theme = localStorage.getItem("aur-bataao-theme");
    return theme === "light" || theme === "dark" ? theme : "";
  } catch (_) { return ""; }
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  const dark = theme === "dark";
  const next = dark ? "light" : "dark";
  themeToggle.setAttribute("aria-checked", String(dark));
  themeToggle.setAttribute("aria-label", `Switch to ${next} mode`);
  themeToggle.title = `Switch to ${next} mode`;
  if (persist) {
    try { localStorage.setItem("aur-bataao-theme", theme); } catch (_) { /* unavailable */ }
  }
}

applyTheme(document.documentElement.dataset.theme || (darkModePreference.matches ? "dark" : "light"));
themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
});
darkModePreference.addEventListener("change", (event) => {
  if (!savedTheme()) applyTheme(event.matches ? "dark" : "light");
});

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(url, { ...options, headers });
  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.error || `Request failed (${response.status})`);
  return body;
}

function notify(message, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = error ? "show error" : "show";
  toastTimer = setTimeout(() => { toast.className = ""; }, 2600);
}

function showError(error) {
  errorBox.textContent = error instanceof Error ? error.message : String(error);
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function setBusy(busy, label = "Working…") {
  state.busy = busy;
  sendButton.disabled = busy || state.profiles.length === 0;
  messageInput.disabled = busy || state.profiles.length === 0;
  if (busy) connectionStatus.textContent = label;
  else updateConnectionStatus();
}

function updateConnectionStatus() {
  const profile = state.profiles.find((item) => String(item.id) === profileSelect.value);
  if (!profile) {
    connectionStatus.textContent = "Configure an endpoint to begin.";
    return;
  }
  const keyState = profile.api_key_env && !profile.api_key_configured ? " · key variable missing" : "";
  connectionStatus.textContent = `${profile.name} · ${profile.model}${keyState}`;
}

function fillProfileForm(profile) {
  document.querySelector("#profile-id").value = profile?.id || "";
  document.querySelector("#profile-name").value = profile?.name || "";
  document.querySelector("#profile-base-url").value = profile?.base_url || "http://127.0.0.1:1234/v1";
  document.querySelector("#profile-model").value = profile?.model || "";
  document.querySelector("#profile-api-key-env").value = profile?.api_key_env || "";
  document.querySelector("#profile-timeout").value = profile?.timeout_seconds || 60;
  document.querySelector("#profile-tools").checked = profile?.supports_tools ?? true;
  document.querySelector("#profile-default").checked = profile?.is_default ?? state.profiles.length === 0;
  document.querySelector("#discover-models").disabled = !profile;
}

async function loadProfiles(preferredId = null) {
  state.profiles = (await api("/api/llm-profiles")).profiles;
  profileSelect.replaceChildren();
  if (!state.profiles.length) {
    profileSelect.append(new Option("No saved endpoints", ""));
    fillProfileForm(null);
  } else {
    state.profiles.forEach((profile) => {
      profileSelect.append(new Option(`${profile.name} · ${profile.model}`, String(profile.id)));
    });
    const chosen = state.profiles.find((profile) => profile.id === preferredId)
      || state.profiles.find((profile) => profile.is_default)
      || state.profiles[0];
    profileSelect.value = String(chosen.id);
    fillProfileForm(chosen);
  }
  setBusy(false);
}

profileSelect.addEventListener("change", () => {
  const profile = state.profiles.find((item) => String(item.id) === profileSelect.value);
  fillProfileForm(profile || null);
  updateConnectionStatus();
});

document.querySelector("#new-profile").addEventListener("click", () => fillProfileForm(null));

profileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const id = document.querySelector("#profile-id").value;
  const body = {
    name: document.querySelector("#profile-name").value,
    base_url: document.querySelector("#profile-base-url").value,
    model: document.querySelector("#profile-model").value,
    api_key_env: document.querySelector("#profile-api-key-env").value || null,
    timeout_seconds: Number(document.querySelector("#profile-timeout").value),
    supports_tools: document.querySelector("#profile-tools").checked,
    is_default: document.querySelector("#profile-default").checked,
  };
  try {
    const result = await api(id ? `/api/llm-profiles/${id}` : "/api/llm-profiles", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(body),
    });
    await loadProfiles(result.profile.id);
    notify("Endpoint saved");
  } catch (error) { showError(error); }
});

document.querySelector("#discover-models").addEventListener("click", async () => {
  const id = document.querySelector("#profile-id").value;
  if (!id) return;
  clearError();
  try {
    const { models } = await api(`/api/llm-profiles/${id}/models`);
    const options = document.querySelector("#model-options");
    options.replaceChildren(...models.map((model) => new Option(model, model)));
    notify(models.length ? `Found ${models.length} model${models.length === 1 ? "" : "s"}` : "No models reported");
  } catch (error) { showError(error); }
});

function renderSessions() {
  sessionList.replaceChildren();
  state.sessions.forEach((session) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-button";
    button.textContent = session.title;
    button.title = session.title;
    if (session.id === state.sessionId) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => selectSession(session.id));
    sessionList.append(button);
  });
  noSessions.hidden = state.sessions.length > 0;
}

async function loadSessions() {
  state.sessions = (await api("/api/agent/sessions")).sessions;
  renderSessions();
}

function renderMessages(messages) {
  messageList.replaceChildren();
  const visible = messages.filter((message) => (
    (message.role === "user" || message.role === "assistant") && typeof message.content === "string"
  ) || message.role === "tool");
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "agent-empty";
    const heading = document.createElement("h2");
    heading.textContent = "What should we work through?";
    const copy = document.createElement("p");
    copy.textContent = "Ask about your tasks, plan the next action, or propose an update. You approve every change before it is applied.";
    empty.append(heading, copy);
    messageList.append(empty);
    return;
  }
  visible.forEach((message) => {
    if (message.role === "tool") {
      const details = document.createElement("details");
      details.className = "agent-message tool";
      const summary = document.createElement("summary");
      summary.textContent = "Tool result";
      const output = document.createElement("pre");
      try { output.textContent = JSON.stringify(JSON.parse(message.content), null, 2); }
      catch (_) { output.textContent = message.content; }
      details.append(summary, output);
      messageList.append(details);
      return;
    }
    const item = document.createElement("div");
    item.className = `agent-message ${message.role}`;
    item.textContent = message.content;
    messageList.append(item);
  });
  messageList.scrollTop = messageList.scrollHeight;
}

function renderApprovals(approvals) {
  approvalList.replaceChildren();
  approvals.filter((approval) => approval.status === "pending").forEach((approval) => {
    const card = document.createElement("article");
    card.className = "approval-card";
    const heading = document.createElement("h2");
    heading.textContent = `Allow ${approval.tool_name}?`;
    const argumentsBlock = document.createElement("pre");
    argumentsBlock.textContent = JSON.stringify(approval.arguments, null, 2);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const reject = document.createElement("button");
    reject.type = "button";
    reject.className = "reject-button";
    reject.textContent = "Reject";
    reject.addEventListener("click", () => decideApproval(approval.id, false));
    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "primary";
    approve.textContent = "Approve";
    approve.addEventListener("click", () => decideApproval(approval.id, true));
    actions.append(reject, approve);
    card.append(heading, argumentsBlock, actions);
    approvalList.append(card);
  });
}

async function selectSession(sessionId) {
  clearError();
  state.sessionId = sessionId;
  renderSessions();
  try {
    const detail = await api(`/api/agent/sessions/${sessionId}`);
    conversationTitle.textContent = detail.session.title;
    const sessionProfile = state.profiles.find((profile) => profile.id === detail.session.profile_id);
    if (sessionProfile) {
      profileSelect.value = String(sessionProfile.id);
      fillProfileForm(sessionProfile);
      updateConnectionStatus();
    }
    renderMessages(detail.messages);
    const latestRun = detail.runs.at(-1);
    if (latestRun?.status === "waiting_approval") {
      const runDetail = await api(`/api/agent/runs/${latestRun.id}`);
      renderApprovals(runDetail.approvals);
    } else {
      renderApprovals([]);
    }
  } catch (error) { showError(error); }
}

document.querySelector("#new-conversation").addEventListener("click", () => {
  state.sessionId = null;
  renderSessions();
  renderMessages([]);
  renderApprovals([]);
  conversationTitle.textContent = "New conversation";
  messageInput.focus();
});

async function ensureSession(firstMessage) {
  if (state.sessionId) return state.sessionId;
  const profileId = Number(profileSelect.value);
  if (!profileId) throw new Error("Configure an endpoint before starting a conversation");
  const result = await api("/api/agent/sessions", {
    method: "POST",
    body: JSON.stringify({ profile_id: profileId, title: firstMessage.slice(0, 80) }),
  });
  state.sessionId = result.session.id;
  await loadSessions();
  conversationTitle.textContent = result.session.title;
  return state.sessionId;
}

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = messageInput.value.trim();
  if (!content || state.busy) return;
  clearError();
  setBusy(true, "Agent is working…");
  try {
    const sessionId = await ensureSession(content);
    messageInput.value = "";
    const outcome = await api(`/api/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
    await loadSessions();
    await selectSession(sessionId);
    renderApprovals(outcome.pending_approvals);
  } catch (error) { showError(error); }
  finally { setBusy(false); }
});

async function decideApproval(approvalId, approved) {
  if (state.busy) return;
  clearError();
  setBusy(true, approved ? "Applying approved change…" : "Returning your decision…");
  try {
    const outcome = await api(`/api/agent/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
    await loadSessions();
    await selectSession(state.sessionId);
    renderApprovals(outcome.pending_approvals);
  } catch (error) { showError(error); }
  finally { setBusy(false); }
}

async function initialize() {
  try {
    await loadProfiles();
    await loadSessions();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else renderMessages([]);
  } catch (error) { showError(error); }
}

initialize();
