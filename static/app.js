const toast = document.querySelector("#toast");
const maxAttachmentBytes = Number(document.body.dataset.maxAttachmentBytes);
const maxAttachmentsPerTask = Number(document.body.dataset.maxAttachments);
const maxUploadBytes = Number(document.body.dataset.maxUploadBytes);
const newlyCreatedTaskStorageKey = "aur-bataao-newly-created-task";
const taskReturnViewStorageKey = "aur-bataao-task-return-view";
const themeStorageKey = "aur-bataao-theme";
const themeToggle = document.querySelector("#theme-toggle");
const darkModePreference = window.matchMedia("(prefers-color-scheme: dark)");
let toastTimer;

function savedTheme() {
  try {
    const theme = localStorage.getItem(themeStorageKey);
    return theme === "light" || theme === "dark" ? theme : "";
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

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, {
    ...options,
    headers,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { message = (await response.json()).error || message; } catch (_) { /* no JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function taskRow(element) {
  return element.closest(".task-row");
}

function formatFileSize(byteSize) {
  if (byteSize < 1024) return `${byteSize} B`;
  if (byteSize < 1024 * 1024) return `${(byteSize / 1024).toFixed(1)} KB`;
  return `${(byteSize / (1024 * 1024)).toFixed(1)} MB`;
}

function clipboardFiles(event) {
  return [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(Boolean);
}

function uploadFilename(file, index) {
  if (file.name) return file.name;
  const extension = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
  }[file.type] || "";
  return `clipboard-${Date.now()}-${index + 1}${extension}`;
}

function attachmentError(files, existingCount = 0, pendingFiles = []) {
  const emptyFile = files.find((file) => file.size === 0);
  if (emptyFile) return `${emptyFile.name || "An attachment"} is empty`;
  const oversizedFile = files.find((file) => file.size > maxAttachmentBytes);
  if (oversizedFile) {
    return `${oversizedFile.name || "An attachment"} is larger than ${formatFileSize(maxAttachmentBytes)}`;
  }
  if (existingCount + pendingFiles.length + files.length > maxAttachmentsPerTask) {
    return `A task can have at most ${maxAttachmentsPerTask} attachments`;
  }
  const uploadSize = [...pendingFiles, ...files].reduce((total, file) => total + file.size, 0);
  if (uploadSize >= maxUploadBytes) {
    return `Attach fewer files at once (combined limit ${formatFileSize(maxUploadBytes)})`;
  }
  return "";
}

function bindDropzone(zone, onFiles) {
  const input = zone.querySelector(".attachment-input");
  const chooser = zone.querySelector(".choose-attachments");

  chooser.addEventListener("click", () => input.click());
  zone.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("button")) zone.focus();
  });
  input.addEventListener("change", () => {
    if (input.files.length) onFiles([...input.files]);
    input.value = "";
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

async function uploadTaskAttachments(zone, files) {
  const row = taskRow(zone);
  const selectionError = attachmentError(files, Number(zone.dataset.attachmentCount));
  if (selectionError) {
    notify(selectionError, true);
    return;
  }
  const formData = new FormData();
  files.forEach((file, index) => formData.append("attachments", file, uploadFilename(file, index)));
  zone.classList.add("is-uploading");
  zone.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
  try {
    await api(`/api/tasks/${row.dataset.taskId}/attachments`, {
      method: "POST",
      body: formData,
    });
    notify(`${files.length} attachment${files.length === 1 ? "" : "s"} added`);
    reloadAndRestoreTaskView();
  } catch (error) {
    zone.classList.remove("is-uploading");
    zone.querySelectorAll("button, input").forEach((control) => { control.disabled = false; });
    notify(error.message, true);
  }
}

function renderLabels(row, labels) {
  const detailList = row.querySelector(".task-details .label-list");
  detailList.replaceChildren(...labels.map((label) => {
    const chip = document.createElement("span");
    chip.className = `label-chip ${label.type}`;
    chip.dataset.labelId = label.id;
    chip.append(document.createTextNode(`${label.name} `));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "remove-label";
    button.title = "Remove label";
    button.setAttribute("aria-label", `Remove ${label.name}`);
    button.textContent = "×";
    chip.append(button);
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
  const hasUnresolvedBlockers = unresolvedBlockerIds.length > 0;
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
    || (["todo", "in_progress"].includes(task.status) && !hasUnresolvedBlockers),
  );
  row.dataset.blocked = String(task.status === "blocked" || hasUnresolvedBlockers);
  row.dataset.unresolvedBlockerIds = unresolvedBlockerIds.join(" ");
  row.classList.remove("status-todo", "status-in_progress", "status-blocked", "status-done");
  row.classList.add(`status-${task.status}`);
  row.querySelector(".focus-status-badge").textContent = {
    todo: "To do",
    in_progress: "In progress",
    blocked: "Blocked",
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
  const activeTaskCount = [...document.querySelectorAll(".task-row")]
    .filter((task) => task.dataset.status !== "done").length;
  const timezone = document.body.dataset.timezone;
  countLabel.textContent = `${activeTaskCount} ${activeTaskCount === 1 ? "task" : "tasks"} · ${timezone}`;
}

function expandTaskDetails(row) {
  const details = row.querySelector(".task-details");
  details.hidden = false;
  row.querySelectorAll(".toggle-details, .focus-toggle-details").forEach((control) => {
    control.setAttribute("aria-expanded", "true");
  });
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
  if (field === "status" && value === "waiting_person") {
    control.value = previous;
    expandTaskDetails(row);
    row.querySelector('.waiting-form input[name="person_name"]')?.focus();
    return;
  }
  control.dataset.previous = value;
  if (field === "status") {
    row.dataset.status = value;
    row.classList.remove("status-todo", "status-in_progress", "status-blocked", "status-done");
    row.classList.add(`status-${value}`);
  }
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
    if (field === "status" && row.dataset.waitingOn && value !== "blocked") {
      reloadAndRestoreTaskView();
      return;
    }
    applyTask(row, result.task);
  } catch (error) {
    control.value = previous;
    control.dataset.previous = previous;
    if (field === "status") {
      row.dataset.status = previous;
      row.classList.remove("status-todo", "status-in_progress", "status-blocked", "status-done");
      row.classList.add(`status-${previous}`);
    }
    notify(error.message, true);
  }
}

document.querySelectorAll("[data-field]").forEach((control) => {
  control.dataset.previous = control.value;
  control.addEventListener(control.tagName === "TEXTAREA" || control.dataset.field === "title" ? "blur" : "change", () => patchControl(control));
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
      const focusToggle = row.querySelector(".focus-toggle-details");
      focusToggle.setAttribute("aria-label", "Expand task details and editing controls");
      focusToggle.title = focusToggle.getAttribute("aria-label");
    }
    return;
  }

  const labelFilter = event.target.closest(".task-label-filter");
  if (labelFilter) {
    const labelMenu = document.querySelector("#label-filter");
    labelMenu.querySelectorAll("[data-filter-value]").forEach((checkbox) => {
      checkbox.checked = checkbox.value === labelFilter.dataset.labelId;
    });
    updateMultiSelect(labelMenu);
    applyFilters();
    return;
  }

  const removeLabel = event.target.closest(".remove-label");
  if (removeLabel) {
    const row = taskRow(removeLabel);
    const chip = removeLabel.closest(".label-chip");
    chip.hidden = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/labels/${chip.dataset.labelId}`, { method: "DELETE" });
      removeRenderedLabel(row, chip.dataset.labelId);
    } catch (error) {
      chip.hidden = false;
      notify(error.message, true);
    }
    return;
  }

  const removeRelationship = event.target.closest(".remove-relationship");
  if (removeRelationship) {
    removeRelationship.disabled = true;
    try {
      await api(
        `/api/tasks/${removeRelationship.dataset.blockedTaskId}/dependencies/${removeRelationship.dataset.blockerTaskId}`,
        { method: "DELETE" },
      );
      reloadAndRestoreTaskView();
    } catch (error) {
      removeRelationship.disabled = false;
      notify(error.message, true);
    }
    return;
  }

  const removeAttachment = event.target.closest(".remove-attachment");
  if (removeAttachment) {
    const row = taskRow(removeAttachment);
    const item = removeAttachment.closest(".attachment-item");
    removeAttachment.disabled = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/attachments/${item.dataset.attachmentId}`, {
        method: "DELETE",
      });
      reloadAndRestoreTaskView();
    } catch (error) {
      removeAttachment.disabled = false;
      notify(error.message, true);
    }
  }
});

async function submitAndReload(form, url, body) {
  const button = form.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    await api(url, { method: "POST", body: JSON.stringify(body) });
    reloadAndRestoreTaskView();
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
  }
}

const newTaskDialog = document.querySelector("#new-task-dialog");
const newTaskForm = document.querySelector("#new-task-form");
const newTaskInput = document.querySelector("#new-task-input");
const newTaskSubmit = newTaskForm.querySelector("button[type='submit']");
const pendingAttachmentList = document.querySelector("#pending-attachments");
let pendingAttachments = [];
let newTaskReturnView = "focus";

function currentTaskView() {
  if (!document.body.classList.contains("manage-mode")) return "focus";
  return blockedView ? "blocked" : "manage";
}

function showTaskView(view) {
  if (view === "manage" || view === "blocked") {
    showManageView({ blockedOnly: view === "blocked" });
  } else {
    showFocusView();
  }
}

function rememberTaskView(view = currentTaskView()) {
  try {
    sessionStorage.setItem(taskReturnViewStorageKey, view);
  } catch (_) { /* Storage can be unavailable in privacy modes. */ }
}

function reloadAndRestoreTaskView(view = currentTaskView()) {
  rememberTaskView(view);
  location.reload();
}

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

newTaskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = newTaskInput.value.trim();
  if (!title) return;

  newTaskForm.setAttribute("aria-busy", "true");
  updateNewTaskSubmit();
  const formData = new FormData();
  formData.append("title", title);
  pendingAttachments.forEach((file, index) => {
    formData.append("attachments", file, uploadFilename(file, index));
  });
  try {
    const result = await api("/api/tasks", { method: "POST", body: formData });
    try {
      sessionStorage.setItem(newlyCreatedTaskStorageKey, String(result.task.id));
    } catch (_) { /* Storage can be unavailable in privacy modes. */ }
    reloadAndRestoreTaskView(newTaskReturnView);
  } catch (error) {
    newTaskForm.removeAttribute("aria-busy");
    updateNewTaskSubmit();
    notify(error.message, true);
  }
});

