import { requestJson as api } from "./http.js";
import {
  compareRank,
  compareSmart,
  appViewNavigation,
  formatFileSize,
  dateTimeInTimezone,
  dragInsertionRow,
  focusViewState,
  keyboardRankMove,
  mergeVisibleOrder,
  multiSelectState,
  navigationLockState,
  normalizeSortMode,
  normalizeTheme,
  nextTask,
  pendingFollowUpBecameDue as hasPendingFollowUpBecomeDue,
  rankNeighbors,
  sameTaskOrder,
  taskMatchesFilters,
  uploadFilename,
  validateAttachments,
} from "./task_logic.js";

const toast = document.querySelector("#toast");
const taskWorkspace = document.querySelector("#task-workspace");
const agentWorkspace = document.querySelector("#agent-workspace");
const viewNavigation = document.querySelector(".app-view-nav");
const viewNavigationLockMessage = document.querySelector("#view-navigation-lock-message");
const viewNavigationLockCopy = document.querySelector("#view-navigation-lock-copy");
const appViewLinks = [...document.querySelectorAll("[data-app-view]")];
const maxAttachmentBytes = Number(document.body.dataset.maxAttachmentBytes);
const maxAttachmentsPerTask = Number(document.body.dataset.maxAttachments);
const maxUploadBytes = Number(document.body.dataset.maxUploadBytes);
const themeStorageKey = "aur-bataao-theme";
const themeToggle = document.querySelector("#theme-toggle");
const darkModePreference = window.matchMedia("(prefers-color-scheme: dark)");
let toastTimer;
let currentAppView = "focus";
let lastTaskView = "focus";
let tasksStale = false;
let pendingTaskWrites = 0;
let agentMutationPending = false;
const dirtyTaskControls = new Set();
const taskControlBaselines = new WeakMap();

function savedTheme() {
  try {
    return normalizeTheme(localStorage.getItem(themeStorageKey));
  } catch (_) {
    return "";
  }
}

function applyTheme(theme, persist = false) {
  document.documentElement.dataset.theme = theme;
  const dark = theme === "dark";
  const nextTheme = dark ? "light" : "dark";
  themeToggle.setAttribute("aria-checked", String(dark));
  themeToggle.setAttribute("aria-label", `Switch to ${nextTheme} mode`);
  themeToggle.title = `Switch to ${nextTheme} mode`;
  if (persist) {
    try { localStorage.setItem(themeStorageKey, theme); } catch (_) { /* storage unavailable */ }
  }
}

applyTheme(document.documentElement.dataset.theme || (darkModePreference.matches ? "dark" : "light"));
themeToggle.addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
});
darkModePreference.addEventListener("change", (event) => {
  if (!savedTheme()) applyTheme(event.matches ? "dark" : "light");
});

function notify(message, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = error ? "show error" : "show";
  toastTimer = setTimeout(() => { toast.className = ""; }, 2600);
}

function taskRow(element) {
  return element.closest(".task-row");
}

function clipboardFiles(event) {
  return [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

function attachmentError(files, existingCount = 0, pendingFiles = []) {
  return validateAttachments(files, {
    existingCount,
    pendingFiles,
    maxAttachmentBytes,
    maxAttachmentsPerTask,
    maxUploadBytes,
  });
}

function bindDropzone(zone, onFiles) {
  const input = zone.querySelector(".attachment-input");
  const chooser = zone.querySelector(".choose-attachments");

  chooser.addEventListener("click", () => input.click());
  zone.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("button")) zone.focus();
  });
  input.addEventListener("change", () => {
    const selectedFiles = [...input.files];
    input.value = "";
    if (selectedFiles.length) onFiles(selectedFiles);
  });

  zone.addEventListener("keydown", (event) => {
    if (event.target === zone && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      input.click();
    }
  });
  zone.addEventListener("dragenter", (event) => {
    event.preventDefault();
    zone.classList.add("is-dragging");
  });
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    zone.classList.add("is-dragging");
  });
  zone.addEventListener("dragleave", (event) => {
    if (!zone.contains(event.relatedTarget)) zone.classList.remove("is-dragging");
  });
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-dragging");
    const files = [...(event.dataTransfer?.files || [])];
    if (files.length) onFiles(files);
  });
  zone.addEventListener("paste", (event) => {
    const files = clipboardFiles(event);
    if (files.length) {
      event.preventDefault();
      onFiles(files);
    }
  });
}

function submitTaskAttachments(zone, files) {
  const row = taskRow(zone);
  const selectionError = attachmentError(files, Number(zone.dataset.attachmentCount));
  if (selectionError) {
    notify(selectionError, true);
    return;
  }
  if (typeof DataTransfer === "undefined") {
    notify("Use Choose files to attach files in this browser", true);
    return;
  }
  const transfer = new DataTransfer();
  files.forEach((file, index) => transfer.items.add(new File(
    [file],
    uploadFilename(file, index),
    { type: file.type, lastModified: file.lastModified },
  )));
  zone.querySelector(".attachment-input").files = transfer.files;
  zone.classList.add("is-uploading");
  zone.querySelectorAll("button").forEach((control) => { control.disabled = true; });
  zone.closest("form").requestSubmit();
}
window.addEventListener("app:notify", (event) => {
  notify(event.detail?.message || "", Boolean(event.detail?.error));
});

window.addEventListener("beforeunload", (event) => {
  if (dirtyTaskControls.size === 0) return;
  event.preventDefault();
  event.returnValue = "";
});

