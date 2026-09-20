import assert from "node:assert/strict";
import { test } from "node:test";

import BrowserCoverageReporter, {
  BROWSER_COVERAGE_ATTACHMENT,
} from "../tests-e2e/browser_coverage_reporter.js";


function withoutConsole(callback) {
  const originalLog = console.log;
  const originalError = console.error;
  console.log = () => {};
  console.error = () => {};
  try {
    return callback();
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }
}


test("browser coverage reporter merges attachments across test results", () => {
  const reporter = new BrowserCoverageReporter();
  const entry = { url: "http://127.0.0.1/static/app.js", source: "", functions: [] };
  reporter.onTestEnd(null, {
    attachments: [
      { name: "unrelated", body: Buffer.from("ignored") },
      { name: BROWSER_COVERAGE_ATTACHMENT, body: Buffer.from(JSON.stringify([entry])) },
    ],
  });
  assert.deepEqual(reporter.entries, [entry]);
  assert.equal(withoutConsole(() => reporter.onEnd({ status: "failed" })), undefined);
});


test("browser coverage reporter gates only complete successful runs", () => {
  const diagnostic = new BrowserCoverageReporter();
  assert.equal(withoutConsole(() => diagnostic.onEnd({ status: "passed" })), undefined);

  const malformed = new BrowserCoverageReporter({ gate: true });
  for (const body of [Buffer.from("not json"), Buffer.from("{}"), undefined]) {
    malformed.onTestEnd(null, {
      attachments: [{ name: BROWSER_COVERAGE_ATTACHMENT, body }],
    });
  }
  assert.deepEqual(withoutConsole(() => malformed.onEnd({ status: "passed" })), {
    status: "failed",
  });

  const gated = new BrowserCoverageReporter({ gate: true });
  assert.deepEqual(withoutConsole(() => gated.onEnd({ status: "passed" })), {
    status: "failed",
  });

  const successful = new BrowserCoverageReporter({
    gate: true,
    summarize: (entries) => entries,
    format: () => "complete coverage",
    assert: () => {},
  });
  assert.equal(withoutConsole(() => successful.onEnd({ status: "passed" })), undefined);

  const messageOnlyFailure = new BrowserCoverageReporter({
    gate: true,
    assert: () => { throw { message: "message-only failure" }; },
  });
  assert.deepEqual(withoutConsole(() => messageOnlyFailure.onEnd({ status: "passed" })), {
    status: "failed",
  });
});
