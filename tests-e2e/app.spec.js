import { expect, test as base } from "@playwright/test";

import { BROWSER_COVERAGE_ATTACHMENT } from "./browser_coverage_reporter.js";


const test = base.extend({
  collectBrowserCoverage: [async ({ page }, use, testInfo) => {
    await page.coverage.startJSCoverage({ resetOnNavigation: false });
    try {
      await use();
    } finally {
      const entries = (await page.coverage.stopJSCoverage()).filter((entry) => (
        ["/static/app.js", "/static/agent.js"].includes(new URL(entry.url).pathname)
      ));
      await testInfo.attach(BROWSER_COVERAGE_ATTACHMENT, {
        body: Buffer.from(JSON.stringify(entries)),
        contentType: "application/json",
      });
    }
  }, { auto: true }],
  pageErrors: [async ({ page }, use) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await use(errors);
    expect(errors, "uncaught browser errors").toEqual([]);
  }, { auto: true }],
});


test.beforeEach(async ({ request }) => {
  const response = await request.post("/__e2e__/reset");
  expect(response.ok()).toBeTruthy();
});


async function createTask(request, title) {
  const response = await request.post("/api/tasks", { data: { title } });
  expect(response.status()).toBe(201);
  return (await response.json()).task;
}


async function createAgentConversation(page, title) {
  const input = page.getByLabel("Message the agent");
  await input.fill(title);
  await input.press("Enter");
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");
  return page.getByRole("button", { name: title, exact: true });
}


async function createAgentFolder(page, name) {
  const created = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().endsWith("/api/agent/folders")
  ));
  page.once("dialog", (dialog) => dialog.accept(name));
  await page.getByRole("button", { name: "+ Folder" }).click();
  expect((await created).ok()).toBeTruthy();
  const heading = page.getByRole("heading", { name });
  await expect(heading).toBeVisible();
  return heading;
}


test("creates, edits, and completes a task", async ({ page }) => {
  await page.goto("/?view=manage");
  await page.getByRole("button", { name: "Add a Task..." }).click();
  await page.getByLabel("What needs doing?").fill("Browser-created task");
  await Promise.all([
    page.waitForURL(/notice=task-created/),
    page.getByRole("button", { name: "Save task" }).click(),
  ]);

  const row = page.locator(".task-row").first();
  const title = row.locator(".task-title");
  await expect(title).toHaveValue("Browser-created task");

  const update = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && /\/api\/tasks\/\d+$/.test(new URL(response.url()).pathname)
  ));
  await title.fill("Renamed in browser");
  await title.press("Tab");
  expect((await update).ok()).toBeTruthy();
  await expect(title).toHaveValue("Renamed in browser");

  await Promise.all([
    page.waitForLoadState("domcontentloaded"),
    row.locator(".status-select").selectOption("done"),
  ]);
  await expect(page.locator(".task-title").first()).toHaveValue("Renamed in browser");
});


test("filters and persists keyboard rank changes", async ({ page, request }) => {
  await createTask(request, "Alpha");
  await createTask(request, "Beta");
  await createTask(request, "Gamma");
  await page.goto("/?view=manage");

  await page.getByLabel("Filter tasks").fill("beta");
  await expect(page.locator(".task-row:not([hidden])")).toHaveCount(1);
  await expect(page.locator(".task-row:not([hidden]) .task-title")).toHaveValue("Beta");
  await page.getByLabel("Filter tasks").fill("");

  await page.getByLabel("Sort tasks").evaluate((select) => {
    select.value = "rank";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const betaRow = page.locator(".task-row").filter({
    has: page.locator('.task-title[value="Beta"]'),
  });
  const rankUpdate = page.waitForResponse((response) => (
    response.request().method() === "PATCH" && response.url().endsWith("/rank")
  ));
  await betaRow.locator(".rank-handle").press("ArrowUp");
  expect((await rankUpdate).ok()).toBeTruthy();
  await expect(page.locator(".task-row .task-title").first()).toHaveValue("Beta");

  await page.reload();
  await expect(page.getByLabel("Sort tasks")).toHaveValue("rank");
  await expect(page.locator(".task-row .task-title").first()).toHaveValue("Beta");
});


test("treats blocking as a derived filter, not a workflow status", async ({ page, request }) => {
  const blocked = await createTask(request, "Blocked task");
  const blocker = await createTask(request, "Active blocker");
  const dependency = await request.post(`/api/tasks/${blocked.id}/dependencies`, {
    data: { blocker_task_id: blocker.id },
  });
  expect(dependency.status()).toBe(201);
  await page.goto("/?view=manage");

  const blockedRow = page.locator(".task-row").filter({
    has: page.locator('.task-title[value="Blocked task"]'),
  });
  await expect(blockedRow).toHaveAttribute("data-blocked", "true");
  await page.getByLabel("Filter by state").click();
  await page.locator('#status-filter input[value="todo"]').uncheck();
  await page.locator('#status-filter input[value="in_progress"]').uncheck();
  await expect(page.locator(".task-row:not([hidden])")).toHaveCount(1);
  await expect(page.locator(".task-row:not([hidden]) .task-title")).toHaveValue("Blocked task");
});


test("prevents navigation while an edit is dirty", async ({ page, request }) => {
  await createTask(request, "Unsaved task");
  await page.goto("/?view=manage");
  const title = page.locator(".task-title").first();
  await title.focus();
  await title.fill("Unsaved local edit");

  await expect(page.locator("#view-navigation-lock-message")).toBeVisible();
  await page.getByRole("link", { name: "Switch to Agent" }).dispatchEvent("pointerdown", {
    button: 0,
  });
  await page.getByRole("link", { name: "Switch to Agent" }).dispatchEvent("click", {
    button: 0,
  });
  await expect(page.locator("#task-workspace")).toBeVisible();
  await expect(page).toHaveURL(/view=manage/);
  await expect(page.locator("#toast")).toContainText("Finish editing");
});


test("uploads, downloads, and removes an attachment", async ({ page }) => {
  await page.goto("/?view=manage");
  await page.getByRole("button", { name: "Add a Task..." }).click();
  await page.getByLabel("What needs doing?").fill("Task with attachment");
  await page.locator("#new-task-attachments").setInputFiles({
    name: "browser-note.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("browser attachment"),
  });
  await expect(page.locator("#pending-attachments")).toContainText("browser-note.txt");
  await Promise.all([
    page.waitForURL(/notice=task-created/),
    page.getByRole("button", { name: "Save task" }).click(),
  ]);

  await expect(page.locator(".attachment-item")).toHaveCount(1);
  await page.getByRole("button", { name: "Show task details" }).click();
  const attachment = page.getByRole("link", { name: "browser-note.txt" });
  await expect(attachment).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await attachment.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("browser-note.txt");

  await Promise.all([
    page.waitForURL(/notice=attachment-removed/),
    page.getByRole("button", { name: "Remove browser-note.txt" }).click(),
  ]);
  await expect(page.getByRole("link", { name: "browser-note.txt" })).toHaveCount(0);
});


test("persists the selected theme across reloads", async ({ page }) => {
  await page.goto("/");
  const toggle = page.getByRole("switch", { name: /mode/ });
  const original = await page.locator("html").getAttribute("data-theme");
  await toggle.click();
  const selected = await page.locator("html").getAttribute("data-theme");
  expect(selected).not.toBe(original);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", selected);
});


test("agent approval mutates tasks", async ({ page, request }) => {
  await page.goto("/?view=agent");
  await expect(page.locator("#connection-status")).toContainText("Browser test endpoint");
  await page.getByLabel("Message the agent").fill("Create a task from the browser");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Allow create_task?" })).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator("#message-list")).toContainText("browser-agent task was added");
  expect(await (await request.get("/?view=manage")).text()).toContain("Browser agent task");
});


test("agent request failures restore the composer", async ({ page }) => {
  await page.route("**/api/agent/sessions/*/messages", async (route) => {
    await route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic provider failure" }),
    });
  });
  await page.goto("/?view=agent");
  const input = page.getByLabel("Message the agent");
  await input.fill("This should fail");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("alert")).toContainText("Synthetic provider failure");
  await expect(input).toBeEnabled();
  await expect(page.getByRole("button", { name: "Send" })).toBeEnabled();
});