function taskEditControl(control) {
  if (!(control instanceof HTMLInputElement
    || control instanceof HTMLTextAreaElement
    || control instanceof HTMLSelectElement)) return false;
  if (!control.closest("#task-workspace .task-card, #new-task-dialog, #follow-up-dialog")) return false;
  if (control.matches('[type="hidden"], [type="file"], .status-select, .due-date, [data-task-label-option]')) return false;
  return true;
}

function taskControlValue(control) {
  if (control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)) {
    return String(control.checked);
  }
  return control.value;
}

function setTaskControlBaseline(control) {
  taskControlBaselines.set(control, taskControlValue(control));
  dirtyTaskControls.delete(control);
  updateViewNavigationLock();
}

function updateTaskControlDirtyState(control) {
  if (!taskControlBaselines.has(control)) {
    taskControlBaselines.set(control, control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)
      ? String(control.defaultChecked)
      : control.defaultValue);
  }
  const dirty = taskControlValue(control) !== taskControlBaselines.get(control);
  if (dirty) dirtyTaskControls.add(control);
  else dirtyTaskControls.delete(control);
  updateViewNavigationLock();
}

function viewNavigationLocked() {
  return navigationLockState(
    dirtyTaskControls.size,
    pendingTaskWrites,
    agentMutationPending,
  ).locked;
}

function updateViewNavigationLock(announce = false) {
  const { locked, copy } = navigationLockState(
    dirtyTaskControls.size,
    pendingTaskWrites,
    agentMutationPending,
  );
  viewNavigationLockCopy.textContent = copy;
  viewNavigation.classList.toggle("is-edit-locked", locked);
  viewNavigationLockMessage.hidden = !locked;
  appViewLinks.forEach((link) => {
    if (locked) {
      link.setAttribute("aria-disabled", "true");
      link.setAttribute("aria-describedby", viewNavigationLockMessage.id);
    } else {
      link.removeAttribute("aria-disabled");
      link.removeAttribute("aria-describedby");
    }
  });
  if (announce && locked) notify(agentMutationPending
    ? "Wait for the Agent task update to finish"
    : "Finish editing before switching views");
}

window.addEventListener("agent:task-mutation-start", () => {
  agentMutationPending = true;
  updateViewNavigationLock();
});
window.addEventListener("agent:task-mutation-end", () => {
  agentMutationPending = false;
  updateViewNavigationLock();
});
window.addEventListener("agent:tasks-mutated", () => {
  tasksStale = true;
});

document.addEventListener("focusin", (event) => {
  if (taskEditControl(event.target) && !taskControlBaselines.has(event.target)) {
    taskControlBaselines.set(event.target, taskControlValue(event.target));
  }
});
document.addEventListener("input", (event) => {
  if (taskEditControl(event.target)) updateTaskControlDirtyState(event.target);
});
document.addEventListener("change", (event) => {
  if (taskEditControl(event.target)) updateTaskControlDirtyState(event.target);
});
document.addEventListener("reset", (event) => {
  queueMicrotask(() => {
    event.target.querySelectorAll("input, textarea, select").forEach((control) => {
      if (taskEditControl(control)) setTaskControlBaseline(control);
    });
  });
});
viewNavigation.addEventListener("pointerdown", (event) => {
  if (!viewNavigationLocked() || !event.target.closest("[data-app-view]")) return;
  event.preventDefault();
  updateViewNavigationLock(true);
});

function renderLabels(row, labels) {
  const detailList = row.querySelector(".task-details .label-list");
  detailList.replaceChildren(...labels.map((label) => {
    const chip = document.createElement("span");
    chip.className = `label-chip ${label.type}`;
    chip.dataset.labelId = label.id;
    chip.append(document.createTextNode(`${label.name} `));
    const form = document.createElement("form");
    form.className = "inline-action-form";
    form.method = "post";
    form.action = `/tasks/${row.dataset.taskId}/labels/${label.id}/remove`;
    const returnView = document.createElement("input");
    returnView.type = "hidden";
    returnView.name = "return_view";
    returnView.value = currentTaskView();
    const expand = document.createElement("input");
    expand.type = "hidden";
    expand.name = "expand";
    expand.value = "1";
    const button = document.createElement("button");
    button.type = "submit";
    button.title = "Remove label";
    button.setAttribute("aria-label", `Remove ${label.name}`);
    button.textContent = "×";
    form.append(returnView, expand, button);
    chip.append(form);
    return chip;
  }));

  const summaryList = row.querySelector(".task-label-list");
  summaryList.replaceChildren(...labels.map((label) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `label-chip task-label-filter ${label.type}`;
    chip.dataset.labelId = label.id;
    chip.title = `Show tasks labelled ${label.name}`;
    chip.setAttribute("aria-label", `Filter tasks by ${label.name}`);
    chip.setAttribute("aria-pressed", "false");
    chip.textContent = label.name;
    return chip;
  }));
  row.dataset.labelIds = labels.map((label) => label.id).join(" ");
  syncTaskLabelPicker(row, labels);
  syncLabelFilterOptions();
}

function syncTaskLabelPicker(row, labels) {
  const picker = row.querySelector(".task-label-picker");
  if (!picker) return;
  const assignedLabels = new Set(labels.map((label) => String(label.id)));
  picker.querySelectorAll("[data-task-label-option]").forEach((checkbox) => {
    checkbox.checked = assignedLabels.has(checkbox.value);
  });
  updateMultiSelect(picker);
}

