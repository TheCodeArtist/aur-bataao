import { renderMarkdown } from "./markdown.js";
import {
  absoluteTimestamp,
  agentBadgeState,
  approvalState,
  boundedOptionIndex,
  connectionStatus as profileConnectionStatus,
  conversationTimestamp,
  filterModels,
  formattedToolValue,
  modelDiscoveryState,
  normalizeDiscoveredModels,
  profileFormValues,
  selectProfile,
  startSessionPolling,
  toolCallState,
} from "./agent_logic.js";
import { requestJson as api } from "./http.js";

const state = {
  profiles: [], sessions: [], folders: [], sessionId: null, busy: false,
  modelCache: new Map(), discoveryDirty: false, messageSignature: null,
  initialized: false, normalUpdate: false, warningSources: new Set(),
  currentSession: null, movingSessionId: null, collapsedFolderKeys: new Set(),
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
const newFolderButton = document.querySelector("#new-folder");
const moveConversationDialog = document.querySelector("#move-conversation-dialog");
const moveConversationForm = document.querySelector("#move-conversation-form");
const moveConversationFolder = document.querySelector("#move-conversation-folder");
const moveConversationCancel = document.querySelector("#cancel-move-conversation");
const moveConversationSubmit = moveConversationForm.querySelector('[type="submit"]');
const messageList = document.querySelector("#message-list");
const conversationScroll = document.querySelector(".conversation-scroll");
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
const collapsedFoldersStorageKey = "aur-bataao-agent-collapsed-folders";

try {
  const savedCollapsedFolders = JSON.parse(localStorage.getItem(collapsedFoldersStorageKey) || "[]");
  if (Array.isArray(savedCollapsedFolders)) {
    state.collapsedFolderKeys = new Set(savedCollapsedFolders.filter((key) => typeof key === "string"));
  }
} catch (_) { /* unavailable or invalid */ }

function updateConversationScrollShadows() {
  const overflow = messageList.scrollHeight - messageList.clientHeight;
  conversationScroll.classList.toggle("has-content-above", messageList.scrollTop > 1);
  conversationScroll.classList.toggle("has-content-below", overflow - messageList.scrollTop > 1);
}

messageList.addEventListener("scroll", updateConversationScrollShadows, { passive: true });
messageList.addEventListener("load", updateConversationScrollShadows, true);
messageList.addEventListener("toggle", updateConversationScrollShadows, true);
window.addEventListener("resize", updateConversationScrollShadows);
new ResizeObserver(updateConversationScrollShadows).observe(messageList);
new MutationObserver(updateConversationScrollShadows).observe(messageList, {
  childList: true, subtree: true, characterData: true,
});

function agentIsVisible() {
  return document.body.classList.contains("agent-mode");
}

function renderAgentBadge() {
  const view = agentBadgeState({
    warningCount: state.warningSources.size,
    normalUpdate: state.normalUpdate,
    currentPage: agentViewLink.getAttribute("aria-current") === "page",
  });
  agentUpdateBadge.hidden = !view.visible;
  if (view.visible) agentUpdateBadge.dataset.kind = view.kind;
  else delete agentUpdateBadge.dataset.kind;
  agentViewLink.setAttribute("aria-label", view.label);
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
  renderWaitingIndicator();
}

function updateConnectionStatus() {
  connectionStatus.textContent = profileConnectionStatus(state.profiles, profileSelect.value);
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
  const boundedIndex = boundedOptionIndex(options.length, index);
  if (boundedIndex === null) return;
  activeModelOption = boundedIndex;
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
  const matches = filterModels(models, filter);
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
  const models = profile ? state.modelCache.get(String(profile.id)) : undefined;
  const view = modelDiscoveryState(profile, models);
  discoverModelsButton.disabled = view.disabled;
  discoverModelsButton.textContent = view.buttonLabel;
  setModelStatus(view.status);
}

function fillProfileForm(profile) {
  const values = profileFormValues(profile, state.profiles.length === 0);
  document.querySelector("#profile-id").value = values.id;
  document.querySelector("#profile-name").value = values.name;
  document.querySelector("#profile-base-url").value = values.baseUrl;
  modelInput.value = values.model;
  document.querySelector("#profile-api-key-env").value = values.apiKeyEnv;
  document.querySelector("#profile-timeout").value = values.timeoutSeconds;
  document.querySelector("#profile-tools").checked = values.supportsTools;
  document.querySelector("#profile-default").checked = values.isDefault;
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
    const chosen = selectProfile(state.profiles, preferredId);
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
    const discovered = normalizeDiscoveredModels(models);
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

function createSessionRow(session) {
  const row = document.createElement("div");
  row.className = "session-row";
  if (session.id === state.sessionId) row.dataset.current = "true";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "session-button";
  const title = document.createElement("span");
  title.className = "session-title";
  title.textContent = session.title;
  button.append(title);
  button.title = `${session.title} · ${absoluteTimestamp(session.updated_at)}`;
  if (session.id === state.sessionId) button.setAttribute("aria-current", "page");
  button.addEventListener("click", () => selectSession(session.id));

  const meta = document.createElement("div");
  meta.className = "session-meta";
  meta.append(createTimestamp(session.updated_at, "session-timestamp"));
  const moveButton = document.createElement("button");
  moveButton.type = "button";
  moveButton.className = "session-move-button";
  moveButton.setAttribute("aria-label", `Move ${session.title} to a folder`);
  moveButton.title = "Move to folder";
  const moveIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  moveIcon.setAttribute("viewBox", "0 0 24 24");
  moveIcon.setAttribute("aria-hidden", "true");
  moveIcon.setAttribute("focusable", "false");
  const folderPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  folderPath.setAttribute("d", "M3 4.5h6l2.5 3H21v13H3z");
  const arrowPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrowPath.setAttribute("d", "M7 14h9m-3-3l3 3-3 3");
  moveIcon.append(folderPath, arrowPath);
  moveButton.append(moveIcon);
  moveButton.addEventListener("click", () => openMoveConversation(session));
  meta.append(moveButton);
  row.append(button, meta);
  return row;
}

function folderGroupKey(folder) {
  return folder ? `folder-${folder.id}` : "unfiled";
}

function persistCollapsedFolders() {
  try {
    localStorage.setItem(collapsedFoldersStorageKey, JSON.stringify([...state.collapsedFolderKeys]));
  } catch (_) { /* unavailable */ }
}

function createSessionGroup(name, sessions, folder = null) {
  const key = folderGroupKey(folder);
  const group = document.createElement("section");
  group.className = "session-group";
  group.dataset.folderKey = key;
  const heading = document.createElement("div");
  heading.className = "session-group-heading";
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "folder-toggle-button";
  const toggleIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  toggleIcon.setAttribute("viewBox", "0 0 24 24");
  toggleIcon.setAttribute("aria-hidden", "true");
  toggleIcon.setAttribute("focusable", "false");
  const togglePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  togglePath.setAttribute("d", "m7 9 5 5 5-5");
  toggleIcon.append(togglePath);
  toggle.append(toggleIcon);
  const label = document.createElement("h3");
  label.id = `session-group-heading-${key}`;
  label.textContent = name;
  group.setAttribute("aria-labelledby", label.id);
  const items = document.createElement("div");
  items.id = `session-group-items-${key}`;
  items.className = "session-group-items";
  items.append(...sessions.map(createSessionRow));
  toggle.setAttribute("aria-controls", items.id);
  const applyCollapsed = (collapsed) => {
    group.dataset.collapsed = String(collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", `${collapsed ? "Expand" : "Collapse"} folder ${name}`);
    toggle.title = `${collapsed ? "Expand" : "Collapse"} folder`;
    items.hidden = collapsed;
  };
  applyCollapsed(state.collapsedFolderKeys.has(key));
  toggle.addEventListener("click", () => {
    const collapsed = toggle.getAttribute("aria-expanded") === "true";
    if (collapsed) state.collapsedFolderKeys.add(key);
    else state.collapsedFolderKeys.delete(key);
    applyCollapsed(collapsed);
    persistCollapsedFolders();
  });
  heading.append(toggle, label);
  if (folder) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "folder-delete-button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove folder ${folder.name}`);
    remove.addEventListener("click", () => deleteFolder(folder));
    heading.append(remove);
  }
  group.append(heading, items);
  return group;
}

function renderSessions() {
  sessionList.replaceChildren();
  const unfiled = state.sessions.filter((session) => session.folder_id === null);
  if (unfiled.length) sessionList.append(createSessionGroup("No folder", unfiled));
  state.folders.forEach((folder) => {
    const sessions = state.sessions.filter((session) => session.folder_id === folder.id);
    sessionList.append(createSessionGroup(folder.name, sessions, folder));
  });
  noSessions.hidden = state.sessions.length > 0 || state.folders.length > 0;
  noSessions.textContent = "No conversations yet.";
}

async function loadSessions() {
  const [sessionResult, folderResult] = await Promise.all([
    api("/api/agent/sessions"), api("/api/agent/folders"),
  ]);
  state.sessions = sessionResult.sessions;
  state.folders = folderResult.folders;
  renderSessions();
}

function createTimestamp(value, className = "message-timestamp") {
  const time = document.createElement("time");
  time.className = className;
  time.dateTime = value || "";
  time.textContent = className === "session-timestamp" ? conversationTimestamp(value) : absoluteTimestamp(value);
  time.title = absoluteTimestamp(value);
  return time;
}

function appendMessageTimestamp(item, message) {
  if (message.created_at) item.append(createTimestamp(message.created_at));
}

function renderWaitingIndicator() {
  let indicator = messageList.querySelector(".agent-waiting");
  if (!state.busy) {
    indicator?.remove();
    return;
  }
  messageList.querySelector(".agent-empty")?.remove();
  if (!indicator) {
    indicator = document.createElement("div");
    indicator.className = "agent-message assistant agent-waiting";
    indicator.setAttribute("role", "status");
    indicator.setAttribute("aria-label", "Waiting for LLM response");
    indicator.setAttribute("aria-live", "polite");
    indicator.setAttribute("aria-atomic", "true");

    const dots = document.createElement("span");
    dots.className = "agent-waiting-dots";
    dots.setAttribute("aria-hidden", "true");
    dots.append(document.createElement("span"), document.createElement("span"), document.createElement("span"));

    const label = document.createElement("span");
    label.textContent = "Waiting for LLM response…";
    indicator.append(dots, label);
  }
  messageList.append(indicator);
  messageList.scrollTop = messageList.scrollHeight;
  updateConversationScrollShadows();
}

function openMoveConversation(session) {
  state.movingSessionId = session.id;
  moveConversationFolder.replaceChildren(new Option("No folder", ""));
  state.folders.forEach((folder) => {
    moveConversationFolder.append(new Option(folder.name, String(folder.id)));
  });
  moveConversationFolder.value = session.folder_id === null ? "" : String(session.folder_id);
  moveConversationDialog.showModal();
}

function setMoveConversationPending(pending) {
  moveConversationForm.setAttribute("aria-busy", String(pending));
  moveConversationFolder.disabled = pending;
  moveConversationCancel.disabled = pending;
  moveConversationSubmit.disabled = pending;
}

function closeMoveConversation() {
  state.movingSessionId = null;
  moveConversationDialog.close();
}

function createToolCall(call, runStatus) {
  const view = toolCallState(call, runStatus);
  const details = document.createElement("details");
  details.className = "agent-message tool";
  if (view.id) details.dataset.toolCallId = view.id;

  const summary = document.createElement("summary");
  summary.textContent = view.summary;

  const body = document.createElement("div");
  body.className = "tool-details";
  const callLabel = document.createElement("p");
  callLabel.className = "tool-detail-label";
  callLabel.textContent = "Call";
  const callValue = document.createElement("pre");
  callValue.textContent = formattedToolValue(view.arguments);
  const responseLabel = document.createElement("p");
  responseLabel.className = "tool-detail-label";
  responseLabel.textContent = "Response";
  const responseValue = document.createElement("pre");
  responseValue.className = "tool-response pending";
  responseValue.textContent = "Awaiting tool response…";
  body.append(callLabel, callValue, responseLabel, responseValue);
  details.append(summary, body);
  return { details, name: view.name, summary, responseValue };
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
        appendMessageTimestamp(item, message);
        messageList.append(item);
        rendered += 1;
      }
      (Array.isArray(message.tool_calls) ? message.tool_calls : []).forEach((call) => {
        const view = createToolCall(call, runStatus);
        appendMessageTimestamp(view.details, message);
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
        appendMessageTimestamp(details, message);
        messageList.append(details);
        rendered += 1;
      }
      return;
    }
    if (message.role === "user" && typeof message.content === "string" && message.content.trim()) {
      const item = document.createElement("div");
      item.className = "agent-message user";
      renderMarkdown(item, message.content);
      appendMessageTimestamp(item, message);
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
  renderWaitingIndicator();
  state.messageSignature = signature;
  messageList.scrollTop = messageList.scrollHeight;
  updateConversationScrollShadows();
}

function appendUserMessage(content) {
  state.messageSignature = null;
  messageList.querySelector(".agent-empty")?.remove();
  const item = document.createElement("div");
  item.className = "agent-message user";
  renderMarkdown(item, content);
  item.append(createTimestamp(new Date().toISOString()));
  messageList.append(item);
  messageList.scrollTop = messageList.scrollHeight;
  updateConversationScrollShadows();
  return item;
}

function renderApprovals(approvals, runId = null) {
  approvalList.replaceChildren();
  const view = approvalState(approvals, runId);
  const { pending } = view;
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
  if (view.canResume) {
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
    state.currentSession = detail.session;
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
    setBusy(false);
  } catch (error) { showError(error); }
}

function pollSessionMessages(sessionId) {
  return startSessionPolling({
    sessionId,
    currentSessionId: () => state.sessionId,
    load: () => api(`/api/agent/sessions/${sessionId}`),
    render: (detail) => {
      renderMessages(detail.messages, detail.runs.at(-1)?.status);
    },
  });
}

async function apiWithSessionPolling(sessionId, url, options) {
  const stopPolling = pollSessionMessages(sessionId);
  try {
    return await api(url, options);
  } finally {
    stopPolling();
  }
}

function resetConversation() {
  state.sessionId = null;
  state.currentSession = null;
  renderSessions();
  renderMessages([]);
  renderApprovals([]);
  conversationTitle.textContent = "New conversation";
  setBusy(false);
  messageInput.focus();
}

document.querySelector("#new-conversation").addEventListener("click", resetConversation);

newFolderButton.addEventListener("click", async () => {
  const name = window.prompt("Folder name");
  if (name === null || !name.trim()) return;
  clearError();
  try {
    await api("/api/agent/folders", {
      method: "POST",
      body: JSON.stringify({ name: name.trim() }),
    });
    await loadSessions();
    notify("Folder created");
  } catch (error) { showError(error); }
});

async function deleteFolder(folder) {
  const message = `Remove "${folder.name}"? Conversations in it will move to No folder.`;
  if (!window.confirm(message)) return;
  clearError();
  try {
    await api(`/api/agent/folders/${folder.id}`, { method: "DELETE" });
    await loadSessions();
    notify("Folder removed");
  } catch (error) { showError(error); }
}

moveConversationCancel.addEventListener("click", closeMoveConversation);

moveConversationDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  moveConversationCancel.click();
});

moveConversationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.movingSessionId) return;
  const sessionId = state.movingSessionId;
  const folderId = moveConversationFolder.value ? Number(moveConversationFolder.value) : null;
  state.movingSessionId = null;
  setMoveConversationPending(true);
  clearError();
  try {
    await api(`/api/agent/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ folder_id: folderId }),
    });
    closeMoveConversation();
    await loadSessions();
    notify("Conversation moved");
  } catch (error) {
    state.movingSessionId = sessionId;
    showError(error);
  } finally {
    setMoveConversationPending(false);
  }
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
  state.currentSession = result.session;
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
