import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";


const root = path.dirname(fileURLToPath(import.meta.url));
const virtualEnvironmentPython = process.platform === "win32"
  ? path.join(root, ".venv", "Scripts", "python.exe")
  : path.join(root, ".venv", "bin", "python");
const python = process.env.E2E_PYTHON
  || (existsSync(virtualEnvironmentPython) ? virtualEnvironmentPython : "python");
const baseURL = "http://127.0.0.1:4173";
const browserCoverageGate = process.env.BROWSER_COVERAGE_GATE === "1"
  || process.env.npm_lifecycle_event === "test:e2e:coverage";
const reporters = process.env.CI
  ? [["line"], ["html", { open: "never" }]]
  : [["line"]];
reporters.unshift(["./tests-e2e/browser_coverage_reporter.js", { gate: browserCoverageGate }]);


export default defineConfig({
  testDir: "./tests-e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 20_000,
  expect: { timeout: 5_000 },
  outputDir: "test-results/playwright",
  reporter: reporters,
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `"${python}" tests/e2e_server.py`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: "ignore",
    stderr: "pipe",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: "chromium" },
    },
  ],
});