function removeRenderedLabel(row, labelId) {
  row.querySelector(`.task-details .label-chip[data-label-id="${labelId}"]`)?.remove();
  row.querySelector(`.task-label-filter[data-label-id="${labelId}"]`)?.remove();
  row.dataset.labelIds = [...row.querySelectorAll(".task-label-filter")]
    .map((label) => label.dataset.labelId)
    .join(" ");
  const pickerOption = row.querySelector(`.task-label-picker [data-task-label-option][value="${labelId}"]`);
  if (pickerOption) pickerOption.checked = false;
  if (pickerOption) updateMultiSelect(pickerOption.closest(".task-label-picker"));
  syncLabelFilterOptions();
  applyFilters();
}

function applyTask(row, task) {
  const unresolvedBlockerIds = (task.blocked_by || [])
    .filter((blocker) => !blocker.resolved)
    .map((blocker) => String(blocker.id));
  row.dataset.status = task.status;
  row.dataset.dueDate = task.due_date || "";
  row.dataset.overdue = String(Boolean(task.overdue));
  row.dataset.stalled = String(Boolean(task.stalled));
  row.dataset.followUpDue = String(Boolean(task.follow_up_due));
  row.dataset.followUpDate = task.waiting?.next_follow_up_on || "";
  row.dataset.followUpTime = task.waiting?.effective_follow_up_time || "09:00";
  row.dataset.followUpExactTime = task.waiting?.next_follow_up_time || "";
  row.dataset.waitingOn = task.waiting?.person_name || "";
  row.dataset.actionable = String(
    task.follow_up_due
    || (["todo", "in_progress"].includes(task.status) && !task.blocked),
  );
  row.dataset.blocked = String(Boolean(task.blocked));
  row.dataset.unresolvedBlockerIds = unresolvedBlockerIds.join(" ");
  row.classList.remove("status-todo", "status-in_progress", "status-done");
  row.classList.add(`status-${task.status}`);
  row.classList.toggle("is-blocked", Boolean(task.blocked));
  row.querySelector(".focus-status-badge").textContent = {
    todo: "To do",
    in_progress: "In progress",
    done: "Done",
  }[task.status];
  if (task.labels) renderLabels(row, task.labels);
  updateActiveTaskCount();
  applyFilters();
  sortTasks();
  if (document.body.classList.contains("focus-mode")) renderFocusView();
}

function updateActiveTaskCount() {
  const countLabel = document.querySelector("#active-task-count");
  if (!countLabel) return;
  const tasks = [...document.querySelectorAll(".task-row")];
  const inProgressCount = tasks.filter((task) => task.dataset.status === "in_progress").length;
  const followUpCount = tasks.filter((task) => task.dataset.followUpDue === "true").length;
  const summaries = [];
  if (inProgressCount) summaries.push(`${inProgressCount} in progress`);
  if (followUpCount) {
    summaries.push(`${followUpCount} ${followUpCount === 1 ? "needs" : "need"} follow-up`);
  }
  countLabel.textContent = summaries.join(" · ");
  countLabel.hidden = summaries.length === 0;
}

function expandTaskDetails(row) {
  const details = row.querySelector(".task-details");
  details.hidden = false;
  row.querySelectorAll(".toggle-details, .focus-toggle-details").forEach((control) => {
    control.setAttribute("aria-expanded", "true");
  });
  const manageToggle = row.querySelector(".toggle-details");
  manageToggle.setAttribute("aria-label", "Hide task details");
  const focusToggle = row.querySelector(".focus-toggle-details");
  focusToggle.setAttribute("aria-label", "Collapse task details and editing controls");
  focusToggle.title = focusToggle.getAttribute("aria-label");
}

async function patchControl(control) {
  const row = taskRow(control);
  const field = control.dataset.field;
  const previous = control.dataset.previous ?? control.defaultValue;
  const value = control.value;
  if (value === previous) return;
  control.dataset.previous = value;
  pendingTaskWrites += 1;
  updateViewNavigationLock();
  try {
    const result = await api(`/api/tasks/${row.dataset.taskId}`, {
      method: "PATCH",
      body: JSON.stringify({ [field]: value }),
    });
    if (field === "title") {
      row.dataset.title = result.task.title.toLowerCase();
      row.querySelector(".focus-task-title").textContent = result.task.title;
    }
    row.querySelectorAll(`[data-field="${field}"]`).forEach((fieldControl) => {
      fieldControl.value = result.task[field] ?? "";
      fieldControl.dataset.previous = fieldControl.value;
    });
    applyTask(row, result.task);
    setTaskControlBaseline(control);
  } catch (error) {
    control.value = previous;
    control.dataset.previous = previous;
    setTaskControlBaseline(control);
    notify(error.message, true);
  } finally {
    pendingTaskWrites = Math.max(0, pendingTaskWrites - 1);
    updateViewNavigationLock();
  }
}

document.querySelectorAll("[data-field]").forEach((control) => {
  control.dataset.previous = control.value;
  if (control.dataset.field === "status") {
    control.addEventListener("change", () => {
      const returnView = control.form.querySelector(".return-view-field");
      if (returnView) returnView.value = currentTaskView();
      control.form.requestSubmit();
    });
    return;
  }
  control.addEventListener(
    control.tagName === "TEXTAREA" || control.dataset.field === "title" ? "blur" : "change",
    () => patchControl(control),
  );
});
document.querySelectorAll(".inline-field-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const control = form.querySelector("[data-field]");
    if (control?.dataset.field === "status") return;
    event.preventDefault();
    if (control) patchControl(control);
  });
});

