import { renderMarkdown } from "./markdown.js";

const state = {
  profiles: [], sessions: [], sessionId: null, busy: false,
  modelCache: new Map(), discoveryDirty: false, messageSignature: null,
  initialized: false, normalUpdate: false, warningSources: new Set(),
};
const profileForm = document.querySelector("#profile-form");
const profileSelect = document.querySelector("#profile-select");
const modelInput = document.querySelector("#profile-model");
const modelPicker = document.querySelector("#model-picker");
const modelOptions = document.querySelector("#model-options");
const modelStatus = document.querySelector("#model-discovery-status");
const discoverModelsButton = document.querySelector("#discover-models");
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
const widthToggle = document.querySelector("#width-toggle");
const agentLayout = document.querySelector(".agent-layout");
const agentUpdateBadge = document.querySelector("#agent-update-badge");
const agentViewLink = document.querySelector("#agent-view-link");
let activeModelOption = -1;
let modelRequest = 0;
let initializePromise = null;

function agentIsVisible() {
  return document.body.classList.contains("agent-mode");
}

function renderAgentBadge() {
  const warning = state.warningSources.size > 0;
  const visible = warning || state.normalUpdate;
  agentUpdateBadge.hidden = !visible;
  if (visible) agentUpdateBadge.dataset.kind = warning ? "warning" : "normal";
  else delete agentUpdateBadge.dataset.kind;
  const baseLabel = agentViewLink.getAttribute("aria-current") === "page" ? "Agent" : "Switch to Agent";
  const status = warning ? ", needs attention" : (state.normalUpdate ? ", has updates" : "");
  agentViewLink.setAttribute("aria-label", `${baseLabel}${status}`);
}

function markAgentUpdate() {
  if (agentIsVisible()) return;
  state.normalUpdate = true;
  renderAgentBadge();
}

function setAgentWarning(source, active) {
  if (active && !agentIsVisible()) state.warningSources.add(source);
  else if (!active) state.warningSources.delete(source);
  renderAgentBadge();
}

function clearAgentBadges() {
  state.normalUpdate = false;
  state.warningSources.clear();
  renderAgentBadge();
}

function applyFullWidth(fullWidth, persist = false) {
  if (fullWidth) agentLayout.dataset.width = "full";
  else delete agentLayout.dataset.width;
  widthToggle.setAttribute("aria-checked", String(fullWidth));
  const action = fullWidth ? "Use centered width" : "Use full viewport width";
  widthToggle.setAttribute("aria-label", action);
  widthToggle.title = action;
  if (persist) {
    try { localStorage.setItem("aur-bataao-agent-full-width", String(fullWidth)); } catch (_) { /* unavailable */ }
  }
}