test("shows when the agent is waiting for an LLM response", async ({ page }) => {
  let releaseRequest;
  const requestGate = new Promise((resolve) => { releaseRequest = resolve; });
  await page.route("**/api/agent/sessions/*/messages", async (route) => {
    await requestGate;
    await route.continue();
  });
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Take a moment to answer");
  await page.getByRole("button", { name: "Send" }).click();

  const waiting = page.getByRole("status", { name: "Waiting for LLM response" });
  try {
    await expect(waiting).toBeVisible();
    await expect(page.getByLabel("Message the agent")).toBeDisabled();
  } finally {
    releaseRequest();
  }

  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");
  await expect(waiting).toHaveCount(0);
});


test("discovers, selects, and saves endpoint models", async ({ page }) => {
  await page.goto("/?view=agent");
  await page.getByText("Endpoint settings", { exact: true }).click();
  const settings = page.locator(".endpoint-settings");
  const model = page.locator("#profile-model");

  await settings.getByRole("button", { name: "Discover" }).click();
  await expect(
    page.locator("#model-options").getByRole("option", {
      name: "browser-test-model",
      exact: true,
    }),
  ).toBeVisible();
  await model.fill("browser-test");
  await model.press("ArrowDown");
  await model.press("Enter");
  await expect(model).toHaveValue("browser-test-model");

  await page.locator("#profile-name").fill("Updated browser endpoint");
  const saved = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && /\/api\/llm-profiles\/\d+$/.test(new URL(response.url()).pathname)
  ));
  await settings.getByRole("button", { name: "Save endpoint" }).click();
  expect((await saved).ok()).toBeTruthy();
  await expect(page.locator("#toast")).toContainText("Endpoint saved");
  await expect(page.locator("#connection-status")).toContainText(
    "Updated browser endpoint",
  );

  await settings.getByLabel("Base URL").fill("http://changed.invalid/v1");
  await expect(page.locator("#model-discovery-status")).toContainText(
    "Save endpoint changes",
  );
  await expect(settings.getByRole("button", { name: "Discover" })).toBeDisabled();
});


test("organizes conversations and persists agent width", async ({ page }) => {
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Organize this conversation");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator("#message-list")).toContainText(
    "browser test agent is ready",
  );

  page.once("dialog", (dialog) => dialog.accept("Planning"));
  await page.getByRole("button", { name: "+ Folder" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toBeVisible();

  await page.getByRole("button", {
    name: "Move Organize this conversation to a folder",
    exact: true,
  }).click();
  const moveDialog = page.locator("#move-conversation-dialog");
  await moveDialog.getByLabel("Folder").selectOption({ label: "Planning" });
  await moveDialog.getByRole("button", { name: "Move" }).click();
  await expect(moveDialog).not.toBeVisible();
  await expect(
    page.locator(".session-group").filter({
      has: page.getByRole("heading", { name: "Planning" }),
    }).getByRole("button", { name: "Organize this conversation", exact: true }),
  ).toBeVisible();

  const planningGroup = page.locator('.session-group[data-folder-key^="folder-"]').filter({
    has: page.getByRole("heading", { name: "Planning" }),
  });
  await planningGroup.getByRole("button", { name: "Collapse folder Planning" }).click();
  await expect(planningGroup.locator(".session-group-items")).toBeHidden();
  await expect(planningGroup.getByRole("button", { name: "Expand folder Planning" })).toHaveAttribute(
    "aria-expanded", "false",
  );
  const width = page.locator("#width-toggle");
  await width.click();
  await expect(width).toHaveAttribute("aria-checked", "true");
  await page.reload();
  const reloadedPlanningGroup = page.locator('.session-group[data-folder-key^="folder-"]').filter({
    has: page.getByRole("heading", { name: "Planning" }),
  });
  await expect(reloadedPlanningGroup.locator(".session-group-items")).toBeHidden();
  await expect(width).toHaveAttribute("aria-checked", "true");
  await reloadedPlanningGroup.getByRole("button", { name: "Expand folder Planning" }).click();
  await expect(reloadedPlanningGroup.getByRole("button", {
    name: "Organize this conversation", exact: true,
  })).toBeVisible();
  await reloadedPlanningGroup.getByRole("button", { name: "Collapse folder Planning" }).click();
  await expect(reloadedPlanningGroup.locator(".session-group-items")).toBeHidden();
  await reloadedPlanningGroup.getByRole("button", { name: "Expand folder Planning" }).click();
  await expect(reloadedPlanningGroup.locator(".session-group-items")).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove folder Planning" }).click();
  await expect(page.getByRole("heading", { name: "Planning" })).toHaveCount(0);
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "What should we work through?" })).toBeVisible();
});


test("keeps Agent folders usable when browser storage is unavailable", async ({ page }) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new Error("storage blocked"); };
    Storage.prototype.setItem = () => { throw new Error("storage blocked"); };
  });
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Keep folders usable without storage");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");

  const unfiledGroup = page.locator('.session-group[data-folder-key="unfiled"]');
  await unfiledGroup.getByRole("button", { name: "Collapse folder No folder" }).click();
  await expect(unfiledGroup.locator(".session-group-items")).toBeHidden();
  await unfiledGroup.getByRole("button", { name: "Expand folder No folder" }).click();
  await expect(unfiledGroup.locator(".session-group-items")).toBeVisible();
});


test("rejecting an agent mutation leaves task data unchanged", async ({ page }) => {
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Create a task but let me reject it");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Allow create_task?" })).toBeVisible();
  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(page.locator("#message-list")).toContainText("browser-agent task was added");

  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  await expect(page.locator('.task-title[value="Browser agent task"]')).toHaveCount(0);
});


test("adds, removes, and filters task labels without reloading", async ({ page, request }) => {
  const first = await createTask(request, "First labelled task");
  const second = await createTask(request, "Second labelled task");
  const labelResponse = await request.post(`/api/tasks/${first.id}/labels`, {
    data: { name: "Urgent" },
  });
  expect(labelResponse.status()).toBe(201);

  await page.goto("/?view=manage");
  const firstRow = page.locator(`.task-row[data-task-id="${first.id}"]`);
  const secondRow = page.locator(`.task-row[data-task-id="${second.id}"]`);
  await secondRow.getByRole("button", { name: "Show task details" }).click();
  await secondRow.getByLabel("Choose existing labels for Second labelled task").click();
  const checkbox = secondRow.locator('[data-task-label-option][data-label-name="urgent"]');

  const added = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().endsWith(`/api/tasks/${second.id}/labels`)
  ));
  await checkbox.check();
  expect((await added).status()).toBe(201);
  await expect(secondRow.getByRole("button", { name: "Filter tasks by urgent" })).toBeVisible();

  const removed = page.waitForResponse((response) => (
    response.request().method() === "DELETE"
    && response.url().includes(`/api/tasks/${second.id}/labels/`)
  ));
  await checkbox.uncheck();
  expect((await removed).ok()).toBeTruthy();
  await expect(secondRow.getByRole("button", { name: "Filter tasks by urgent" })).toHaveCount(0);

  await firstRow.getByRole("button", { name: "Filter tasks by urgent" }).click();
  await expect(page.locator(".task-row:not([hidden])")).toHaveCount(1);
  await expect(page.locator(".task-row:not([hidden]) .task-title")).toHaveValue("First labelled task");
});


