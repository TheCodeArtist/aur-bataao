import { expect, test as base } from "@playwright/test";


const test = base.extend({
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

  await page.getByLabel("Sort tasks").selectOption("rank");
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


test("agent approval mutates tasks and returns to the task view", async ({ page }) => {
  await page.goto("/?view=agent");
  await expect(page.locator("#connection-status")).toContainText("Browser test endpoint");
  await page.getByLabel("Message the agent").fill("Create a task from the browser");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("heading", { name: "Allow create_task?" })).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator("#message-list")).toContainText("browser-agent task was added");

  await page.getByRole("link", { name: "Switch to All Tasks" }).click();
  await expect(page).toHaveURL(/view=manage/);
  await expect(page.locator('.task-title[value="Browser agent task"]')).toBeVisible();
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
