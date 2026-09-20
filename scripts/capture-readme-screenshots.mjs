import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";


const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const python = process.env.E2E_PYTHON || (
  existsSync(path.join(root, ".venv", "Scripts", "python.exe"))
    ? path.join(root, ".venv", "Scripts", "python.exe")
    : existsSync(path.join(root, ".venv", "bin", "python"))
      ? path.join(root, ".venv", "bin", "python")
      : "python"
);
const port = 4174;
const baseURL = `http://127.0.0.1:${port}`;
const outputDirectory = path.join(root, "docs", "screenshots");
const edgeExecutable = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";


function isoDate(daysFromToday) {
  const value = new Date();
  value.setUTCDate(value.getUTCDate() + daysFromToday);
  return value.toISOString().slice(0, 10);
}


async function waitForServer(process) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(`Screenshot server exited with code ${process.exitCode}`);
    }
    try {
      const response = await fetch(`${baseURL}/`);
      if (response.ok) return;
    } catch (_) {
      // The private fixture server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error("Timed out waiting for the screenshot server");
}


async function json(request, method, pathname, data) {
  const response = await request.fetch(pathname, { method, data });
  if (!response.ok()) {
    throw new Error(`${method} ${pathname} failed: ${response.status()} ${await response.text()}`);
  }
  return response.status() === 204 ? null : response.json();
}


async function createTask(request, title, fields = {}) {
  const result = await json(request, "POST", "/api/tasks", { title });
  if (Object.keys(fields).length) {
    await json(request, "PATCH", `/api/tasks/${result.task.id}`, fields);
  }
  return result.task;
}


async function addLabels(request, task, labels) {
  for (const name of labels) {
    await json(request, "POST", `/api/tasks/${task.id}/labels`, { name });
  }
}


async function seedDemoData(request) {
  await json(request, "POST", "/__e2e__/reset");

  const blocker = await createTask(request, "Approve visual direction", {
    description: "Confirm the final illustration style and homepage colour treatment.",
    due_date: isoDate(1),
  });
  await addLabels(request, blocker, ["Design"]);

  const blocked = await createTask(request, "Update help centre screenshots", {
    description: "Refresh the getting-started guide with the approved launch visuals.",
    due_date: isoDate(4),
  });
  await addLabels(request, blocked, ["Docs"]);
  await json(request, "POST", `/api/tasks/${blocked.id}/dependencies`, {
    blocker_task_id: blocker.id,
  });

  const accessibility = await createTask(request, "Publish accessibility checklist", {
    description: "Turn the audit notes into a practical pre-release checklist for the team.",
    due_date: isoDate(3),
  });
  await addLabels(request, accessibility, ["Accessibility", "Docs"]);

  const onboarding = await createTask(request, "Review onboarding flow", {
    description: "Check empty states, first-run guidance, and keyboard navigation before release.",
    due_date: isoDate(5),
  });
  await addLabels(request, onboarding, ["Product", "UX"]);
  await json(request, "PUT", `/api/tasks/${onboarding.id}/waiting`, {
    person_name: "Maya",
    note: "Waiting for the final usability notes.",
    next_follow_up_on: isoDate(2),
  });
  await json(request, "POST", `/api/tasks/${onboarding.id}/comments`, {
    body: "Prototype review complete; the new first-run copy tested well.",
    counts_as_progress: true,
  });

  const launch = await createTask(request, "Prepare launch announcement", {
    description: "Draft the release story, collect product highlights, and confirm the publishing checklist.",
    due_date: isoDate(2),
    status: "in_progress",
  });
  await addLabels(request, launch, ["Launch", "Marketing"]);
  await json(request, "POST", `/api/tasks/${launch.id}/comments`, {
    body: "First draft is ready for editorial review.",
    counts_as_progress: true,
  });

  return { onboarding };
}


async function main() {
  await mkdir(outputDirectory, { recursive: true });
  const server = spawn(python, [path.join(root, "tests", "e2e_server.py")], {
    cwd: root,
    env: { ...process.env, AUR_BATAAO_E2E_PORT: String(port) },
    stdio: ["ignore", "ignore", "inherit"],
  });

  let browser;
  try {
    await waitForServer(server);
    browser = await chromium.launch(
      process.platform === "win32" && existsSync(edgeExecutable)
        ? { executablePath: edgeExecutable }
        : {},
    );
    const context = await browser.newContext({
      baseURL,
      colorScheme: "light",
      deviceScaleFactor: 1,
      viewport: { width: 1440, height: 900 },
    });
    await context.addInitScript(() => {
      localStorage.setItem("aur-bataao-theme", "light");
      localStorage.setItem("aur-bataao-task-sort", "smart");
    });
    const demo = await seedDemoData(context.request);
    const page = await context.newPage();

    await page.setViewportSize({ width: 1440, height: 480 });
    await page.goto("/?view=focus");
    await page.locator(".task-card.is-focus-task").waitFor();
    await page.screenshot({
      path: path.join(outputDirectory, "focused-view.jpg"),
      clip: { x: 0, y: 0, width: 1440, height: 330 },
      quality: 90,
      type: "jpeg",
    });

    await page.setViewportSize({ width: 1440, height: 600 });
    await page.goto("/?view=manage");
    await page.locator(".task-row").first().waitFor();
    await page.screenshot({
      path: path.join(outputDirectory, "all-tasks-view.jpg"),
      fullPage: false,
      quality: 90,
      type: "jpeg",
    });

    const onboarding = page.locator(`#task-${demo.onboarding.id}`);
    await onboarding.getByRole("button", { name: "Show task details" }).click();
    await onboarding.locator(".task-details").waitFor();
    await onboarding.scrollIntoViewIfNeeded();
    await onboarding.screenshot({
      path: path.join(outputDirectory, "task-details.jpg"),
      quality: 90,
      type: "jpeg",
    });

    await context.close();
  } finally {
    await browser?.close();
    server.kill();
  }
}


await main();