test("deletes a label after its final task assignment is removed", async ({ page, request }) => {
  const task = await createTask(request, "Temporary label owner");
  const labelResponse = await request.post(`/api/tasks/${task.id}/labels`, {
    data: { name: "temporary" },
  });
  expect(labelResponse.status()).toBe(201);

  await page.goto("/?view=manage");
  const row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  await row.getByLabel("Choose existing labels for Temporary label owner").click();
  await row.locator('[data-task-label-option][data-label-name="temporary"]').uncheck();

  await page.getByLabel("Filter by label").click();
  await page.getByRole("button", { name: "Manage labels…" }).click();
  const dialog = page.getByRole("dialog", { name: "Manage labels" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("0 tasks")).toBeVisible();
  await dialog.getByRole("button", { name: "Close label manager" }).click();
  await expect(dialog).toBeHidden();

  await page.getByLabel("Filter by label").click();
  await page.getByRole("button", { name: "Manage labels…" }).click();
  await Promise.all([
    page.waitForURL(/notice=label-deleted/),
    dialog.getByRole("button", { name: "Delete" }).click(),
  ]);
  await expect(page.locator("#toast")).toContainText("Label deleted");

  await row.getByRole("button", { name: "Show task details" }).click();
  await row.getByLabel("Choose existing labels for Temporary label owner").click();
  await expect(row.locator('[data-task-label-option][data-label-name="temporary"]')).toHaveCount(0);

  await page.getByLabel("Filter by label").click();
  await page.getByRole("button", { name: "Manage labels…" }).click();
  await expect(page.getByRole("dialog", { name: "Manage labels" })).toContainText(
    "No saved manual labels.",
  );
});


test("edits waiting schedules and records a follow-up", async ({ page, request }) => {
  const task = await createTask(request, "Waiting workflow");
  const waiting = await request.put(`/api/tasks/${task.id}/waiting`, {
    data: {
      person_name: "Asha",
      note: "Needs a reply",
      next_follow_up_on: "2099-01-10",
      next_follow_up_time: "14:30",
    },
  });
  expect(waiting.status()).toBe(201);

  await page.goto("/?view=manage");
  const row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  const schedule = row.locator(".follow-up-time-control");
  const date = schedule.locator('input[name="next_follow_up_on"]');
  const time = schedule.locator('input[name="next_follow_up_time"]');
  const toggle = schedule.locator(".toggle-follow-up-time");

  await toggle.click();
  await expect(toggle).toHaveText("+ Add time");
  await expect(time).toHaveValue("");
  await date.fill("");
  await toggle.click();
  await expect(page.locator("#toast")).toContainText("Choose a follow-up date");
  await date.fill("2099-01-11");
  await toggle.click();
  await expect(time).toHaveValue("09:00");

  await row.getByRole("button", { name: "Followed up, still waiting…" }).click();
  const dialog = page.locator("#follow-up-dialog");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#follow-up-dialog-person")).toHaveText("Waiting on Asha");
  await dialog.getByLabel("Context").fill("Sent a reminder");
  await dialog.getByLabel("Next follow-up").fill("2099-01-12");
  await Promise.all([
    page.waitForURL(/notice=follow-up-recorded/),
    dialog.getByRole("button", { name: "Save follow-up" }).click(),
  ]);
  await page.getByText("Recent follow-ups (1)").click();
  await expect(page.getByText(/Sent a reminder/)).toBeVisible();
});


test("validates dropped and pasted attachments before task submission", async ({ page }) => {
  await page.goto("/?view=manage");
  await page.getByRole("button", { name: "Add a Task..." }).click();
  const dialog = page.locator("#new-task-dialog");
  const zone = page.locator("#new-task-dropzone");

  await zone.dispatchEvent("dragenter");
  await expect(zone).toHaveClass(/is-dragging/);
  await zone.dispatchEvent("dragleave", { relatedTarget: null });
  await expect(zone).not.toHaveClass(/is-dragging/);
  await zone.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["note"], "dropped.txt", { type: "text/plain", lastModified: 1 }));
    element.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: transfer }));
    element.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: transfer }));
  });
  await expect(page.locator("#pending-attachments")).toContainText("dropped.txt");

  await dialog.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([], "empty.txt", { type: "text/plain", lastModified: 2 }));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: transfer });
    element.dispatchEvent(event);
  });
  await expect(page.locator("#toast")).toContainText("empty.txt is empty");
  await page.getByRole("button", { name: "Remove dropped.txt" }).click();
  await expect(page.locator("#pending-attachments li")).toHaveCount(0);

  await dialog.getByLabel("What needs doing?").fill("Discarded draft");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Add a Task..." }).click();
  await expect(dialog.getByLabel("What needs doing?")).toHaveValue("");
});


test("restores an inline edit when the API rejects it", async ({ page, request }) => {
  const task = await createTask(request, "Server-owned title");
  await page.route(`**/api/tasks/${task.id}`, async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "Synthetic edit conflict" }),
      });
    } else {
      await route.continue();
    }
  });
  await page.goto("/?view=manage");
  const title = page.locator(`.task-row[data-task-id="${task.id}"] .task-title`);
  await title.fill("Rejected local title");
  await title.blur();
  await expect(page.locator("#toast")).toContainText("Synthetic edit conflict");
  await expect(title).toHaveValue("Server-owned title");
  await expect(page.locator("#view-navigation-lock-message")).toBeHidden();
});


test("supports first endpoint setup and model discovery failures", async ({ page, request }) => {
  expect((await request.post("/__e2e__/profiles/clear")).ok()).toBeTruthy();
  await page.goto("/?view=agent");
  await expect(page.locator("#connection-status")).toHaveText("Configure an endpoint to begin.");
  await expect(page.getByRole("button", { name: "Send" })).toBeDisabled();
  await page.getByText("Endpoint settings", { exact: true }).click();
  const settings = page.locator(".endpoint-settings");
  await expect(page.locator("#model-discovery-status")).toContainText("Save the endpoint");
  await settings.getByLabel("Name", { exact: true }).fill("First endpoint");
  await settings.getByLabel("Base URL").fill("http://first.invalid/v1");
  await page.locator("#profile-model").fill("first-model");
  const created = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && new URL(response.url()).pathname === "/api/llm-profiles"
  ));
  await settings.getByRole("button", { name: "Save endpoint" }).click();
  expect((await created).status()).toBe(201);
  await expect(page.locator("#connection-status")).toContainText("First endpoint");

  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: {} }),
    });
  });
  await settings.getByRole("button", { name: "Discover" }).click();
  await expect(page.locator("#model-discovery-status")).toContainText("invalid model list");
  await expect(settings.getByRole("button", { name: "Retry" })).toBeEnabled();
});


test("uploads to an existing task and reports unsupported transfer APIs", async ({ page, request }) => {
  const task = await createTask(request, "Existing attachment target");
  await page.goto("/?view=manage");
  let row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  const input = row.locator(".attachment-input");
  await Promise.all([
    page.waitForURL(/notice=attachments-added/),
    input.setInputFiles({
      name: "existing.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("existing attachment"),
    }),
  ]);
  await expect(page.getByRole("link", { name: "existing.txt" })).toBeVisible();

  row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.locator(".attachment-input").setInputFiles({
    name: "empty.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(0),
  });
  await expect(page.locator("#toast")).toContainText("empty.txt is empty");

  await page.addInitScript(() => {
    Object.defineProperty(window, "DataTransfer", { value: undefined, configurable: true });
  });
  await page.reload();
  row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  if (await row.locator(".task-details").isHidden()) {
    await row.getByRole("button", { name: "Show task details" }).click();
  }
  await row.locator(".attachment-input").setInputFiles({
    name: "fallback.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("fallback"),
  });
  await expect(page.locator("#toast")).toContainText("Use Choose files");
  await expect(page.getByRole("link", { name: "fallback.txt" })).toHaveCount(0);
});


test("follows system theme when storage is unavailable and clears notices", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new Error("storage blocked"); };
    Storage.prototype.setItem = () => { throw new Error("storage blocked"); };
  });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByLabel("Sort tasks").evaluate((select) => {
    select.value = "rank";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.getByRole("switch", { name: "Switch to light mode" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  await page.evaluate(() => {
    window.dispatchEvent(new CustomEvent("app:notify", {
      detail: { message: "Temporary warning", error: true },
    }));
  });
  await expect(page.locator("#toast")).toHaveClass(/error/);
  await expect(page.locator("#toast")).toHaveText("Temporary warning");
  await expect(page.locator("#toast")).toHaveClass("", { timeout: 4000 });
});


test("cycles focus, resets filters, and honors mutation navigation locks", async ({ page, request }) => {
  const first = await createTask(request, "First focus choice");
  const second = await createTask(request, "Second focus choice");
  await request.patch(`/api/tasks/${first.id}`, { data: { due_date: "2000-01-01" } });
  await page.goto("/?view=focus");
  const initial = await page.locator(".task-row.is-focus-task").getAttribute("data-task-id");
  await page.locator("#aur-bataao-button").click();
  await expect(page.locator(".task-row.is-focus-task")).not.toHaveAttribute("data-task-id", initial);

  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  const states = page.locator("#status-filter");
  await states.getByLabel("Filter by state").click();
  await states.locator("[data-select-all]").check();
  await states.locator("[data-select-all]").uncheck();
  await expect(page.locator("#filter-empty-state")).toBeVisible();
  await states.locator("summary").press("Escape");
  await expect(states).not.toHaveAttribute("open", "");
  await states.getByLabel("Filter by state").click();
  await states.locator("[data-select-all]").check();
  await page.getByLabel("Overdue").check();
  await expect(page.locator(".task-row:not([hidden])")).toHaveCount(1);
  await expect(page.locator(".task-row:not([hidden])")).toHaveAttribute("data-task-id", String(first.id));
  await page.getByLabel("Overdue").uncheck();

  await page.evaluate(() => window.dispatchEvent(new CustomEvent("agent:task-mutation-start")));
  await page.getByRole("link", { name: "Switch to Agent" }).dispatchEvent("click", { button: 0 });
  await expect(page.locator("#toast")).toContainText("Wait for the Agent task update");
  await expect(page.locator("#task-workspace")).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("agent:task-mutation-end")));
  await page.getByRole("link", { name: "Switch to Agent" }).click();
  await expect(page.locator("#agent-workspace")).toBeVisible();
  expect(second.id).toBeGreaterThan(first.id);
});


