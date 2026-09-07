const toast = document.querySelector("#toast");
let toastTimer;

function notify(message, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = error ? "show error" : "show";
  toastTimer = setTimeout(() => { toast.className = ""; }, 2600);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
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

  const removeDependency = event.target.closest(".remove-dependency");
  if (removeDependency) {
    const row = taskRow(removeDependency);
    removeDependency.disabled = true;
    try {
      await api(`/api/tasks/${row.dataset.taskId}/dependencies/${removeDependency.dataset.blockerId}`, { method: "DELETE" });
      location.reload();
    } catch (error) {
      removeDependency.disabled = false;
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

document.querySelector("#new-task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitAndReload(form, "/api/tasks", { title: form.elements.title.value });
});

document.querySelectorAll(".add-subtask-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, "/api/tasks", { title: form.elements.title.value, parent_task_id: Number(row.dataset.taskId) });
  });
});

document.querySelectorAll(".add-label-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${row.dataset.taskId}/labels`, { name: form.elements.name.value });
  });
});

document.querySelectorAll(".add-dependency-form").forEach((form) => {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const row = taskRow(form);
    submitAndReload(form, `/api/tasks/${row.dataset.taskId}/dependencies`, { blocker_task_id: form.elements.blocker_task_id.value });
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
