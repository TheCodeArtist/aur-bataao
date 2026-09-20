import assert from "node:assert/strict";
import { test } from "node:test";

import {
  absoluteTimestamp,
  conversationTimestamp,
  formattedToolValue,
  parseTimestamp,
  startSessionPolling,
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


test("session polling prevents overlap and ignores stale results", async () => {
  let refresh;
  let resolveLoad;
  let activeSession = "one";
  let loads = 0;
  const rendered = [];
  const cancelled = [];
  const stop = startSessionPolling({
    sessionId: "one",
    currentSessionId: () => activeSession,
    load: async () => {
      loads += 1;
      return new Promise((resolve) => { resolveLoad = resolve; });
    },
    render: (detail) => rendered.push(detail),
    schedule: (callback, interval) => {
      assert.equal(interval, 750);
      refresh = callback;
      return 41;
    },
    cancel: (timer) => cancelled.push(timer),
  });

  const firstRefresh = refresh();
  await refresh();
  assert.equal(loads, 1);
  activeSession = "two";
  resolveLoad({ messages: [] });
  await firstRefresh;
  assert.deepEqual(rendered, []);
  await refresh();
  assert.equal(loads, 1);
  stop();
  assert.deepEqual(cancelled, [41]);
});


test("session polling renders current results and treats failures as best effort", async () => {
  let refresh;
  let shouldFail = false;
  const rendered = [];
  const stop = startSessionPolling({
    sessionId: "current",
    currentSessionId: () => "current",
    load: async () => {
      if (shouldFail) throw new Error("temporary");
      return { messages: ["ready"] };
    },
    render: (detail) => rendered.push(detail),
    schedule: (callback) => {
      refresh = callback;
      return 7;
    },
    cancel: () => {},
    interval: 25,
  });
  await refresh();
  assert.deepEqual(rendered, [{ messages: ["ready"] }]);
  shouldFail = true;
  await refresh();
  assert.equal(rendered.length, 1);
  stop();
  await refresh();
  assert.equal(rendered.length, 1);
});