test("updates pointer drag ranking", async ({ page, request }) => {
  await createTask(request, "Drag first");
  await createTask(request, "Drag second");
  await createTask(request, "Drag third");
  await page.goto("/?view=manage");
  await page.getByLabel("Sort tasks").selectOption("rank");
  const rows = page.locator(".task-row");
  const source = rows.first().locator(".rank-handle");
  const target = rows.last().locator(".rank-handle");
  const rankUpdate = page.waitForResponse((response) => (
    response.request().method() === "PATCH" && response.url().endsWith("/rank")
  ));
  await source.dragTo(target);
  expect((await rankUpdate).ok()).toBeTruthy();
  await expect(page.locator(".task-row .task-title").first()).not.toHaveValue("Drag first");
  const movedOrder = await page.locator(".task-row .task-title").evaluateAll(
    (titles) => titles.map((title) => title.value),
  );
  expect(movedOrder).not.toEqual(["Drag first", "Drag second", "Drag third"]);
  const draggedRow = page.locator(".task-row").filter({
    has: page.locator('.task-title[value="Drag first"]'),
  });
  const keyboardUpdate = page.waitForResponse((response) => (
    response.request().method() === "PATCH" && response.url().endsWith("/rank")
  ));
  await draggedRow.locator(".rank-handle").press("ArrowUp");
  expect((await keyboardUpdate).ok()).toBeTruthy();
});


test("handles model picker empty results and endpoint save errors", async ({ page, request }) => {
  const extra = await request.post("/api/llm-profiles", {
    data: {
      name: "Missing key endpoint",
      base_url: "http://missing-key.invalid/v1",
      model: "manual-model",
      api_key_env: "MISSING_BROWSER_TEST_KEY",
      timeout_seconds: 30,
      supports_tools: false,
      is_default: false,
    },
  });
  expect(extra.status()).toBe(201);
  await page.goto("/?view=agent");
  await page.getByText("Endpoint settings", { exact: true }).click();
  await page.getByLabel("Saved endpoint").selectOption({ label: "Missing key endpoint · manual-model" });
  await expect(page.locator("#connection-status")).toContainText("key variable missing");

  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: [] }),
    });
  });
  await page.getByRole("button", { name: "Discover" }).click();
  await expect(page.locator("#model-discovery-status")).toContainText("No models were returned");
  await expect(page.locator("#model-options")).toContainText("No models were returned");

  await page.unroute("**/api/llm-profiles/*/models");
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.locator("#model-discovery-status")).toContainText(
    "MISSING_BROWSER_TEST_KEY is not set",
  );
  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: ["recovered-model"] }),
    });
  });
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#model-discovery-status")).toContainText("1 model available");
  const model = page.locator("#profile-model");
  await model.fill("does-not-exist");
  await expect(page.locator("#model-options")).toContainText("No matching models");
  await model.press("Escape");
  await expect(page.locator("#model-options")).toBeHidden();

  await page.route("**/api/llm-profiles/*", async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "Synthetic endpoint conflict" }),
      });
    } else {
      await route.continue();
    }
  });
  await page.locator("#profile-name").fill("Rejected endpoint name");
  await page.getByRole("button", { name: "Save endpoint" }).click();
  await expect(page.getByRole("alert")).toContainText("Synthetic endpoint conflict");
});


test("resumes a persisted approval decision after interruption", async ({ page, request }) => {
  const seeded = await request.post("/__e2e__/agent/interrupted");
  expect(seeded.ok()).toBeTruthy();
  await page.goto("/?view=agent");
  await expect(page.getByRole("heading", { name: "Continue this interrupted run?" })).toBeVisible();
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.locator("#message-list")).toContainText("browser-agent task was added");
  expect(await (await request.get("/?view=manage")).text()).toContain("Recovered browser task");
});


test("renders paired and orphan tool history accessibly", async ({ page, request }) => {
  const seeded = await request.post("/__e2e__/agent/messages");
  expect(seeded.ok()).toBeTruthy();
  await page.goto("/?view=agent");
  await expect(page.locator("#message-list strong")).toHaveText("assistant");
  await expect(page.getByText("Ran list_tasks")).toBeVisible();
  await page.getByText("Ran list_tasks").click();
  await expect(page.locator(".tool-response")).toContainText('"tasks": []');
  await expect(page.getByText("Tool response")).toBeVisible();
  await page.getByText("Tool response").click();
  await expect(page.locator(".agent-message.tool").last()).toContainText("orphan result");
  const pairedCall = page.locator('details[data-tool-call-id="paired-call"]');
  await page.getByLabel("Message the agent").fill("Refresh open history");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");
  await expect(pairedCall).toHaveAttribute("open", "");
});


test("submits messages by keyboard and safely cancels organization actions", async ({ page, request }) => {
  await page.goto("/?view=agent");
  const input = page.getByLabel("Message the agent");
  await input.fill("Keyboard conversation");
  await input.press("Shift+Enter");
  await expect(input).toHaveValue(/Keyboard conversation/);
  await input.press("Enter");
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");

  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "+ Folder" }).click();
  page.once("dialog", (dialog) => dialog.accept("   "));
  await page.getByRole("button", { name: "+ Folder" }).click();
  await expect(page.locator(".session-group-heading")).toHaveCount(1);
  page.once("dialog", (dialog) => dialog.accept("Cancelled move target"));
  await page.getByRole("button", { name: "+ Folder" }).click();
  await expect(page.getByRole("heading", { name: "Cancelled move target" })).toBeVisible();

  await page.getByRole("button", {
    name: "Move Keyboard conversation to a folder",
    exact: true,
  }).click();
  const moveDialog = page.locator("#move-conversation-dialog");
  await moveDialog.getByLabel("Folder").selectOption({ label: "Cancelled move target" });
  await page.keyboard.press("Escape");
  await expect(moveDialog).not.toBeVisible();
  await page.locator("#move-conversation-form").evaluate((form) => form.requestSubmit());
  const sessions = await (await request.get("/api/agent/sessions")).json();
  const session = sessions.sessions.find((item) => item.title === "Keyboard conversation");
  expect(session.folder_id).toBeNull();
});


test("wires dropzones, unload protection, and dialog dismissal", async ({ page, request }) => {
  const task = await createTask(request, "Paste attachment target");
  await page.goto("/?view=manage");
  await page.getByRole("button", { name: "Add a Task..." }).click();
  const dialog = page.locator("#new-task-dialog");
  const zone = page.locator("#new-task-dropzone");

  await zone.dispatchEvent("pointerdown");
  await expect(zone).toBeFocused();
  const keyboardChooser = page.waitForEvent("filechooser");
  await zone.press("Enter");
  await (await keyboardChooser).setFiles([]);
  const buttonChooser = page.waitForEvent("filechooser");
  await zone.getByRole("button", { name: "Choose files" }).click();
  await (await buttonChooser).setFiles([]);

  await zone.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["pasted"], "zone-paste.txt", { type: "text/plain" }));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: transfer });
    element.dispatchEvent(event);
  });
  await expect(page.locator("#pending-attachments")).toContainText("zone-paste.txt");
  await dialog.getByLabel("What needs doing?").fill("Dirty draft");
  const unload = await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    return { dispatched: window.dispatchEvent(event), prevented: event.defaultPrevented };
  });
  expect(unload).toEqual({ dispatched: false, prevented: true });
  await dialog.getByRole("button", { name: "Close new task dialog" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Add a Task..." }).click();
  await dialog.dispatchEvent("click");
  await expect(dialog).not.toBeVisible();

  const row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  const taskZone = row.locator(".task-attachment-dropzone");
  await taskZone.evaluate((element) => {
    element.closest("form").addEventListener(
      "submit",
      (event) => event.preventDefault(),
      { capture: true },
    );
  });
  const details = row.locator(".task-details");
  await details.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["detail"], "detail-paste.txt", { type: "text/plain" }));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: transfer });
    element.dispatchEvent(event);
  });
  await expect(taskZone).toHaveClass(/is-uploading/);
  expect(await row.locator(".attachment-input").evaluate((input) => input.files.length)).toBe(1);
});