document.addEventListener("click", async (event) => {
  const toggle = event.target.closest(".toggle-details, .focus-toggle-details");
  if (toggle) {
    const row = taskRow(toggle);
    const details = row.querySelector(".task-details");
    const opening = details.hidden;
    if (opening) {
      expandTaskDetails(row);
    } else {
      details.hidden = true;
      row.querySelectorAll(".toggle-details, .focus-toggle-details").forEach((control) => {
        control.setAttribute("aria-expanded", "false");
      });
      const manageToggle = row.querySelector(".toggle-details");
      manageToggle.setAttribute("aria-label", "Show task details");
      const focusToggle = row.querySelector(".focus-toggle-details");
      focusToggle.setAttribute("aria-label", "Expand task details and editing controls");
      focusToggle.title = focusToggle.getAttribute("aria-label");
    }
    return;
  }

  const labelChip = event.target.closest(".task-label-filter");
  if (labelChip) {
    const labelMenu = document.querySelector("#label-filter");
    labelMenu.querySelectorAll("[data-filter-value]").forEach((checkbox) => {
      checkbox.checked = checkbox.value === labelChip.dataset.labelId;
    });
    labelChipFilter = true;
    updateMultiSelect(labelMenu);
    applyFilters();
    return;
  }

});

const newTaskDialog = document.querySelector("#new-task-dialog");
const newTaskForm = document.querySelector("#new-task-form");
const newTaskInput = document.querySelector("#new-task-input");
const newTaskSubmit = newTaskForm.querySelector("button[type='submit']");
const pendingAttachmentList = document.querySelector("#pending-attachments");
let pendingAttachments = [];
let newTaskReturnView = "focus";

function currentTaskView() {
  if (currentAppView === "manage" || currentAppView === "blocked") {
    return blockedView ? "blocked" : "manage";
  }
  if (currentAppView === "focus") return "focus";
  return lastTaskView;
}

function showTaskView(view) {
  if (view === "manage" || view === "blocked") {
    showManageView({ blockedOnly: view === "blocked" });
  } else {
    showFocusView();
  }
}

document.addEventListener("submit", (event) => {
  const returnView = event.target.elements?.namedItem("return_view");
  if (returnView) returnView.value = currentTaskView();
});

