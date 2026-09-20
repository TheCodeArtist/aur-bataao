import assert from "node:assert/strict";
import { test } from "node:test";

import {
  absoluteTimestamp,
  conversationTimestamp,
  formattedToolValue,
  parseTimestamp,
} from "../static/agent_logic.js";


test("timestamp parsing and absolute labels handle invalid values", () => {
  assert.equal(parseTimestamp("not-a-date"), null);
  assert.equal(parseTimestamp("2026-09-20T10:00:00").getFullYear(), 2026);
  assert.equal(absoluteTimestamp("bad", "en-US"), "Time unavailable");
  assert.match(absoluteTimestamp("2026-09-20T10:00:00", "en-US"), /2026/);
});


test("conversation labels distinguish recent and older dates", () => {
  const now = new Date(2026, 8, 20, 12, 0);
  assert.equal(conversationTimestamp("bad", now, "en-US"), "");
  assert.match(conversationTimestamp("2026-09-20T10:00:00", now, "en-US"), /^Today,/);
  assert.match(conversationTimestamp("2026-09-19T10:00:00", now, "en-US"), /^Yesterday,/);
  assert.equal(conversationTimestamp("2026-08-01T10:00:00", now, "en-US"), "Aug 1");
  assert.equal(conversationTimestamp("2025-08-01T10:00:00", now, "en-US"), "Aug 1, 2025");
});


test("tool values are readable without trusting their input shape", () => {
  assert.equal(formattedToolValue('{"title":"Task"}'), '{\n  "title": "Task"\n}');
  assert.equal(formattedToolValue({ count: 2 }), '{\n  "count": 2\n}');
  assert.equal(formattedToolValue("not json"), "not json");
  assert.equal(formattedToolValue(null), "null");
  assert.equal(formattedToolValue(undefined), undefined);
});