test("dismisses follow-up dialogs and opens the blocked-task view", async ({ page, request }) => {
  const task = await createTask(request, "Waiting-only task");
  await request.post(`/api/tasks/${task.id}/labels`, { data: { name: "waiting" } });
  await request.put(`/api/tasks/${task.id}/waiting`, {
    data: { person_name: "Dev", next_follow_up_on: "2099-01-10" },
  });
  await page.goto("/?view=focus");
  await expect(page.locator("#focus-empty-state")).toBeVisible();
  await page.locator("#view-blocked-tasks").dispatchEvent("click");
  await expect(page.locator("#blocked-view-indicator")).toBeVisible();
  await expect(page.locator(".task-row:not([hidden])")).toHaveCount(1);
  await page.getByRole("button", { name: "Show all active tasks" }).click();
  await expect(page.locator("#blocked-view-indicator")).toBeHidden();

  const row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  await row.getByRole("button", { name: "Followed up, still waiting…" }).click();
  const dialog = page.locator("#follow-up-dialog");
  const followUpForm = page.locator("#follow-up-form");
  await followUpForm.evaluate((form) => {
    form.addEventListener("submit", (event) => event.preventDefault(), { capture: true });
  });
  await dialog.locator(".toggle-follow-up-time").click();
  await dialog.getByLabel("Context").fill("Submission lock");
  await dialog.getByRole("button", { name: "Save follow-up" }).click();
  await expect(dialog.getByRole("button", { name: "Save follow-up" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Close follow-up dialog" }).click();
  await expect(dialog).not.toBeVisible();
  await row.getByRole("button", { name: "Followed up, still waiting…" }).click();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).not.toBeVisible();
  await row.getByRole("button", { name: "Followed up, still waiting…" }).click();
  await dialog.dispatchEvent("click");
  await expect(dialog).not.toBeVisible();
});


test("handles history navigation and periodic no-op reconciliation", async ({ page, request }) => {
  await createTask(request, "History task");
  await page.clock.install({ time: new Date() });
  await page.goto("/?view=manage");
  await page.evaluate(() => {
    history.pushState({ view: "unknown" }, "", "/?view=unknown");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.locator("#task-workspace")).toBeVisible();
  await expect(page.locator("body")).toHaveClass(/focus-mode/);

  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  const title = page.locator(".task-title").first();
  await title.fill("Dirty history title");
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await expect(page.locator("#toast")).toContainText("Finish editing");
  await title.fill("History task");
  await title.blur();
  await page.clock.fastForward(60_000);
  await expect(page.locator("#task-workspace")).toBeVisible();
});


test("keeps Agent wiring observable without navigating the document", async ({ page }) => {
  await page.goto("/?view=agent");
  const width = page.locator("#width-toggle");
  await width.click();
  await expect(width).toHaveAttribute("aria-checked", "true");

  await page.getByText("Endpoint settings", { exact: true }).click();
  await page.getByRole("button", { name: "Add another endpoint" }).click();
  await expect(page.locator("#profile-id")).toHaveValue("");
  await page.getByLabel("Saved endpoint").selectOption({ index: 0 });
  await page.getByRole("button", { name: "Discover" }).click();
  const model = page.locator("#profile-model");
  await model.click();
  await page.getByRole("option", { name: "browser-test-model", exact: true }).click();
  await expect(model).toHaveValue("browser-test-model");
  await page.getByText("Endpoint settings", { exact: true }).click();

  const input = page.getByLabel("Message the agent");
  await input.fill("First selectable conversation");
  await input.press("Enter");
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");
  await page.getByRole("button", { name: "New conversation" }).click();
  await input.fill("Second selectable conversation");
  await input.press("Enter");
  await expect(page.locator("#message-list")).toContainText("browser test agent is ready");
  await page.getByRole("button", { name: "First selectable conversation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "First selectable conversation" })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept("Observed folder"));
  await page.getByRole("button", { name: "+ Folder" }).click();
  await page.getByRole("button", {
    name: "Move First selectable conversation to a folder",
    exact: true,
  }).click();
  await expect(page.locator("#move-conversation-dialog option")).toHaveCount(2);
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", {
    name: "Move First selectable conversation to a folder",
    exact: true,
  }).click();
  const moveDialog = page.locator("#move-conversation-dialog");
  await moveDialog.getByLabel("Folder").selectOption({ label: "Observed folder" });
  await moveDialog.getByRole("button", { name: "Move" }).click();
  await expect(
    page.locator(".session-group").filter({
      has: page.getByRole("heading", { name: "Observed folder" }),
    }).getByRole("button", { name: "First selectable conversation", exact: true }),
  ).toBeVisible();
});


test("updates inline task state and rolls back a failed label mutation", async ({ page, request }) => {
  const blocked = await createTask(request, "Inline state target");
  const blocker = await createTask(request, "Inline blocker");
  expect((await request.post(`/api/tasks/${blocked.id}/dependencies`, {
    data: { blocker_task_id: blocker.id },
  })).status()).toBe(201);
  expect((await request.post(`/api/tasks/${blocker.id}/labels`, {
    data: { name: "alpha" },
  })).status()).toBe(201);
  expect((await request.post(`/api/tasks/${blocker.id}/labels`, {
    data: { name: "beta" },
  })).status()).toBe(201);
  await page.goto("/?view=manage");
  const row = page.locator(`.task-row[data-task-id="${blocked.id}"]`);

  const title = row.locator(".task-title");
  await title.fill("Inline state updated");
  const titleUpdate = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && response.url().endsWith(`/api/tasks/${blocked.id}`)
  ));
  await title.evaluate((control) => control.form.requestSubmit());
  expect((await titleUpdate).ok()).toBeTruthy();
  await expect(title).toHaveValue("Inline state updated");
  await expect(row).toHaveAttribute("data-unresolved-blocker-ids", String(blocker.id));

  const statusForm = row.locator(".status-select").locator("xpath=..");
  await statusForm.evaluate((form) => {
    form.addEventListener("submit", (event) => event.preventDefault(), { capture: true });
  });
  await statusForm.locator(".return-view-field").evaluate((input) => { input.value = "focus"; });
  await row.locator(".status-select").selectOption("in_progress");
  await expect(statusForm.locator(".return-view-field")).toHaveValue("manage");

  await row.getByRole("button", { name: "Show task details" }).click();
  const picker = row.locator(".task-label-picker");
  await picker.locator("summary").click();
  const alpha = picker.locator('[data-task-label-option][data-label-name="alpha"]');
  await page.route(`**/api/tasks/${blocked.id}/labels`, async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic label conflict" }),
    });
  });
  await alpha.click();
  await expect(page.locator("#toast")).toContainText("Synthetic label conflict");
  await expect(alpha).not.toBeChecked();
  await page.unroute(`**/api/tasks/${blocked.id}/labels`);
  const beta = picker.locator('[data-task-label-option][data-label-name="beta"]');
  const betaAdded = page.waitForResponse((response) => (
    response.request().method() === "POST"
    && response.url().endsWith(`/api/tasks/${blocked.id}/labels`)
  ));
  await beta.check();
  expect((await betaAdded).status()).toBe(201);
  await expect(row.getByRole("button", { name: "Filter tasks by beta" })).toBeVisible();
  await row.getByRole("button", { name: "Hide task details" }).click();
  await expect(row.locator(".task-details")).toBeHidden();

  await page.getByRole("button", { name: "Add a Task..." }).click();
  const newTaskForm = page.locator("#new-task-form");
  await newTaskForm.evaluate((form) => {
    form.addEventListener("submit", (event) => event.preventDefault(), { capture: true });
  });
  await newTaskForm.getByLabel("What needs doing?").fill("Locked submission");
  await newTaskForm.getByRole("button", { name: "Save task" }).click();
  await expect(newTaskForm).toHaveAttribute("aria-busy", "true");
  await expect(newTaskForm.getByRole("button", { name: "Save task" })).toBeDisabled();
});