function renderPendingAttachments() {
  pendingAttachmentList.replaceChildren(...pendingAttachments.map((file, index) => {
    const item = document.createElement("li");
    item.className = "pending-attachment";

    const details = document.createElement("span");
    details.append(document.createTextNode(file.name || "clipboard image"));
    const size = document.createElement("small");
    size.textContent = formatFileSize(file.size);
    details.append(size);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${file.name || "clipboard image"}`);
    remove.addEventListener("click", () => {
      pendingAttachments.splice(index, 1);
      renderPendingAttachments();
    });
    item.append(details, remove);
    return item;
  }));
  if (typeof DataTransfer !== "undefined") {
    const transfer = new DataTransfer();
    pendingAttachments.forEach((file, index) => transfer.items.add(new File(
      [file],
      uploadFilename(file, index),
      { type: file.type, lastModified: file.lastModified },
    )));
    document.querySelector("#new-task-attachments").files = transfer.files;
  }
}

function stageAttachments(files) {
  const knownFiles = new Set(
    pendingAttachments.map((file) => `${file.name}:${file.size}:${file.lastModified}`),
  );
  const additions = [];
  files.forEach((file) => {
    const key = `${file.name}:${file.size}:${file.lastModified}`;
    if (!knownFiles.has(key)) {
      knownFiles.add(key);
      additions.push(file);
    }
  });
  const selectionError = attachmentError(additions, 0, pendingAttachments);
  if (selectionError) {
    notify(selectionError, true);
    return;
  }
  pendingAttachments.push(...additions);
  renderPendingAttachments();
}

function updateNewTaskSubmit() {
  newTaskSubmit.disabled = !newTaskInput.value.trim() || newTaskForm.getAttribute("aria-busy") === "true";
}

document.querySelectorAll(".open-task-dialog").forEach((button) => {
  button.addEventListener("click", () => {
    newTaskReturnView = currentTaskView();
    newTaskDialog.showModal();
    requestAnimationFrame(() => newTaskInput.focus());
  });
});
newTaskDialog.querySelector(".dialog-close").addEventListener("click", () => newTaskDialog.close());
newTaskDialog.querySelector(".cancel-new-task").addEventListener("click", () => newTaskDialog.close());
newTaskDialog.addEventListener("click", (event) => {
  if (event.target === newTaskDialog) newTaskDialog.close();
});
newTaskDialog.addEventListener("close", () => {
  showTaskView(newTaskReturnView);
  newTaskForm.reset();
  newTaskForm.removeAttribute("aria-busy");
  pendingAttachments = [];
  renderPendingAttachments();
  updateNewTaskSubmit();
});
newTaskInput.addEventListener("input", updateNewTaskSubmit);
bindDropzone(document.querySelector("#new-task-dropzone"), stageAttachments);
newTaskDialog.addEventListener("paste", (event) => {
  if (document.querySelector("#new-task-dropzone").contains(event.target)) return;
  const files = clipboardFiles(event);
  if (files.length) {
    event.preventDefault();
    stageAttachments(files);
  }
});

newTaskForm.addEventListener("submit", () => {
  newTaskForm.setAttribute("aria-busy", "true");
  updateNewTaskSubmit();
});

document.querySelectorAll(".task-attachment-dropzone").forEach((zone) => {
  bindDropzone(zone, (files) => submitTaskAttachments(zone, files));
  taskRow(zone).querySelector(".task-details").addEventListener("paste", (event) => {
    if (zone.contains(event.target)) return;
    const files = clipboardFiles(event);
    if (files.length) {
      event.preventDefault();
      submitTaskAttachments(zone, files);
    }
  });
});

document.querySelectorAll(".task-label-picker").forEach((picker) => {
  picker.addEventListener("change", async (event) => {
    const checkbox = event.target.closest("[data-task-label-option]");
    if (!checkbox) return;
    const row = taskRow(picker);
    const adding = checkbox.checked;
    checkbox.disabled = true;
    updateMultiSelect(picker);
    try {
      if (adding) {
        const result = await api(`/api/tasks/${row.dataset.taskId}/labels`, {
          method: "POST",
          body: JSON.stringify({ name: checkbox.dataset.labelName }),
        });
        applyTask(row, result.task);
      } else {
        await api(`/api/tasks/${row.dataset.taskId}/labels/${checkbox.value}`, { method: "DELETE" });
        removeRenderedLabel(row, checkbox.value);
      }
    } catch (error) {
      checkbox.checked = !adding;
      notify(error.message, true);
    } finally {
      checkbox.disabled = false;
      updateMultiSelect(picker);
    }
  });
});


const followUpDialog = document.querySelector("#follow-up-dialog");
const followUpForm = document.querySelector("#follow-up-form");
const followUpPerson = document.querySelector("#follow-up-dialog-person");

function setExactTimeVisibility(container, visible, focus = false) {
  const input = container.querySelector('input[name="next_follow_up_time"]');
  container.querySelectorAll(".exact-time-field").forEach((field) => {
    field.hidden = !visible;
  });
  const toggle = container.querySelector(".toggle-follow-up-time");
  toggle.textContent = visible ? "Remove time" : "+ Add time";
  toggle.setAttribute("aria-expanded", String(visible));
  if (visible && !input.value) input.value = "09:00";
  if (!visible) input.value = "";
  if (visible && focus) input.focus();
}

document.querySelectorAll(".follow-up-time-control").forEach((container) => {
  const toggle = container.querySelector(".toggle-follow-up-time");
  const dateInput = container.querySelector('input[name="next_follow_up_on"]');
  toggle.addEventListener("click", () => {
    const showing = toggle.getAttribute("aria-expanded") === "true";
    if (!showing && !dateInput.value) {
      notify("Choose a follow-up date before adding a time", true);
      dateInput.focus();
      return;
    }
    setExactTimeVisibility(container, !showing, !showing);
  });
  dateInput.addEventListener("change", () => {
    if (!dateInput.value) setExactTimeVisibility(container, false);
  });
});

document.querySelectorAll(".followed-up").forEach((button) => {
  button.addEventListener("click", () => {
    const row = taskRow(button);
    followUpForm.reset();
    followUpForm.elements.task_id.value = row.dataset.taskId;
    followUpForm.action = `/tasks/${row.dataset.taskId}/follow-ups`;
    followUpForm.elements.return_view.value = currentTaskView();
    followUpForm.elements.next_follow_up_time.value = row.dataset.followUpExactTime;
    setExactTimeVisibility(followUpForm, Boolean(row.dataset.followUpExactTime));
    followUpPerson.textContent = `Waiting on ${button.dataset.personName || row.dataset.waitingOn}`;
    followUpDialog.showModal();
    requestAnimationFrame(() => followUpForm.elements.note.focus());
  });
});

followUpDialog.querySelector(".dialog-close").addEventListener("click", () => followUpDialog.close());
followUpDialog.querySelector(".cancel-follow-up").addEventListener("click", () => followUpDialog.close());
followUpDialog.addEventListener("click", (event) => {
  if (event.target === followUpDialog) followUpDialog.close();
});
followUpDialog.addEventListener("close", () => {
  followUpForm.reset();
  followUpForm.querySelector("button[type='submit']").disabled = false;
});
followUpForm.addEventListener("submit", () => {
  const button = followUpForm.querySelector("button[type='submit']");
  button.disabled = true;
});

const searchFilter = document.querySelector("#search-filter");
const statusFilter = document.querySelector("#status-filter");
const labelFilter = document.querySelector("#label-filter");
const overdueFilter = document.querySelector("#overdue-filter");
const stalledFilter = document.querySelector("#stalled-filter");
const followUpFilter = document.querySelector("#follow-up-filter");
const blockedViewIndicator = document.querySelector("#blocked-view-indicator");
let blockedView = false;
let labelChipFilter = false;

function multiSelectOptions(filter) {
  return [...filter.querySelectorAll("[data-filter-value], [data-task-label-option]")];
}

function selectedFilterValues(filter) {
  return new Set(
    multiSelectOptions(filter)
      .filter((checkbox) => checkbox.checked)
      .map((checkbox) => checkbox.value),
  );
}

function updateMultiSelect(filter) {
  const options = multiSelectOptions(filter);
  const selectedCount = options.filter((checkbox) => checkbox.checked).length;
  const view = multiSelectState(options.length, selectedCount, {
    all: filter.dataset.allLabel,
    empty: filter.dataset.emptyLabel,
    singular: filter.dataset.singular,
    plural: filter.dataset.plural,
  });
  const selectAll = filter.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.checked = view.allSelected;
    selectAll.indeterminate = view.indeterminate;
  }
  filter.querySelector(".multi-select-summary").textContent = view.summary;
}

document.querySelectorAll(".multi-select").forEach((select) => {
  updateMultiSelect(select);
  select.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      select.open = false;
      select.querySelector("summary").focus();
    }
  });
});

document.querySelectorAll(".filter-multi-select").forEach((filter) => {
  filter.addEventListener("change", (event) => {
    if (event.target.matches("[data-select-all]")) {
      multiSelectOptions(filter).forEach((checkbox) => {
        checkbox.checked = event.target.checked;
      });
    }
    if (filter === labelFilter) labelChipFilter = false;
    blockedView = false;
    blockedViewIndicator.hidden = true;
    updateMultiSelect(filter);
    applyFilters();
  });
});

document.addEventListener("click", (event) => {
  document.querySelectorAll(".multi-select[open]").forEach((filter) => {
    if (!filter.contains(event.target)) filter.open = false;
  });
});

function syncLabelFilterOptions() {
  const currentOptions = multiSelectOptions(labelFilter);
  const selectedLabels = selectedFilterValues(labelFilter);
  const hadAllSelected = currentOptions.every((checkbox) => checkbox.checked);
  const labels = new Map();
  document.querySelectorAll(".task-label-filter").forEach((label) => {
    labels.set(label.dataset.labelId, label.textContent.trim());
  });
  const sortedLabels = [...labels.entries()]
    .sort((first, second) => first[1].localeCompare(second[1]));
  const selectedLabelStillExists = sortedLabels.some(([id]) => selectedLabels.has(id));
  const resetMissingSelection = selectedLabels.size > 0 && !selectedLabelStillExists;
  if (resetMissingSelection) labelChipFilter = false;
  const options = sortedLabels
    .map(([id, name]) => {
      const option = document.createElement("label");
      option.className = "multi-select-option";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = id;
      checkbox.dataset.filterValue = "";
      checkbox.checked = hadAllSelected || resetMissingSelection || selectedLabels.has(id);
      const text = document.createElement("span");
      text.textContent = name;
      option.append(checkbox, text);
      return option;
    });
  labelFilter.querySelector(".multi-select-options").replaceChildren(...options);
  updateMultiSelect(labelFilter);
}

function applyFilters() {
  const query = searchFilter.value.trim().toLowerCase();
  const statuses = selectedFilterValues(statusFilter);
  const labels = selectedFilterValues(labelFilter);
  const labelOptions = multiSelectOptions(labelFilter);
  const labelsAreFiltered = labelChipFilter || labels.size !== labelOptions.length;
  const overdueOnly = overdueFilter.checked;
  const stalledOnly = stalledFilter.checked;
  const followUpsOnly = followUpFilter.checked;
  let visibleTasks = 0;
  document.querySelectorAll(".task-row").forEach((row) => {
    row.hidden = !taskMatchesFilters({
      title: row.dataset.title,
      status: row.dataset.status,
      labelIds: row.dataset.labelIds,
      blocked: row.dataset.blocked === "true",
      overdue: row.dataset.overdue === "true",
      stalled: row.dataset.stalled === "true",
      followUpDue: row.dataset.followUpDue === "true",
    }, {
      query, statuses, labels, labelsAreFiltered, overdueOnly, stalledOnly,
      followUpsOnly, blockedOnly: blockedView,
    });
    if (!row.hidden) visibleTasks += 1;
    row.querySelectorAll(".task-label-filter").forEach((chip) => {
      chip.setAttribute("aria-pressed", String(labelsAreFiltered && labels.has(chip.dataset.labelId)));
    });
  });
  const emptyState = document.querySelector("#filter-empty-state");
  if (emptyState) emptyState.hidden = visibleTasks !== 0;
  updateRankPresentation();
}
[searchFilter, overdueFilter, stalledFilter, followUpFilter].forEach((control) => {
  control.addEventListener("input", () => {
    blockedView = false;
    blockedViewIndicator.hidden = true;
    applyFilters();
  });
});

const taskList = document.querySelector("#task-list");
const sortControl = document.querySelector("#sort-control");
const taskSortStorageKey = "aur-bataao-task-sort";
let reorderInFlight = false;
let dragState = null;

function taskRows() {
  return [...taskList.children].filter((child) => child.classList.contains("task-row"));
}

function renderTaskOrder(rows) {
  const anchor = [...taskList.children]
    .find((child) => !child.classList.contains("task-row")) || null;
  rows.forEach((row) => taskList.insertBefore(row, anchor));
}

let focusTaskId = document.querySelector(".task-row.is-focus-task")?.dataset.taskId || "";
const focusEmptyState = document.querySelector("#focus-empty-state");
const focusNextAction = document.querySelector("#focus-next-action");
const aurBataaoButton = document.querySelector("#aur-bataao-button");

function updateAppViewNavigation(view) {
  const currentView = appViewNavigation(view);
  const names = { focus: "Focused", manage: "All Tasks", agent: "Agent" };
  appViewLinks.forEach((link) => {
    if (link.dataset.appView === currentView) {
      link.setAttribute("aria-current", "page");
      link.setAttribute("aria-label", names[link.dataset.appView]);
    } else {
      link.removeAttribute("aria-current");
      link.setAttribute("aria-label", `Switch to ${names[link.dataset.appView]}`);
    }
  });
}

function setWorkspaceVisibility(showAgent) {
  taskWorkspace.hidden = showAgent;
  taskWorkspace.inert = showAgent;
  agentWorkspace.hidden = !showAgent;
  agentWorkspace.inert = !showAgent;
}

function announceViewChange(view) {
  window.dispatchEvent(new CustomEvent("app:viewchange", { detail: { view } }));
}

function actionableTaskRows() {
  return taskRows()
    .filter((row) => row.dataset.actionable === "true")
    .sort(compareSmart);
}

function renderFocusView(preferredTaskId = focusTaskId) {
  const candidates = actionableTaskRows();
  const view = focusViewState(candidates, preferredTaskId);
  const { current } = view;
  taskRows().forEach((row) => row.classList.toggle("is-focus-task", row === current));
  focusTaskId = current?.dataset.taskId || "";

  document.body.classList.toggle("has-actionable", Boolean(current));
  document.body.classList.toggle("focus-empty", !current);
  focusEmptyState.hidden = Boolean(current);
  focusNextAction.hidden = !current;
  aurBataaoButton.disabled = !view.canChooseAnother;
  aurBataaoButton.title = view.canChooseAnother
    ? "Suggest another task"
    : "No other actionable task right now";
}

function showAnotherTask(currentRow) {
  const candidates = actionableTaskRows();
  const next = nextTask(candidates, currentRow);
  if (!next) return;
  renderFocusView(next.dataset.taskId);
}

function showManageView({ blockedOnly = false } = {}) {
  document.body.classList.remove("focus-mode", "agent-mode");
  document.body.classList.add("manage-mode");
  blockedView = blockedOnly;
  currentAppView = blockedOnly ? "blocked" : "manage";
  lastTaskView = currentAppView;
  setWorkspaceVisibility(false);
  updateAppViewNavigation(currentAppView);
  blockedViewIndicator.hidden = !blockedOnly;
  applyFilters();
  document.title = "All Tasks · Aur Bataao";
  announceViewChange(currentAppView);
}

function showFocusView() {
  document.body.classList.remove("manage-mode", "agent-mode");
  document.body.classList.add("focus-mode");
  currentAppView = "focus";
  lastTaskView = "focus";
  setWorkspaceVisibility(false);
  updateAppViewNavigation("focus");
  renderFocusView();
  document.title = "Focused · Aur Bataao";
  announceViewChange("focus");
}

function showAgentView() {
  document.body.classList.remove("focus-mode", "manage-mode");
  document.body.classList.add("agent-mode");
  currentAppView = "agent";
  setWorkspaceVisibility(true);
  updateAppViewNavigation("agent");
  document.title = "Agent · Aur Bataao";
  announceViewChange("agent");
}

function showAppView(view) {
  if (view === "agent") showAgentView();
  else showTaskView(view);
}

appViewLinks.forEach((link) => {
  link.addEventListener("click", (event) => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (viewNavigationLocked()) {
      updateViewNavigationLock(true);
      return;
    }
    if (link.getAttribute("aria-current") === "page") return;
    const view = link.dataset.appView;
    if (view !== "agent" && tasksStale) {
      location.assign(link.href);
      return;
    }
    showAppView(view);
    history.pushState({ view }, "", link.href);
  });
});
window.addEventListener("popstate", () => {
  if (viewNavigationLocked()) {
    const currentUrl = new URL(location.href);
    currentUrl.searchParams.set("view", currentAppView);
    history.pushState({ view: currentAppView }, "", currentUrl);
    updateViewNavigationLock(true);
    return;
  }
  const view = new URL(location.href).searchParams.get("view") || "focus";
  if (view !== "agent" && tasksStale) {
    location.reload();
    return;
  }
  showAppView(["focus", "manage", "blocked", "agent"].includes(view) ? view : "focus");
});
document.querySelector("#view-blocked-tasks").addEventListener("click", () => {
  searchFilter.value = "";
  overdueFilter.checked = false;
  stalledFilter.checked = false;
  followUpFilter.checked = false;
  statusFilter.querySelectorAll("[data-filter-value]").forEach((checkbox) => {
    checkbox.checked = checkbox.value !== "done";
  });
  labelFilter.querySelectorAll("[data-filter-value]").forEach((checkbox) => {
    checkbox.checked = true;
  });
  labelChipFilter = false;
  updateMultiSelect(statusFilter);
  updateMultiSelect(labelFilter);
  showManageView({ blockedOnly: true });
});
document.querySelector("#clear-blocked-view").addEventListener("click", () => {
  blockedView = false;
  blockedViewIndicator.hidden = true;
  applyFilters();
});

aurBataaoButton.addEventListener("click", () => {
  const currentRow = taskRows().find((row) => row.classList.contains("is-focus-task"));
  if (currentRow) showAnotherTask(currentRow);
});

function updateRankPresentation() {
  const rankMode = sortControl.value === "rank";
  const visibleRows = taskRows().filter((row) => !row.hidden);
  const ranksByTask = new Map(
    visibleRows.sort(compareRank).map((row, index) => [row.dataset.taskId, index + 1]),
  );
  taskRows().forEach((row) => {
    const rank = ranksByTask.get(row.dataset.taskId);
    const badge = row.querySelector(".rank-badge");
    const handle = row.querySelector(".rank-handle");
    const title = row.querySelector(".task-title").value;
    badge.textContent = rank === undefined ? "" : `#${rank}`;
    badge.hidden = !rankMode || rank === undefined;
    handle.disabled = !rankMode || rank === undefined || reorderInFlight;
    handle.draggable = rankMode && rank !== undefined && !reorderInFlight;
    handle.setAttribute("aria-label", rank === undefined
      ? `Move ${title} by rank`
      : `Move ${title}, currently rank ${rank}. Use the up and down arrow keys`);
  });
}

function sortTasks() {
  const rows = taskRows();
  rows.sort(sortControl.value === "rank" ? compareRank : compareSmart);
  renderTaskOrder(rows);
  updateRankPresentation();
}

function saveSortPreference(value) {
  try {
    localStorage.setItem(taskSortStorageKey, value);
  } catch (_) { /* Storage can be unavailable in privacy modes. */ }
}

function savedSortPreference() {
  try {
    return normalizeSortMode(localStorage.getItem(taskSortStorageKey));
  } catch (_) {
    return "smart";
  }
}

function setSortMode(value, persist = true) {
  sortControl.value = normalizeSortMode(value);
  taskList.dataset.sort = sortControl.value;
  if (persist) saveSortPreference(sortControl.value);
  sortTasks();
}

function setReorderInFlight(value) {
  reorderInFlight = value;
  updateRankPresentation();
}

async function persistRankMove(row, previousOrder) {
  const orderedRows = taskRows();
  const { afterTaskId, beforeTaskId } = rankNeighbors(orderedRows, row);
  setReorderInFlight(true);
  try {
    const result = await api(`/api/tasks/${row.dataset.taskId}/rank`, {
      method: "PATCH",
      body: JSON.stringify({
        after_task_id: afterTaskId,
        before_task_id: beforeTaskId,
      }),
    });
    result.ranks.forEach((rank) => {
      const rankedRow = taskList.querySelector(`.task-row[data-task-id="${rank.id}"]`);
      if (rankedRow) rankedRow.dataset.rankKey = rank.rank_key;
    });
    sortTasks();
    const currentRank = taskRows().sort(compareRank).indexOf(row) + 1;
    notify(`Moved to rank ${currentRank}`);
  } catch (error) {
    renderTaskOrder(previousOrder);
    sortTasks();
    notify(error.message, true);
  } finally {
    setReorderInFlight(false);
    row.querySelector(".rank-handle").focus();
  }
}

function dragAfterRow(pointerY, draggedRow) {
  return dragInsertionRow(taskRows(), pointerY, draggedRow);
}

taskList.addEventListener("dragstart", (event) => {
  const handle = event.target.closest(".rank-handle");
  if (!handle || handle.disabled || sortControl.value !== "rank") {
    event.preventDefault();
    return;
  }
  const row = taskRow(handle);
  dragState = { row, snapshot: taskRows(), dropped: false };
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", row.dataset.taskId);
  row.classList.add("is-dragging");
});

taskList.addEventListener("dragover", (event) => {
  if (!dragState) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  const afterRow = dragAfterRow(event.clientY, dragState.row);
  if (afterRow) {
    taskList.insertBefore(dragState.row, afterRow);
    return;
  }
  const lastVisibleRow = taskRows()
    .filter((row) => row !== dragState.row && !row.hidden)
    .at(-1);
  if (lastVisibleRow) lastVisibleRow.after(dragState.row);
});

taskList.addEventListener("drop", (event) => {
  if (!dragState) return;
  event.preventDefault();
  dragState.dropped = true;
});

taskList.addEventListener("dragend", async () => {
  if (!dragState) return;
  const state = dragState;
  dragState = null;
  state.row.classList.remove("is-dragging");
  if (!state.dropped) {
    renderTaskOrder(state.snapshot);
    return;
  }

  const visibleOrder = taskRows().filter((row) => !row.hidden);
  const mergedOrder = mergeVisibleOrder(state.snapshot, visibleOrder);
  renderTaskOrder(mergedOrder);
  if (!sameTaskOrder(state.snapshot, mergedOrder)) {
    await persistRankMove(state.row, state.snapshot);
  }
});

taskList.addEventListener("keydown", async (event) => {
  const handle = event.target.closest(".rank-handle");
  if (!handle || handle.disabled || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
  event.preventDefault();
  const row = taskRow(handle);
  const snapshot = taskRows();
  const movedOrder = keyboardRankMove(
    snapshot,
    row,
    event.key === "ArrowUp" ? "up" : "down",
  );
  if (!movedOrder) return;
  renderTaskOrder(movedOrder);
  await persistRankMove(row, snapshot);
});

sortControl.addEventListener("change", () => setSortMode(sortControl.value));
applyFilters();
setSortMode(savedSortPreference(), false);
renderFocusView();
showAppView(document.body.dataset.initialView || "focus");
if (document.body.dataset.error) {
  notify(document.body.dataset.error, true);
} else if (document.body.dataset.notice) {
  notify(document.body.dataset.notice);
}

// Reconcile at midnight and surface timed follow-ups within a minute.
function dateTimeInConfiguredTimezone() {
  return dateTimeInTimezone(document.body.dataset.timezone);
}

function pendingFollowUpBecameDue(localDateTime) {
  return hasPendingFollowUpBecomeDue(taskRows(), localDateTime);
}
setInterval(async () => {
  const localDateTime = dateTimeInConfiguredTimezone();
  if (
    localDateTime.date !== document.body.dataset.today
    || pendingFollowUpBecameDue(localDateTime)
  ) {
    try {
      await api("/api/reconcile", { method: "POST", body: "{}" });
      location.assign(`/?view=${encodeURIComponent(currentTaskView())}`);
    } catch (error) {
      notify(error.message, true);
    }
  }
}, 60_000);