document.querySelectorAll(".task-attachment-dropzone").forEach((zone) => {
  bindDropzone(zone, (files) => uploadTaskAttachments(zone, files));
  taskRow(zone).querySelector(".task-details").addEventListener("paste", (event) => {
    if (zone.contains(event.target)) return;
    const files = clipboardFiles(event);
    if (files.length) {
      event.preventDefault();
      uploadTaskAttachments(zone, files);
    }
  });
});

document.querySelectorAll(".create-blocker-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, "/api/tasks", {
      title: form.elements.title.value,
      blocks_task_id: Number(row.dataset.taskId),
    });
  });
});

document.querySelectorAll(".add-label-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${row.dataset.taskId}/labels`, { name: form.elements.name.value });
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

document.querySelectorAll(".add-blocker-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${row.dataset.taskId}/dependencies`, { blocker_task_id: form.elements.blocker_task_id.value });
  });
});

document.querySelectorAll(".add-blocked-task-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${form.elements.blocked_task_id.value}/dependencies`, { blocker_task_id: Number(row.dataset.taskId) });
  });
});

document.querySelectorAll(".comment-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${row.dataset.taskId}/comments`, {
      body: form.elements.body.value,
      counts_as_progress: form.elements.counts_as_progress.checked,
    });
  });
});