test("surfaces background Agent updates and warnings", async ({ page }) => {
  await page.route("**/api/agent/sessions/*/messages", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1600));
    await route.continue();
  });
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Background response");
  await page.getByRole("button", { name: "Send" }).click();
  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  await expect(page.getByRole("link", { name: /Agent, has updates/ })).toBeVisible();
  await expect(page.locator("#agent-update-badge")).toHaveAttribute("data-kind", "normal");
  await page.getByRole("link", { name: /Agent, has updates/ }).click();
  await expect(page.locator("#agent-update-badge")).toBeHidden();

  await page.getByText("Endpoint settings", { exact: true }).click();
  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  await page.route("**/api/llm-profiles/*", async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "Background endpoint warning" }),
      });
    } else {
      await route.continue();
    }
  });
  await page.locator("#profile-name").evaluate((control) => {
    control.value = "Hidden form update";
    control.form.requestSubmit();
  });
  await expect(page.getByRole("link", { name: /Agent, needs attention/ })).toBeVisible();
  await expect(page.locator("#agent-update-badge")).toHaveAttribute("data-kind", "warning");
});


test("supports complete keyboard model selection and guarded discovery", async ({ page }) => {
  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: ["alpha-model", "beta-model"] }),
    });
  });
  await page.goto("/?view=agent");
  await page.getByText("Endpoint settings", { exact: true }).click();
  const model = page.locator("#profile-model");
  const options = page.locator("#model-options");
  await page.getByRole("button", { name: "Discover" }).click();
  await expect(page.locator("#model-discovery-status")).toContainText("2 models available");

  await model.press("Escape");
  await expect(options).toBeHidden();
  await model.evaluate((control) => control.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  await expect(options).toBeVisible();
  await model.press("Escape");
  await page.getByRole("button", { name: "Refresh" }).focus();
  await model.focus();
  await expect(options).toBeVisible();
  await model.press("ArrowUp");
  await expect(model).toHaveAttribute("aria-activedescendant", "model-option-1");
  await model.press("Escape");
  await model.press("ArrowDown");
  await expect(model).toHaveAttribute("aria-activedescendant", "model-option-0");
  await model.press("ArrowDown");
  await model.press("ArrowUp");
  await model.press("Enter");
  await expect(model).toHaveValue("alpha-model");
  await model.press("x");

  await page.getByLabel("Saved endpoint").evaluate((select) => {
    select.value = "";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator("#profile-id")).toHaveValue("");
  await page.getByRole("button", { name: "Discover" }).dispatchEvent("click");
});


test("restores a new Agent message when no endpoint can create a session", async ({ page, request }) => {
  expect((await request.post("/__e2e__/profiles/clear")).ok()).toBeTruthy();
  await page.goto("/?view=agent");
  const input = page.getByLabel("Message the agent");
  await input.evaluate((control) => { control.disabled = false; });
  await page.getByRole("button", { name: "Send" }).evaluate((control) => { control.disabled = false; });
  await input.fill("Restore this draft");
  await page.locator("#composer").evaluate((form) => form.requestSubmit());
  await expect(page.getByRole("alert")).toContainText("Configure an endpoint");
  await expect(input).toHaveValue("Restore this draft");
  await expect(page.locator("#message-list")).not.toContainText("Restore this draft");
});


test("covers task input, follow-up, and notification alternatives without navigation", async ({ page, request }) => {
  const task = await createTask(request, "Controller alternatives");
  expect((await request.put(`/api/tasks/${task.id}/waiting`, {
    data: {
      person_name: "Mira",
      next_follow_up_on: "2099-02-01",
      next_follow_up_time: "13:15",
    },
  })).ok()).toBeTruthy();
  await page.goto("/?view=manage");

  const theme = page.getByRole("switch", { name: /mode/ });
  await theme.click();
  await theme.click();
  const cleanUnload = await page.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    return window.dispatchEvent(event);
  });
  expect(cleanUnload).toBeTruthy();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("app:notify")));
  await expect(page.locator("#toast")).toHaveText("");

  const row = page.locator(`.task-row[data-task-id="${task.id}"]`);
  await row.getByRole("button", { name: "Show task details" }).click();
  const zone = row.locator(".task-attachment-dropzone");
  await zone.evaluate((element) => {
    element.dispatchEvent(new KeyboardEvent("keydown", {
      key: " ", bubbles: true, cancelable: true,
    }));
    element.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true }));
    element.dispatchEvent(new Event("paste", { bubbles: true, cancelable: true }));
  });
  await zone.locator(".attachment-input").setInputFiles({
    name: "empty-controller.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(0),
  });
  await expect(page.locator("#toast")).toContainText("empty-controller.txt is empty");

  const schedule = row.locator(".follow-up-time-control");
  const date = schedule.locator('input[name="next_follow_up_on"]');
  const time = schedule.locator('input[name="next_follow_up_time"]');
  const toggle = schedule.locator(".toggle-follow-up-time");
  await toggle.click();
  await date.fill("");
  await date.dispatchEvent("change");
  await toggle.click();
  await expect(page.locator("#toast")).toContainText("Choose a follow-up date");
  await date.fill("2099-02-02");
  await toggle.click();
  await expect(time).toBeFocused();

  await row.getByRole("button", { name: "Followed up, still waiting…" }).evaluate((button) => {
    delete button.dataset.personName;
    button.click();
  });
  await expect(page.locator("#follow-up-dialog-person")).toHaveText("Waiting on Mira");
  await page.locator("#follow-up-dialog").getByRole("button", { name: "Cancel" }).click();
  await schedule.evaluate((container) => container.closest("form").reset());
  await page.evaluate(() => Promise.resolve());

  await row.locator(".task-label-picker").evaluate((picker) => {
    picker.dispatchEvent(new Event("change", { bubbles: true }));
  });

  await page.getByRole("button", { name: "Add a Task..." }).click();
  const newTaskDialog = page.locator("#new-task-dialog");
  await newTaskDialog.evaluate((dialog) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["image"], "", { type: "image/png", lastModified: 4 }));
    const event = new Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: transfer });
    dialog.dispatchEvent(event);
  });
  await expect(page.locator("#pending-attachments")).toContainText("clipboard image");
  await page.getByRole("button", { name: "Remove clipboard image" }).click();
  await newTaskDialog.getByRole("button", { name: "Cancel" }).click();

  const currentView = page.getByRole("link", { name: "All Tasks" });
  await currentView.dispatchEvent("click", { button: 0, ctrlKey: true });
  await currentView.dispatchEvent("click", { button: 0 });
});


test("rolls ranking back on failure and ignores incomplete drag operations", async ({ page, request }) => {
  await createTask(request, "Rank guard one");
  await createTask(request, "Rank guard two");
  await createTask(request, "Rank guard three");
  await page.goto("/?view=manage");
  await page.getByLabel("Sort tasks").selectOption("rank");
  const list = page.locator("#task-list");
  const rows = page.locator(".task-row");

  await list.dispatchEvent("dragstart");
  await list.dispatchEvent("dragover");
  await list.dispatchEvent("drop");
  await list.dispatchEvent("dragend");
  await rows.first().locator(".rank-handle").press("Enter");
  await rows.first().locator(".rank-handle").press("ArrowUp");
  await rows.last().locator(".rank-handle").press("ArrowDown");

  await page.route("**/api/tasks/*/rank", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic rank conflict" }),
    });
  });
  const titlesBefore = await page.locator(".task-row .task-title").evaluateAll(
    (titles) => titles.map((title) => title.value),
  );
  await rows.first().locator(".rank-handle").press("ArrowDown");
  await expect(page.locator("#toast")).toContainText("Synthetic rank conflict");
  await expect(page.locator(".task-row .task-title")).toHaveCount(3);
  expect(await page.locator(".task-row .task-title").evaluateAll(
    (titles) => titles.map((title) => title.value),
  )).toEqual(titlesBefore);

  await page.unroute("**/api/tasks/*/rank");
  await rows.first().locator(".rank-handle").evaluate((handle) => {
    const transfer = new DataTransfer();
    handle.dispatchEvent(new DragEvent("dragstart", {
      bubbles: true,
      cancelable: true,
      dataTransfer: transfer,
    }));
    handle.dispatchEvent(new DragEvent("dragend", { bubbles: true, dataTransfer: transfer }));
  });
  expect(await page.locator(".task-row .task-title").evaluateAll(
    (titles) => titles.map((title) => title.value),
  )).toEqual(titlesBefore);

  const insertBeforeUpdate = page.waitForResponse((response) => (
    response.request().method() === "PATCH" && response.url().endsWith("/rank")
  ));
  await rows.last().locator(".rank-handle").dragTo(rows.first().locator(".rank-handle"));
  expect((await insertBeforeUpdate).ok()).toBeTruthy();
});


