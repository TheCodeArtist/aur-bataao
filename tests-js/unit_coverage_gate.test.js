import assert from "node:assert/strict";
import { test } from "node:test";

import { coverageGateError } from "../tests-e2e/unit_coverage_gate.js";


test("unit coverage gate requires a complete report", () => {
  assert.equal(
    coverageGateError("start of coverage report\nend of coverage report"),
    null,
  );
  assert.match(
    coverageGateError("Warning: Code coverage could not be enabled."),
    /could not be enabled/,
  );
  assert.match(coverageGateError(""), /complete coverage report/);
  assert.match(
    coverageGateError("start of coverage report"),
    /complete coverage report/,
  );
  assert.match(
    coverageGateError("end of coverage report"),
    /complete coverage report/,
  );
});
