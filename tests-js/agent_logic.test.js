import assert from "node:assert/strict";
import { test } from "node:test";

import {
  absoluteTimestamp,
  agentBadgeState,
  approvalState,
  boundedOptionIndex,
  connectionStatus,
  conversationTimestamp,
  filterModels,
  formattedToolValue,
  modelDiscoveryState,
  normalizeDiscoveredModels,
  parseTimestamp,
  profileFormValues,
  selectProfile,
  startSessionPolling,
  toolCallState,
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
  const circular = {};
  circular.self = circular;
  assert.equal(formattedToolValue(circular), "[object Object]");
});


test("profile decisions describe connection and form state", () => {
  const profiles = [
    { id: 1, name: "Local", model: "small", api_key_env: "", api_key_configured: false },
    { id: 2, name: "Cloud", model: "large", api_key_env: "API_KEY", api_key_configured: false },
    { id: 3, name: "Ready", model: "medium", api_key_env: "API_KEY", api_key_configured: true },
  ];
  assert.equal(connectionStatus(profiles, "missing"), "Configure an endpoint to begin.");
  assert.equal(connectionStatus(profiles, "1"), "Local · small");
  assert.equal(connectionStatus(profiles, "2"), "Cloud · large · key variable missing");
  assert.equal(connectionStatus(profiles, "3"), "Ready · medium");

  assert.deepEqual(profileFormValues(null, true), {
    id: "",
    name: "",
    baseUrl: "http://127.0.0.1:1234/v1",
    model: "",
    apiKeyEnv: "",
    timeoutSeconds: 60,
    supportsTools: true,
    isDefault: true,
  });
  assert.deepEqual(profileFormValues({
    id: 4,
    name: "Explicit",
    base_url: "https://example.test/v1",
    model: "model",
    api_key_env: "KEY",
    timeout_seconds: 12,
    supports_tools: false,
    is_default: false,
  }), {
    id: 4,
    name: "Explicit",
    baseUrl: "https://example.test/v1",
    model: "model",
    apiKeyEnv: "KEY",
    timeoutSeconds: 12,
    supportsTools: false,
    isDefault: false,
  });
  assert.equal(profileFormValues(null, false).isDefault, false);
});


test("profile selection follows explicit, default, first, and empty precedence", () => {
  const profiles = [{ id: 1 }, { id: 2, is_default: true }, { id: 3 }];
  assert.equal(selectProfile(profiles, 3), profiles[2]);
  assert.equal(selectProfile(profiles, 99), profiles[1]);
  assert.equal(selectProfile([{ id: 1 }, { id: 2 }], 99).id, 1);
  assert.equal(selectProfile([], 99), null);
});


test("model discovery normalizes, filters, and describes results", () => {
  assert.throws(() => normalizeDiscoveredModels({}), /invalid model list/);
  assert.deepEqual(
    normalizeDiscoveredModels(["Alpha", "Alpha", "", "   ", null, 4, "Beta"]),
    ["Alpha", "Beta"],
  );
  assert.deepEqual(filterModels(["Alpha", "beta"], "  ALP "), ["Alpha"]);
  const models = ["Alpha", "beta"];
  assert.equal(filterModels(models), models);

  assert.deepEqual(modelDiscoveryState(null), {
    disabled: true,
    buttonLabel: "Discover",
    status: "Save the endpoint to discover its models.",
  });
  assert.deepEqual(modelDiscoveryState({ id: 1 }), {
    disabled: false,
    buttonLabel: "Discover",
    status: "Discover models from this endpoint, or enter a model ID manually.",
  });
  assert.match(modelDiscoveryState({ id: 1 }, []).status, /^0 models/);
  assert.match(modelDiscoveryState({ id: 1 }, ["only"]).status, /^1 model available/);
  assert.match(modelDiscoveryState({ id: 1 }, ["one", "two"]).status, /^2 models available/);
});


test("agent badges distinguish normal updates, warnings, and current view", () => {
  assert.deepEqual(agentBadgeState({ warningCount: 0, normalUpdate: false, currentPage: false }), {
    visible: false, kind: "", label: "Switch to Agent",
  });
  assert.deepEqual(agentBadgeState({ warningCount: 0, normalUpdate: true, currentPage: false }), {
    visible: true, kind: "normal", label: "Switch to Agent, has updates",
  });
  assert.deepEqual(agentBadgeState({ warningCount: 2, normalUpdate: true, currentPage: true }), {
    visible: true, kind: "warning", label: "Agent, needs attention",
  });
});


test("model option indexes are bounded", () => {
  assert.equal(boundedOptionIndex(0, 4), null);
  assert.equal(boundedOptionIndex(3, -1), 0);
  assert.equal(boundedOptionIndex(3, 1), 1);
  assert.equal(boundedOptionIndex(3, 9), 2);
});


test("tool call and approval states cover every run outcome", () => {
  const call = { id: "call-1", function: { name: "create_task", arguments: "{}" } };
  assert.deepEqual(toolCallState(call, "waiting_approval"), {
    id: "call-1",
    name: "create_task",
    arguments: "{}",
    summary: "Waiting for approval to run create_task…",
  });
  assert.equal(toolCallState(call, "failed").summary, "Did not finish create_task");
  assert.equal(toolCallState(call, "cancelled").summary, "Did not finish create_task");
  assert.equal(toolCallState(call, "running").summary, "Running create_task…");
  assert.deepEqual(toolCallState(null, null), {
    id: "", name: "tool", arguments: {}, summary: "Running tool…",
  });

  const pending = { id: 1, status: "pending" };
  assert.deepEqual(approvalState([pending], "run-1"), {
    pending: [pending], canResume: false,
  });
  assert.deepEqual(approvalState([{ status: "approved" }], "run-1"), {
    pending: [], canResume: true,
  });
  assert.equal(approvalState([{ status: "rejected" }]).canResume, false);
  assert.equal(approvalState([{ status: "consumed" }], "run-1").canResume, false);
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


test("session polling does not render an in-flight result after stop", async () => {
  let refresh;
  let resolveLoad;
  const rendered = [];
  const stop = startSessionPolling({
    sessionId: "current",
    currentSessionId: () => "current",
    load: () => new Promise((resolve) => { resolveLoad = resolve; }),
    render: (detail) => rendered.push(detail),
    schedule: (callback) => {
      refresh = callback;
      return 9;
    },
    cancel: () => {},
  });

  const pending = refresh();
  stop();
  resolveLoad({ messages: ["too late"] });
  await pending;
  assert.deepEqual(rendered, []);
});