test("reports Agent folder and conversation load failures", async ({ page }) => {
  await page.goto("/?view=agent");
  await page.locator("#composer").evaluate((form) => form.requestSubmit());
  const session = await createAgentConversation(page, "Organization failure recovery");
  await session.click();
  await session.click();

  await page.route("**/api/agent/folders", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "Synthetic folder conflict" }),
      });
    } else {
      await route.continue();
    }
  });
  page.once("dialog", (dialog) => dialog.accept("Broken folder"));
  await page.getByRole("button", { name: "+ Folder" }).click();
  await expect(page.getByRole("alert")).toContainText("Synthetic folder conflict");
  await page.unroute("**/api/agent/folders");

  const folderHeading = await createAgentFolder(page, "Movable folder");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Remove folder Movable folder" }).click();
  await expect(folderHeading).toBeVisible();

  await page.route("**/api/agent/folders/*", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic folder removal conflict" }),
    });
  });
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Remove folder Movable folder" }).click();
  await expect(page.getByRole("alert")).toContainText("Synthetic folder removal conflict");
  await page.unroute("**/api/agent/folders/*");

  await page.route("**/api/agent/sessions/*", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic conversation load failure" }),
    });
  });
  await session.click();
  await expect(page.getByRole("alert")).toContainText("Synthetic conversation load failure");
});


test("guards an in-flight Agent conversation move", async ({ page }) => {
  await page.goto("/?view=agent");
  await createAgentConversation(page, "Move locking recovery");
  await createAgentFolder(page, "Movable folder");

  const moveButton = page.getByRole("button", {
    name: "Move Move locking recovery to a folder",
    exact: true,
  });
  const moveDialog = page.locator("#move-conversation-dialog");
  await moveButton.click();
  await moveDialog.getByLabel("Folder").selectOption({ label: "Movable folder" });
  await moveDialog.getByRole("button", { name: "Move" }).click();
  await expect(moveDialog).not.toBeVisible();
  await expect(
    page.locator(".session-group").filter({
      has: page.getByRole("heading", { name: "Movable folder" }),
    }).getByRole("button", { name: "Move locking recovery", exact: true }),
  ).toBeVisible();

  await moveButton.click();
  await expect(moveDialog.getByLabel("Folder")).toHaveValue(/\d+/);
  await moveDialog.getByLabel("Folder").selectOption("");
  let releaseMove;
  const moveGate = new Promise((resolve) => { releaseMove = resolve; });
  let moveRequests = 0;
  await page.route("**/api/agent/sessions/*", async (route) => {
    if (route.request().method() === "PATCH") {
      moveRequests += 1;
      await moveGate;
    }
    await route.continue();
  });
  const moved = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && /\/api\/agent\/sessions\/[^/]+$/.test(new URL(response.url()).pathname)
  ));
  const moveSubmit = moveDialog.getByRole("button", { name: "Move" });
  try {
    await moveSubmit.evaluate((button) => {
      button.click();
      button.closest("form").requestSubmit();
    });
    await expect.poll(() => moveRequests).toBe(1);
    await expect(moveSubmit).toBeDisabled();
    await expect(moveDialog.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(moveDialog).toBeVisible();
    releaseMove();
    expect((await moved).ok()).toBeTruthy();
  } finally {
    releaseMove();
    await page.unroute("**/api/agent/sessions/*");
  }
  await expect(moveDialog).not.toBeVisible();
  expect(moveRequests).toBe(1);
});


test("recovers after an Agent conversation move fails", async ({ page }) => {
  await page.goto("/?view=agent");
  await createAgentConversation(page, "Move failure recovery");
  await createAgentFolder(page, "Movable folder");
  const moveButton = page.getByRole("button", {
    name: "Move Move failure recovery to a folder",
    exact: true,
  });
  const moveDialog = page.locator("#move-conversation-dialog");

  await page.route("**/api/agent/sessions/*", async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ error: "Synthetic move conflict" }),
      });
    } else {
      await route.continue();
    }
  });
  await moveButton.click();
  await moveDialog.getByLabel("Folder").selectOption({ label: "Movable folder" });
  await moveDialog.getByRole("button", { name: "Move" }).click();
  await expect(page.getByRole("alert")).toContainText("Synthetic move conflict");
  await expect(moveDialog.getByLabel("Folder")).toBeEnabled();
  await expect(moveDialog.getByRole("button", { name: "Move" })).toBeEnabled();
  await expect(moveDialog.getByRole("button", { name: "Cancel" })).toBeEnabled();
  await page.unroute("**/api/agent/sessions/*");

  await moveDialog.getByRole("button", { name: "Move" }).click();
  await expect(moveDialog).not.toBeVisible();
  await expect(
    page.locator(".session-group").filter({
      has: page.getByRole("heading", { name: "Movable folder" }),
    }).getByRole("button", { name: "Move failure recovery", exact: true }),
  ).toBeVisible();
});


test("updates focus after dynamic task changes", async ({ page, request }) => {
  const active = await createTask(request, "Dynamic active task");
  await createTask(request, "Another dynamic task");
  expect((await request.patch(`/api/tasks/${active.id}`, {
    data: { status: "in_progress", due_date: "2000-01-01" },
  })).ok()).toBeTruthy();

  await page.goto("/?view=focus");
  await page.locator("#aur-bataao-button").dispatchEvent("click");
  const focusTaskId = await page.locator(".task-row.is-focus-task").getAttribute("data-task-id");
  expect(focusTaskId).toBeTruthy();
  const focusRow = page.locator(`.task-row[data-task-id="${focusTaskId}"]`);
  await page.locator(".task-row:not(.is-focus-task)").evaluateAll((rows) => {
    rows.forEach((row) => { row.dataset.actionable = "false"; });
  });
  await page.locator("#aur-bataao-button").dispatchEvent("click");
  await focusRow.getByRole("button", {
    name: "Expand task details and editing controls",
  }).click();
  const focusTitle = focusRow.locator(".focus-title-editor");
  const titleUpdated = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && response.url().endsWith(`/api/tasks/${focusTaskId}`)
  ));
  await focusTitle.fill("Dynamic focus updated");
  await focusTitle.blur();
  expect((await titleUpdated).ok()).toBeTruthy();
  await expect(focusRow.locator(".focus-task-title")).toHaveText("Dynamic focus updated");
  await expect(page.locator("#active-task-count")).toContainText("1 in progress");
  const focusReturnView = focusRow.locator(".return-view-field").first();
  await focusReturnView.evaluate((input) => input.closest("form").dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  ));
  await expect(focusReturnView).toHaveValue("focus");

  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  const activeRow = page.locator(`.task-row[data-task-id="${active.id}"]`);
  if (await activeRow.locator(".task-details").isHidden()) {
    await activeRow.getByRole("button", { name: "Show task details" }).click();
  }
});