document.querySelectorAll(".waiting-form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const row = taskRow(form);
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/waiting`, {
        method: "PUT",
        body: JSON.stringify({
          person_name: form.elements.person_name.value,
          note: form.elements.note.value,
          next_follow_up_on: form.elements.next_follow_up_on.value || null,
          next_follow_up_time: form.elements.next_follow_up_time.value || null,
        }),
      });
      reloadAndRestoreTaskView();
    } catch (error) {
      button.disabled = false;
      notify(error.message, true);
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
followUpForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = followUpForm.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    await api(`/api/tasks/${followUpForm.elements.task_id.value}/follow-ups`, {
      method: "POST",
      body: JSON.stringify({
        note: followUpForm.elements.note.value,
        next_follow_up_on: followUpForm.elements.next_follow_up_on.value || null,
        next_follow_up_time: followUpForm.elements.next_follow_up_time.value || null,
      }),
    });
    reloadAndRestoreTaskView();
  } catch (error) {
    button.disabled = false;
    notify(error.message, true);
  }
});

document.querySelectorAll(".resolve-waiting").forEach((button) => {
  button.addEventListener("click", async () => {
    const row = taskRow(button);
    button.disabled = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/waiting/resolve`, {
        method: "POST",
        body: "{}",
      });
      reloadAndRestoreTaskView();
    } catch (error) {
      button.disabled = false;
      notify(error.message, true);
    }
  });
});

