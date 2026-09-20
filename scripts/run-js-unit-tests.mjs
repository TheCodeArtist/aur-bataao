import { spawn } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { coverageGateError } from "../tests-e2e/unit_coverage_gate.js";


const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const includedSources = [
  "static/markdown.js",
  "static/http.js",
  "static/task_logic.js",
  "static/agent_logic.js",
  "tests-e2e/browser_coverage.js",
  "tests-e2e/unit_coverage_gate.js",
];
const testFiles = readdirSync(path.join(root, "tests-js"))
  .filter((name) => name.endsWith(".test.js"))
  .sort()
  .map((name) => path.join("tests-js", name));
const arguments_ = [
  "--test",
  "--experimental-test-coverage",
  ...includedSources.map((source) => `--test-coverage-include=${source}`),
  "--test-coverage-lines=100",
  "--test-coverage-branches=100",
  "--test-coverage-functions=100",
  ...testFiles,
];

const child = spawn(process.execPath, arguments_, {
  cwd: root,
  env: process.env,
  stdio: ["inherit", "pipe", "pipe"],
});
let output = "";
child.stdout.setEncoding("utf8");
child.stderr.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  output += chunk;
  process.stdout.write(chunk);
});
child.stderr.on("data", (chunk) => {
  output += chunk;
  process.stderr.write(chunk);
});

const outcome = await new Promise((resolve) => {
  child.once("error", (error) => resolve({ error, status: null }));
  child.once("close", (status) => resolve({ error: null, status }));
});
if (outcome.error) {
  console.error(`Could not run JavaScript unit tests: ${outcome.error.message}`);
  process.exit(1);
}
if (outcome.status !== 0) process.exit(outcome.status ?? 1);

const gateError = coverageGateError(output);
if (gateError) {
  console.error(gateError);
  process.exit(1);
}
