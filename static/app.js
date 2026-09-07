const toast = document.querySelector("#toast");
const maxAttachmentBytes = Number(document.body.dataset.maxAttachmentBytes);
const maxAttachmentsPerTask = Number(document.body.dataset.maxAttachments);
const maxUploadBytes = Number(document.body.dataset.maxUploadBytes);
let toastTimer;

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
    location.reload();
  } catch (error) {
    zone.classList.remove("is-uploading");
    zone.querySelectorAll("button, input").forEach((control) => { control.disabled = false; });
    notify(error.message, true);
  }
}

function renderLabels(row, labels) {
  const list = row.querySelector(".label-list");
  list.replaceChildren(...labels.map((label) => {
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
}

function applyTask(row, task) {
  row.dataset.status = task.status;
  row.dataset.overdue = String(Boolean(task.overdue));
  row.dataset.stalled = String(Boolean(task.stalled));
  row.classList.remove("status-todo", "status-in_progress", "status-blocked", "status-done");
  row.classList.add(`status-${task.status}`);
  if (task.labels) renderLabels(row, task.labels);
  applyFilters();
}

async function patchControl(control) {
  const row = taskRow(control);
  const field = control.dataset.field;
  const previous = control.dataset.previous ?? control.defaultValue;
  const value = control.value;
  if (value === previous) return;
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
    }
    control.value = result.task[field] ?? "";
    control.dataset.previous = control.value;
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
  const toggle = event.target.closest(".toggle-details");
  if (toggle) {
    const details = taskRow(toggle).querySelector(".task-details");
    const opening = details.hidden;
    details.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    return;
  }

  const removeLabel = event.target.closest(".remove-label");
  if (removeLabel) {
    const row = taskRow(removeLabel);
    const chip = removeLabel.closest(".label-chip");
    chip.hidden = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/labels/${chip.dataset.labelId}`, { method: "DELETE" });
      chip.remove();
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
      location.reload();
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
      location.reload();
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
    location.reload();
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

document.querySelector("#open-task-dialog").addEventListener("click", () => {
  newTaskDialog.showModal();
  requestAnimationFrame(() => newTaskInput.focus());
});
newTaskDialog.querySelector(".dialog-close").addEventListener("click", () => newTaskDialog.close());
newTaskDialog.querySelector(".cancel-new-task").addEventListener("click", () => newTaskDialog.close());
newTaskDialog.addEventListener("click", (event) => {
  if (event.target === newTaskDialog) newTaskDialog.close();
});
newTaskDialog.addEventListener("close", () => {
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
    await api("/api/tasks", { method: "POST", body: formData });
    location.reload();
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

const filterControls = ["#search-filter", "#status-filter", "#overdue-filter", "#stalled-filter"].map((selector) => document.querySelector(selector));
function applyFilters() {
  const query = filterControls[0].value.trim().toLowerCase();
  const status = filterControls[1].value;
  const overdueOnly = filterControls[2].checked;
  const stalledOnly = filterControls[3].checked;
  document.querySelectorAll(".task-row").forEach((row) => {
    row.hidden = Boolean(
      (query && !row.dataset.title.includes(query)) ||
      (status && row.dataset.status !== status) ||
      (overdueOnly && row.dataset.overdue !== "true") ||
      (stalledOnly && row.dataset.stalled !== "true")
    );
  });
}
filterControls.forEach((control) => control.addEventListener("input", applyFilters));

// Reconcile within a minute of midnight in the configured user timezone.
function dateInConfiguredTimezone() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: document.body.dataset.timezone,
    year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}
setInterval(async () => {
  if (dateInConfiguredTimezone() !== document.body.dataset.today) {
    try {
      await api("/api/reconcile", { method: "POST", body: "{}" });
      location.reload();
    } catch (error) {
      notify(error.message, true);
    }
  }
}, 60_000);