test("updates labels after dynamic task changes", async ({ page, request }) => {
  const active = await createTask(request, "Dynamic labelled task");
  expect((await request.post(`/api/tasks/${active.id}/labels`, { data: { name: "alpha" } })).ok()).toBeTruthy();
  expect((await request.post(`/api/tasks/${active.id}/labels`, { data: { name: "beta" } })).ok()).toBeTruthy();
  expect((await request.post(`/api/tasks/${active.id}/labels`, { data: { name: "gamma" } })).ok()).toBeTruthy();
  await page.goto("/?view=manage");

  const activeRow = page.locator(`.task-row[data-task-id="${active.id}"]`);
  await activeRow.getByRole("button", { name: "Show task details" }).click();
  const picker = activeRow.locator(".task-label-picker");
  await picker.locator("summary").click();
  await page.locator("#label-filter .multi-select-option").filter({
    hasText: "alpha",
  }).locator("input").evaluate((checkbox) => {
    checkbox.checked = false;
    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await picker.locator('[data-task-label-option][data-label-name="alpha"]').uncheck();
  await expect(activeRow.getByRole("button", { name: "Filter tasks by beta" })).toBeVisible();
  await expect(activeRow.getByRole("button", { name: "Filter tasks by alpha" })).toHaveCount(0);
  await activeRow.getByRole("button", { name: "Filter tasks by beta" }).click();
  await picker.locator("summary").click();
  await picker.locator('[data-task-label-option][data-label-name="beta"]').uncheck();
  await expect(activeRow.getByRole("button", { name: "Filter tasks by gamma" })).toBeVisible();
});


test("updates counters and return views after dynamic task changes", async ({ page, request }) => {
  const active = await createTask(request, "Dynamic active task");
  const waiting = await createTask(request, "Dynamic waiting task");
  const another = await createTask(request, "Another dynamic task");
  expect((await request.patch(`/api/tasks/${active.id}`, {
    data: { status: "in_progress", due_date: "2000-01-01" },
  })).ok()).toBeTruthy();
  expect((await request.put(`/api/tasks/${waiting.id}/waiting`, {
    data: { person_name: "Nia", next_follow_up_on: "2099-03-01" },
  })).ok()).toBeTruthy();
  await page.goto("/?view=manage");

  const activeRow = page.locator(`.task-row[data-task-id="${active.id}"]`);
  const waitingRow = page.locator(`.task-row[data-task-id="${waiting.id}"]`);
  await activeRow.getByRole("button", { name: "Show task details" }).click();
  await waitingRow.getByRole("button", { name: "Show task details" }).click();
  const waitingTitle = waitingRow.locator(".task-title");
  const waitingUpdated = page.waitForResponse((response) => (
    response.request().method() === "PATCH"
    && response.url().endsWith(`/api/tasks/${waiting.id}`)
  ));
  await waitingTitle.fill("Dynamic waiting updated");
  await waitingTitle.blur();
  expect((await waitingUpdated).ok()).toBeTruthy();
  await expect(waitingRow).toHaveAttribute("data-title", "dynamic waiting updated");

  await page.evaluate(({ waitingId, anotherId }) => {
    document.querySelector(`.task-row[data-task-id="${waitingId}"]`).dataset.followUpDue = "true";
    document.querySelector(`.task-row[data-task-id="${anotherId}"]`).dataset.followUpDue = "true";
  }, { waitingId: waiting.id, anotherId: another.id });
  const description = activeRow.locator("textarea[data-field='description']");
  await description.evaluate((control) => { delete control.dataset.previous; });
  await description.fill("Refresh live counts");
  await description.blur();
  await expect(page.locator("#active-task-count")).toContainText("2 need follow-up");
  await page.evaluate((taskId) => {
    document.querySelector(`.task-row[data-task-id="${taskId}"]`).dataset.followUpDue = "false";
  }, another.id);
  await description.fill("Refresh one live count");
  await description.blur();
  await expect(page.locator("#active-task-count")).toContainText("1 needs follow-up");

  await page.locator("#view-blocked-tasks").dispatchEvent("click");
  const statusForm = waitingRow.locator(".status-select").locator("xpath=..");
  await statusForm.evaluate((form) => {
    form.addEventListener("submit", (event) => event.preventDefault(), { capture: true });
  });
  await waitingRow.locator(".status-select").selectOption("in_progress");
  await expect(statusForm.locator(".return-view-field")).toHaveValue("blocked");

  await page.getByRole("link", { name: "Switch to Agent" }).click();
  const returnView = page.locator(".inline-action-form [name='return_view']").first();
  await returnView.evaluate((input) => input.closest("form").dispatchEvent(
    new Event("submit", { bubbles: true, cancelable: true }),
  ));
  await expect(returnView).toHaveValue("blocked");
});


test("refreshes stale task views and reports reconciliation failures", async ({ page, request }) => {
  await createTask(request, "Stale task view");
  await page.clock.install({ time: new Date() });
  await page.goto("/?view=agent");
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("agent:tasks-mutated")));
  await Promise.all([
    page.waitForURL(/view=manage/),
    page.getByRole("link", { name: "Switch to All Tasks" }).click(),
  ]);
  await expect(page.locator(".task-title")).toHaveValue("Stale task view");

  await page.route("**/api/reconcile", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic reconciliation failure" }),
    });
  });
  await page.evaluate(() => { document.body.dataset.today = "1900-01-01"; });
  await page.clock.fastForward(60_000);
  await expect(page.locator("#toast")).toContainText("Synthetic reconciliation failure");

  await page.evaluate(() => {
    history.pushState({}, "", "/");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.locator("body")).toHaveClass(/focus-mode/);
  await page.evaluate(() => {
    history.pushState({ view: "manage" }, "", "/?view=manage");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.locator("body")).toHaveClass(/manage-mode/);
});


test("ignores stale model discovery success and failure", async ({ page }) => {
  let releaseSuccess;
  const successGate = new Promise((resolve) => { releaseSuccess = resolve; });
  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await successGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: ["stale-model"] }),
    });
  });
  await page.goto("/?view=agent");
  await page.getByText("Endpoint settings", { exact: true }).click();
  const discover = page.getByRole("button", { name: "Discover" });
  await discover.click();
  await expect(page.locator("#model-discovery-status")).toContainText("Discovering models");
  await page.getByLabel("Base URL").fill("http://changed-before-success.invalid/v1");
  const staleSuccess = page.waitForResponse((response) => response.url().endsWith("/models"));
  releaseSuccess();
  await staleSuccess;
  await page.evaluate(() => Promise.resolve());
  await expect(page.locator("#model-discovery-status")).toContainText("Save endpoint changes");

  await page.unroute("**/api/llm-profiles/*/models");
  await page.reload();
  await page.getByText("Endpoint settings", { exact: true }).click();
  let releaseFailure;
  const failureGate = new Promise((resolve) => { releaseFailure = resolve; });
  await page.route("**/api/llm-profiles/*/models", async (route) => {
    await failureGate;
    await route.abort("failed");
  });
  await page.getByRole("button", { name: "Discover" }).click();
  await page.getByLabel("Base URL").fill("http://changed-before-failure.invalid/v1");
  const staleFailure = page.waitForEvent("requestfailed", {
    predicate: (request) => request.url().endsWith("/models"),
  });
  releaseFailure();
  await staleFailure;
  await page.evaluate(() => Promise.resolve());
  await expect(page.locator("#model-discovery-status")).toContainText("Save endpoint changes");
});


test("reports approval, resume, and Agent initialization failures", async ({ page, request }) => {
  await page.goto("/?view=agent");
  await page.getByLabel("Message the agent").fill("Create a task for failed approval");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Allow create_task?" })).toBeVisible();
  await page.route("**/api/agent/approvals/*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 100));
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic approval failure" }),
    });
  });
  const approve = page.getByRole("button", { name: "Approve" });
  await approve.evaluate((button) => {
    button.click();
    button.click();
  });
  await expect(page.getByRole("alert")).toContainText("Synthetic approval failure");

  expect((await request.post("/__e2e__/agent/interrupted")).ok()).toBeTruthy();
  await page.goto("/?view=agent");
  await page.getByRole("button", { name: "Interrupted approval", exact: true }).click();
  await page.route("**/api/agent/runs/*/resume", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 100));
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic resume failure" }),
    });
  });
  const resume = page.getByRole("button", { name: "Resume" });
  await resume.evaluate((button) => {
    button.click();
    button.click();
  });
  await expect(page.getByRole("alert")).toContainText("Synthetic resume failure");

  await page.unrouteAll();
  await page.route("**/api/llm-profiles", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "Synthetic initialization failure" }),
    });
  });
  await page.goto("/?view=agent");
  await expect(page.getByRole("alert")).toContainText("Synthetic initialization failure");
});