const searchFilter = document.querySelector("#search-filter");
const statusFilter = document.querySelector("#status-filter");
const labelFilter = document.querySelector("#label-filter");
const overdueFilter = document.querySelector("#overdue-filter");
const stalledFilter = document.querySelector("#stalled-filter");
const followUpFilter = document.querySelector("#follow-up-filter");
const blockedViewIndicator = document.querySelector("#blocked-view-indicator");
let blockedView = false;

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
  const allSelected = options.length > 0 && selectedCount === options.length;
  const selectAll = filter.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.checked = allSelected;
    selectAll.indeterminate = selectedCount > 0 && !allSelected;
  }

  let summary = filter.dataset.allLabel;
  if (!allSelected) {
    if (selectedCount === 0) {
      summary = filter.dataset.emptyLabel;
    } else {
      summary = `${selectedCount} ${selectedCount === 1 ? filter.dataset.singular : filter.dataset.plural}`;
    }
  }
  filter.querySelector(".multi-select-summary").textContent = summary;
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
  const labelsAreFiltered = labels.size !== labelOptions.length;
  const overdueOnly = overdueFilter.checked;
  const stalledOnly = stalledFilter.checked;
  const followUpsOnly = followUpFilter.checked;
  let visibleTasks = 0;
  document.querySelectorAll(".task-row").forEach((row) => {
    const rowLabels = row.dataset.labelIds.split(" ").filter(Boolean);
    row.hidden = Boolean(
      (query && !row.dataset.title.includes(query)) ||
      !statuses.has(row.dataset.status) ||
      (labelsAreFiltered && !rowLabels.some((label) => labels.has(label))) ||
      (overdueOnly && row.dataset.overdue !== "true") ||
      (stalledOnly && row.dataset.stalled !== "true") ||
      (followUpsOnly && row.dataset.followUpDue !== "true") ||
      (blockedView && row.dataset.blocked !== "true")
    );
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
const smartStatusOrder = { in_progress: 0, blocked: 1, todo: 2, done: 3 };
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

function compareRank(first, second) {
  const firstRank = BigInt(first.dataset.rankKey);
  const secondRank = BigInt(second.dataset.rankKey);
  if (firstRank < secondRank) return -1;
  if (firstRank > secondRank) return 1;
  return Number(first.dataset.taskId) - Number(second.dataset.taskId);
}

function compareSmart(first, second) {
  const followUpDifference = Number(second.dataset.followUpDue === "true")
    - Number(first.dataset.followUpDue === "true");
  if (followUpDifference) return followUpDifference;
  if (first.dataset.followUpDue === "true" && second.dataset.followUpDue === "true") {
    const followUpDateDifference = first.dataset.followUpDate.localeCompare(second.dataset.followUpDate);
    if (followUpDateDifference) return followUpDateDifference;
    const followUpTimeDifference = first.dataset.followUpTime.localeCompare(second.dataset.followUpTime);
    if (followUpTimeDifference) return followUpTimeDifference;
  }
  const statusDifference = smartStatusOrder[first.dataset.status] - smartStatusOrder[second.dataset.status];
  if (statusDifference) return statusDifference;
  const firstDueDate = first.dataset.dueDate || "9999-12-31";
  const secondDueDate = second.dataset.dueDate || "9999-12-31";
  const dueDateDifference = firstDueDate.localeCompare(secondDueDate);
  if (dueDateDifference) return dueDateDifference;
  return Number(second.dataset.taskId) - Number(first.dataset.taskId);
}

let focusTaskId = document.querySelector(".task-row.is-focus-task")?.dataset.taskId || "";
const focusEmptyState = document.querySelector("#focus-empty-state");
const focusNextAction = document.querySelector("#focus-next-action");
const aurBataaoButton = document.querySelector("#aur-bataao-button");
const viewToggle = document.querySelector("#view-toggle");
const viewToggleLabel = viewToggle.querySelector(".view-toggle-label");

function actionableTaskRows() {
  return taskRows()
    .filter((row) => row.dataset.actionable === "true")
    .sort(compareSmart);
}

function renderFocusView(preferredTaskId = focusTaskId) {
  const candidates = actionableTaskRows();
  const current = candidates.find((row) => row.dataset.taskId === preferredTaskId) || candidates[0] || null;
  taskRows().forEach((row) => row.classList.toggle("is-focus-task", row === current));
  focusTaskId = current?.dataset.taskId || "";

  document.body.classList.toggle("has-actionable", Boolean(current));
  document.body.classList.toggle("focus-empty", !current);
  focusEmptyState.hidden = Boolean(current);
  focusNextAction.hidden = !current;
  aurBataaoButton.disabled = candidates.length < 2;
  aurBataaoButton.title = candidates.length < 2 ? "No other actionable task right now" : "Suggest another task";
}

function showAnotherTask(currentRow) {
  const candidates = actionableTaskRows();
  if (candidates.length < 2) return;
  const currentIndex = candidates.indexOf(currentRow);
  const next = candidates[(currentIndex + 1) % candidates.length];
  renderFocusView(next.dataset.taskId);
}

function showManageView({ blockedOnly = false } = {}) {
  document.body.classList.remove("focus-mode");
  document.body.classList.add("manage-mode");
  viewToggleLabel.textContent = "Aur Bataao";
  viewToggle.setAttribute("aria-label", "Switch to Aur Bataao view");
  blockedView = blockedOnly;
  blockedViewIndicator.hidden = !blockedOnly;
  applyFilters();
}

function showFocusView() {
  document.body.classList.remove("manage-mode");
  document.body.classList.add("focus-mode");
  viewToggleLabel.textContent = "View all tasks";
  viewToggle.setAttribute("aria-label", "Switch to all tasks view");
  renderFocusView();
}

viewToggle.setAttribute("aria-label", "Switch to all tasks view");
viewToggle.addEventListener("click", () => {
  if (document.body.classList.contains("focus-mode")) {
    showManageView();
  } else {
    showFocusView();
  }
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

document.querySelectorAll(".complete-focus-task").forEach((button) => {
  button.addEventListener("click", async () => {
    const row = taskRow(button);
    const statusControl = row.querySelector('[data-field="status"]');
    const previousStatus = row.dataset.status;
    const previousControlValue = statusControl.value;
    const candidates = actionableTaskRows();
    const currentIndex = candidates.indexOf(row);
    const next = candidates.length > 1
      ? candidates[(currentIndex + 1) % candidates.length]
      : null;
    const dependentSnapshots = taskRows()
      .filter((candidate) => candidate.dataset.unresolvedBlockerIds.split(" ").includes(row.dataset.taskId))
      .map((candidate) => ({
        row: candidate,
        blockerIds: candidate.dataset.unresolvedBlockerIds,
        actionable: candidate.dataset.actionable,
        blocked: candidate.dataset.blocked,
      }));

    row.dataset.status = "done";
    row.dataset.actionable = "false";
    row.dataset.blocked = "false";
    row.classList.remove(`status-${previousStatus}`);
    row.classList.add("status-done");
    statusControl.value = "done";
    statusControl.dataset.previous = "done";
    dependentSnapshots.forEach((snapshot) => {
      const remainingBlockerIds = snapshot.blockerIds
        .split(" ")
        .filter((blockerId) => blockerId && blockerId !== row.dataset.taskId);
      snapshot.row.dataset.unresolvedBlockerIds = remainingBlockerIds.join(" ");
      snapshot.row.dataset.blocked = String(
        snapshot.row.dataset.status === "blocked" || remainingBlockerIds.length > 0,
      );
      snapshot.row.dataset.actionable = String(
        ["todo", "in_progress"].includes(snapshot.row.dataset.status)
        && remainingBlockerIds.length === 0,
      );
    });
    updateActiveTaskCount();
    applyFilters();
    renderFocusView(next?.dataset.taskId || "");

    try {
      const result = await api(`/api/tasks/${row.dataset.taskId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "done" }),
      });
      applyTask(row, result.task);
    } catch (error) {
      row.dataset.status = previousStatus;
      row.dataset.actionable = "true";
      row.classList.remove("status-done");
      row.classList.add(`status-${previousStatus}`);
      statusControl.value = previousControlValue;
      statusControl.dataset.previous = previousControlValue;
      dependentSnapshots.forEach((snapshot) => {
        snapshot.row.dataset.unresolvedBlockerIds = snapshot.blockerIds;
        snapshot.row.dataset.actionable = snapshot.actionable;
        snapshot.row.dataset.blocked = snapshot.blocked;
      });
      updateActiveTaskCount();
      applyFilters();
      renderFocusView(row.dataset.taskId);
      notify(error.message, true);
    }
  });
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
    const value = localStorage.getItem(taskSortStorageKey);
    return value === "rank" ? "rank" : "smart";
  } catch (_) {
    return "smart";
  }
}

function setSortMode(value, persist = true) {
  sortControl.value = value === "rank" ? "rank" : "smart";
  taskList.dataset.sort = sortControl.value;
  if (persist) saveSortPreference(sortControl.value);
  sortTasks();
}

function emphasizeNewlyCreatedTask() {
  let taskId = "";
  try {
    taskId = sessionStorage.getItem(newlyCreatedTaskStorageKey) || "";
    sessionStorage.removeItem(newlyCreatedTaskStorageKey);
  } catch (_) { /* Storage can be unavailable in privacy modes. */ }
  if (!taskId) return;

  const row = taskRows().find((candidate) => candidate.dataset.taskId === taskId);
  if (!row) return;
  if (row.hidden) {
    notify("Task added — hidden by current filters");
    return;
  }

  const bounds = row.getBoundingClientRect();
  const isOutsideViewport = bounds.top < 0 || bounds.bottom > window.innerHeight;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (isOutsideViewport) {
    row.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "nearest",
    });
  }

  row.classList.add("is-newly-created");
  setTimeout(() => row.classList.remove("is-newly-created"), 3600);
  notify("Task added");
}

function restoreTaskViewAfterReload() {
  let view = "";
  try {
    view = sessionStorage.getItem(taskReturnViewStorageKey) || "";
    sessionStorage.removeItem(taskReturnViewStorageKey);
  } catch (_) { /* Storage can be unavailable in privacy modes. */ }
  if (view) showTaskView(view);
}

function setReorderInFlight(value) {
  reorderInFlight = value;
  updateRankPresentation();
}

function sameTaskOrder(first, second) {
  return first.length === second.length
    && first.every((row, index) => row === second[index]);
}

function mergeVisibleOrder(snapshot, visibleOrder) {
  let visibleIndex = 0;
  return snapshot.map((row) => (row.hidden ? row : visibleOrder[visibleIndex++]));
}

async function persistRankMove(row, previousOrder) {
  const orderedRows = taskRows();
  const position = orderedRows.indexOf(row);
  const afterTaskId = position > 0 ? Number(orderedRows[position - 1].dataset.taskId) : null;
  const beforeTaskId = position < orderedRows.length - 1
    ? Number(orderedRows[position + 1].dataset.taskId)
    : null;
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
  return taskRows()
    .filter((row) => row !== draggedRow && !row.hidden)
    .reduce((closest, row) => {
      const box = row.getBoundingClientRect();
      const offset = pointerY - box.top - box.height / 2;
      return offset < 0 && offset > closest.offset ? { offset, row } : closest;
    }, { offset: Number.NEGATIVE_INFINITY, row: null }).row;
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
  const visibleOrder = snapshot.filter((candidate) => !candidate.hidden);
  const currentIndex = visibleOrder.indexOf(row);
  const targetIndex = event.key === "ArrowUp" ? currentIndex - 1 : currentIndex + 1;
  if (targetIndex < 0 || targetIndex >= visibleOrder.length) return;
  visibleOrder.splice(currentIndex, 1);
  visibleOrder.splice(targetIndex, 0, row);
  renderTaskOrder(mergeVisibleOrder(snapshot, visibleOrder));
  await persistRankMove(row, snapshot);
});

sortControl.addEventListener("change", () => setSortMode(sortControl.value));
applyFilters();
setSortMode(savedSortPreference(), false);
renderFocusView();
restoreTaskViewAfterReload();
emphasizeNewlyCreatedTask();

// Reconcile at midnight and surface timed follow-ups within a minute.
function dateTimeInConfiguredTimezone() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: document.body.dataset.timezone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return {
    date: `${values.year}-${values.month}-${values.day}`,
    minute: `${values.hour}:${values.minute}`,
  };
}

function pendingFollowUpBecameDue(localDateTime) {
  const currentMinute = `${localDateTime.date}T${localDateTime.minute}`;
  return taskRows().some((row) => (
    row.dataset.status === "blocked"
    && row.dataset.followUpDue !== "true"
    && row.dataset.followUpDate
    && `${row.dataset.followUpDate}T${row.dataset.followUpTime}` <= currentMinute
  ));
}
setInterval(async () => {
  const localDateTime = dateTimeInConfiguredTimezone();
  if (
    localDateTime.date !== document.body.dataset.today
    || pendingFollowUpBecameDue(localDateTime)
  ) {
    try {
      await api("/api/reconcile", { method: "POST", body: "{}" });
      reloadAndRestoreTaskView();
    } catch (error) {
      notify(error.message, true);
    }
  }
}, 60_000);