let savedFullWidth = false;
try { savedFullWidth = localStorage.getItem("aur-bataao-agent-full-width") === "true"; } catch (_) { /* unavailable */ }
applyFullWidth(savedFullWidth);
widthToggle.addEventListener("click", () => {
  applyFullWidth(agentLayout.dataset.width !== "full", true);
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
  window.dispatchEvent(new CustomEvent("app:notify", { detail: { message, error } }));
}

function showError(error) {
  errorBox.textContent = error instanceof Error ? error.message : String(error);
  errorBox.hidden = false;
  setAgentWarning("error", true);
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
  setAgentWarning("error", false);
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

function selectedProfileId() {
  return document.querySelector("#profile-id").value;
}

function setModelStatus(message, error = false) {
  modelStatus.textContent = message;
  modelStatus.classList.toggle("error", error);
}

function closeModelOptions() {
  modelOptions.hidden = true;
  modelOptions.removeAttribute("aria-busy");
  modelInput.setAttribute("aria-expanded", "false");
  modelInput.removeAttribute("aria-activedescendant");
  activeModelOption = -1;
}

function setActiveModelOption(index) {
  const options = [...modelOptions.querySelectorAll(".model-option")];
  if (!options.length) return;
  activeModelOption = Math.max(0, Math.min(index, options.length - 1));
  options.forEach((option, optionIndex) => {
    option.classList.toggle("is-active", optionIndex === activeModelOption);
  });
  const active = options[activeModelOption];
  modelInput.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function renderModelOptions(filter = "") {
  const models = state.modelCache.get(selectedProfileId());
  if (!models) return closeModelOptions();
  const query = filter.trim().toLocaleLowerCase();
  const matches = query
    ? models.filter((model) => model.toLocaleLowerCase().includes(query))
    : models;
  modelOptions.replaceChildren();
  modelOptions.removeAttribute("aria-busy");
  activeModelOption = -1;
  modelInput.removeAttribute("aria-activedescendant");
  if (!matches.length) {
    const empty = document.createElement("p");
    empty.className = "model-option-empty";
    empty.textContent = models.length
      ? "No matching models. You can enter the model ID manually."
      : "No models were returned. Enter the model ID manually.";
    modelOptions.append(empty);
  } else {
    matches.forEach((model, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.tabIndex = -1;
      option.id = `model-option-${index}`;
      option.className = "model-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(model === modelInput.value));
      option.dataset.model = model;
      option.textContent = model;
      modelOptions.append(option);
    });
  }
  modelOptions.hidden = false;
  modelInput.setAttribute("aria-expanded", "true");
}

function chooseModel(model) {
  modelInput.value = model;
  modelInput.focus();
  closeModelOptions();
}

function updateModelDiscovery(profile) {
  modelRequest += 1;
  state.discoveryDirty = false;
  closeModelOptions();
  if (!profile) {
    discoverModelsButton.disabled = true;
    discoverModelsButton.textContent = "Discover";
    setModelStatus("Save the endpoint to discover its models.");
    return;
  }
  const models = state.modelCache.get(String(profile.id));
  discoverModelsButton.disabled = false;
  discoverModelsButton.textContent = models ? "Refresh" : "Discover";
  setModelStatus(models
    ? `${models.length} model${models.length === 1 ? "" : "s"} available. Type to filter or choose one.`
    : "Discover models from this endpoint, or enter a model ID manually.");
}

function fillProfileForm(profile) {
  document.querySelector("#profile-id").value = profile?.id || "";
  document.querySelector("#profile-name").value = profile?.name || "";
  document.querySelector("#profile-base-url").value = profile?.base_url || "http://127.0.0.1:1234/v1";
  modelInput.value = profile?.model || "";
  document.querySelector("#profile-api-key-env").value = profile?.api_key_env || "";
  document.querySelector("#profile-timeout").value = profile?.timeout_seconds || 60;
  document.querySelector("#profile-tools").checked = profile?.supports_tools ?? true;
  document.querySelector("#profile-default").checked = profile?.is_default ?? state.profiles.length === 0;
  updateModelDiscovery(profile);
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
    model: modelInput.value,
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
    state.modelCache.delete(String(result.profile.id));
    await loadProfiles(result.profile.id);
    notify("Endpoint saved");
  } catch (error) { showError(error); }
});

discoverModelsButton.addEventListener("click", async () => {
  const id = selectedProfileId();
  if (!id || state.discoveryDirty) return;
  const request = ++modelRequest;
  discoverModelsButton.disabled = true;
  discoverModelsButton.textContent = "Loading…";
  setModelStatus("Discovering models…");
  modelOptions.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "model-option-empty";
  loading.textContent = "Discovering models…";
  modelOptions.append(loading);
  modelOptions.setAttribute("aria-busy", "true");
  modelOptions.hidden = false;
  modelInput.setAttribute("aria-expanded", "true");
  try {
    const { models } = await api(`/api/llm-profiles/${id}/models`);
    if (!Array.isArray(models)) throw new Error("The endpoint returned an invalid model list");
    const discovered = [...new Set(models.filter((model) => typeof model === "string" && model.trim()))];
    if (request !== modelRequest || id !== selectedProfileId()) return;
    state.modelCache.set(id, discovered);
    modelOptions.removeAttribute("aria-busy");
    discoverModelsButton.textContent = "Refresh";
    setModelStatus(discovered.length
      ? `${discovered.length} model${discovered.length === 1 ? "" : "s"} available. Type to filter or choose one.`
      : "No models were returned. Enter a model ID manually.");
    renderModelOptions();
  } catch (error) {
    if (request !== modelRequest) return;
    modelOptions.removeAttribute("aria-busy");
    closeModelOptions();
    discoverModelsButton.textContent = "Retry";
    setModelStatus(`Could not discover models: ${error.message}`, true);
  } finally {
    if (request === modelRequest) discoverModelsButton.disabled = false;
  }
});

modelInput.addEventListener("focus", () => {
  if (state.modelCache.has(selectedProfileId()) && modelOptions.hidden) renderModelOptions();
});

modelInput.addEventListener("click", () => {
  if (state.modelCache.has(selectedProfileId()) && modelOptions.hidden) renderModelOptions();
});

modelInput.addEventListener("input", () => {
  if (state.modelCache.has(selectedProfileId())) renderModelOptions(modelInput.value);
});

modelInput.addEventListener("keydown", (event) => {
  const models = state.modelCache.get(selectedProfileId());
  if (event.key === "ArrowDown" && modelOptions.hidden && models) {
    event.preventDefault();
    renderModelOptions();
    setActiveModelOption(0);
    return;
  }
  if (modelOptions.hidden) return;
  const options = modelOptions.querySelectorAll(".model-option");
  if (event.key === "ArrowDown" && options.length) {
    event.preventDefault();
    setActiveModelOption(activeModelOption + 1);
  } else if (event.key === "ArrowUp" && options.length) {
    event.preventDefault();
    setActiveModelOption(activeModelOption < 0 ? options.length - 1 : activeModelOption - 1);
  } else if (event.key === "Enter" && activeModelOption >= 0) {
    event.preventDefault();
    chooseModel(options[activeModelOption].dataset.model);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closeModelOptions();
  }
});

modelOptions.addEventListener("click", (event) => {
  const option = event.target.closest(".model-option");
  if (option) chooseModel(option.dataset.model);
});

modelPicker.addEventListener("focusout", (event) => {
  if (!modelPicker.contains(event.relatedTarget)) closeModelOptions();
});

document.addEventListener("pointerdown", (event) => {
  if (!modelPicker.contains(event.target)) closeModelOptions();
});

["profile-base-url", "profile-api-key-env", "profile-timeout"].forEach((id) => {
  document.querySelector(`#${id}`).addEventListener("input", () => {
    const profileId = selectedProfileId();
    if (!profileId || state.discoveryDirty) return;
    state.discoveryDirty = true;
    modelRequest += 1;
    state.modelCache.delete(profileId);
    discoverModelsButton.disabled = true;
    discoverModelsButton.textContent = "Discover";
    closeModelOptions();
    setModelStatus("Save endpoint changes before discovering models.");
  });
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

function formattedToolValue(value) {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    return JSON.stringify(parsed, null, 2);
  } catch (_) {
    return typeof value === "string" ? value : String(value ?? "");
  }
}

function createToolCall(call, runStatus) {
  const name = call?.function?.name || "tool";
  const details = document.createElement("details");
  details.className = "agent-message tool";
  if (call?.id) details.dataset.toolCallId = call.id;

  const summary = document.createElement("summary");
  if (runStatus === "waiting_approval") summary.textContent = `Waiting for approval to run ${name}…`;
  else if (runStatus === "failed" || runStatus === "cancelled") summary.textContent = `Did not finish ${name}`;
  else summary.textContent = `Running ${name}…`;

  const body = document.createElement("div");
  body.className = "tool-details";
  const callLabel = document.createElement("p");
  callLabel.className = "tool-detail-label";
  callLabel.textContent = "Call";
  const callValue = document.createElement("pre");
  callValue.textContent = formattedToolValue(call?.function?.arguments ?? {});
  const responseLabel = document.createElement("p");
  responseLabel.className = "tool-detail-label";
  responseLabel.textContent = "Response";
  const responseValue = document.createElement("pre");
  responseValue.className = "tool-response pending";
  responseValue.textContent = "Awaiting tool response…";
  body.append(callLabel, callValue, responseLabel, responseValue);
  details.append(summary, body);
  return { details, name, summary, responseValue };
}

function renderMessages(messages, runStatus = null) {
  const signature = JSON.stringify([messages, runStatus]);
  if (signature === state.messageSignature) return;
  if (state.initialized) markAgentUpdate();
  const expandedCalls = new Set(
    [...messageList.querySelectorAll("details[data-tool-call-id][open]")]
      .map((details) => details.dataset.toolCallId),
  );
  messageList.replaceChildren();
  const toolCalls = new Map();
  let rendered = 0;

  messages.forEach((message) => {
    if (message.role === "assistant") {
      if (typeof message.content === "string" && message.content.trim()) {
        const item = document.createElement("div");
        item.className = "agent-message assistant";
        renderMarkdown(item, message.content);
        messageList.append(item);
        rendered += 1;
      }
      (Array.isArray(message.tool_calls) ? message.tool_calls : []).forEach((call) => {
        const view = createToolCall(call, runStatus);
        if (expandedCalls.has(call.id)) view.details.open = true;
        messageList.append(view.details);
        if (call.id) toolCalls.set(call.id, view);
        rendered += 1;
      });
      return;
    }
    if (message.role === "tool") {
      const view = toolCalls.get(message.tool_call_id);
      if (view) {
        view.summary.textContent = `Ran ${view.name}`;
        view.responseValue.classList.remove("pending");
        view.responseValue.textContent = formattedToolValue(message.content);
      } else {
        const details = document.createElement("details");
        details.className = "agent-message tool";
        const summary = document.createElement("summary");
        summary.textContent = "Tool response";
        const output = document.createElement("pre");
        output.textContent = formattedToolValue(message.content);
        details.append(summary, output);
        messageList.append(details);
        rendered += 1;
      }
      return;
    }
    if (message.role === "user" && typeof message.content === "string" && message.content.trim()) {
      const item = document.createElement("div");
      item.className = "agent-message user";
      renderMarkdown(item, message.content);
      messageList.append(item);
      rendered += 1;
    }
  });

  if (!rendered) {
    const empty = document.createElement("div");
    empty.className = "agent-empty";
    const heading = document.createElement("h2");
    heading.textContent = "What should we work through?";
    const copy = document.createElement("p");
    copy.textContent = "Ask about your tasks, plan the next action, or propose an update. You approve every change before it is applied.";
    empty.append(heading, copy);
    messageList.append(empty);
  }
  state.messageSignature = signature;
  messageList.scrollTop = messageList.scrollHeight;
}

function appendUserMessage(content) {
  state.messageSignature = null;
  messageList.querySelector(".agent-empty")?.remove();
  const item = document.createElement("div");
  item.className = "agent-message user";
  renderMarkdown(item, content);
  messageList.append(item);
  messageList.scrollTop = messageList.scrollHeight;
  return item;
}

function renderApprovals(approvals, runId = null) {
  approvalList.replaceChildren();
  const pending = approvals.filter((approval) => approval.status === "pending");
  setAgentWarning("approval", pending.length > 0);
  pending.forEach((approval) => {
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
  const hasUnconsumedDecision = approvals.some((approval) => (
    approval.status === "approved" || approval.status === "rejected"
  ));
  if (!pending.length && hasUnconsumedDecision && runId) {
    const card = document.createElement("article");
    card.className = "approval-card";
    const heading = document.createElement("h2");
    heading.textContent = "Continue this interrupted run?";
    const copy = document.createElement("p");
    copy.textContent = "Your approval decision was saved, but the run stopped before it could continue.";
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const resume = document.createElement("button");
    resume.type = "button";
    resume.className = "primary";
    resume.textContent = "Resume";
    resume.addEventListener("click", () => resumeRun(runId));
    actions.append(resume);
    card.append(heading, copy, actions);
    approvalList.append(card);
  }
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
    const latestRun = detail.runs.at(-1);
    renderMessages(detail.messages, latestRun?.status);
    if (latestRun?.status === "waiting_approval") {
      const runDetail = await api(`/api/agent/runs/${latestRun.id}`);
      renderApprovals(runDetail.approvals, latestRun.id);
    } else {
      renderApprovals([]);
    }
  } catch (error) { showError(error); }
}

function pollSessionMessages(sessionId) {
  let stopped = false;
  let inFlight = false;
  const refresh = async () => {
    if (stopped || inFlight || state.sessionId !== sessionId) return;
    inFlight = true;
    try {
      const detail = await api(`/api/agent/sessions/${sessionId}`);
      if (stopped || state.sessionId !== sessionId) return;
      renderMessages(detail.messages, detail.runs.at(-1)?.status);
    } catch (_) {
      // The primary request reports failures; polling is best-effort UI only.
    } finally {
      inFlight = false;
    }
  };
  const timer = setInterval(refresh, 750);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

async function apiWithSessionPolling(sessionId, url, options) {
  const stopPolling = pollSessionMessages(sessionId);
  try {
    return await api(url, options);
  } finally {
    stopPolling();
  }
}

document.querySelector("#new-conversation").addEventListener("click", () => {
  state.sessionId = null;
  renderSessions();
  renderMessages([]);
  renderApprovals([]);
  conversationTitle.textContent = "New conversation";
  messageInput.focus();
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  composer.requestSubmit();
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
  messageInput.value = "";
  const optimisticMessage = appendUserMessage(content);
  setBusy(true, "Agent is working…");
  let sessionId = state.sessionId;
  try {
    sessionId = await ensureSession(content);
    const outcome = await apiWithSessionPolling(sessionId, `/api/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
    await loadSessions();
    await selectSession(sessionId);
    renderApprovals(outcome.pending_approvals);
  } catch (error) {
    if (sessionId) await selectSession(sessionId);
    else {
      optimisticMessage.remove();
      messageInput.value = content;
    }
    showError(error);
  }
  finally { setBusy(false); }
});

async function decideApproval(approvalId, approved) {
  if (state.busy) return;
  const sessionId = state.sessionId;
  clearError();
  setBusy(true, approved ? "Applying approved change…" : "Returning your decision…");
  if (approved) window.dispatchEvent(new CustomEvent("agent:task-mutation-start"));
  try {
    const outcome = await apiWithSessionPolling(sessionId, `/api/agent/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
    await loadSessions();
    await selectSession(sessionId);
    renderApprovals(outcome.pending_approvals);
    if (approved) window.dispatchEvent(new CustomEvent("agent:tasks-mutated"));
  } catch (error) { showError(error); }
  finally {
    if (approved) window.dispatchEvent(new CustomEvent("agent:task-mutation-end"));
    setBusy(false);
  }
}

async function resumeRun(runId) {
  if (state.busy) return;
  const sessionId = state.sessionId;
  clearError();
  setBusy(true, "Resuming saved decision…");
  window.dispatchEvent(new CustomEvent("agent:task-mutation-start"));
  try {
    await apiWithSessionPolling(sessionId, `/api/agent/runs/${runId}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadSessions();
    await selectSession(sessionId);
    window.dispatchEvent(new CustomEvent("agent:tasks-mutated"));
  } catch (error) { showError(error); }
  finally {
    window.dispatchEvent(new CustomEvent("agent:task-mutation-end"));
    setBusy(false);
  }
}

async function initialize() {
  try {
    await loadProfiles();
    await loadSessions();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else renderMessages([]);
  } catch (error) { showError(error); }
  finally { state.initialized = true; }
}

function initializeAgent() {
  if (!initializePromise) initializePromise = initialize();
  return initializePromise;
}

async function enterAgentView() {
  await initializeAgent();
  clearAgentBadges();
}

window.addEventListener("app:viewchange", (event) => {
  if (event.detail?.view === "agent") enterAgentView();
  else renderAgentBadge();
});

if (agentIsVisible()) enterAgentView();
