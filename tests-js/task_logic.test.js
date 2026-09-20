import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appViewNavigation,
  compareRank,
  compareSmart,
  dateTimeInTimezone,
  dragInsertionRow,
  focusViewState,
  formatFileSize,
  mergeVisibleOrder,
  multiSelectState,
  navigationLockState,
  keyboardRankMove,
  normalizeSortMode,
  normalizeTheme,
  nextTask,
  pendingFollowUpBecameDue,
  rankNeighbors,
  sameTaskOrder,
  smartStateOrder,
  taskMatchesFilters,
  uploadFilename,
  validateAttachments,
} from "../static/task_logic.js";


function task(values = {}) {
  return {
    dataset: {
      taskId: "1",
      rankKey: "100",
      status: "todo",
      blocked: "false",
      followUpDue: "false",
      followUpDate: "",
      followUpTime: "09:00",
      dueDate: "",
      waitingOn: "",
      ...values,
    },
    hidden: false,
  };
}


const attachmentLimits = {
  maxAttachmentBytes: 1024,
  maxAttachmentsPerTask: 3,
  maxUploadBytes: 2048,
};


test("formats sizes and generates stable upload names", () => {
  assert.equal(formatFileSize(100), "100 B");
  assert.equal(formatFileSize(2048), "2.0 KB");
  assert.equal(formatFileSize(2 * 1024 * 1024), "2.0 MB");
  assert.equal(uploadFilename({ name: "report.txt" }, 0, 10), "report.txt");
  assert.equal(uploadFilename({ name: "", type: "image/gif" }, 0, 10), "clipboard-10-1.gif");
  assert.equal(uploadFilename({ name: "", type: "image/jpeg" }, 1, 10), "clipboard-10-2.jpg");
  assert.equal(uploadFilename({ name: "", type: "image/png" }, 2, 10), "clipboard-10-3.png");
  assert.equal(uploadFilename({ name: "", type: "image/webp" }, 3, 10), "clipboard-10-4.webp");
  assert.equal(uploadFilename({ name: "", type: "text/plain" }, 4, 10), "clipboard-10-5");
});


test("validates attachment batches in risk order", () => {
  assert.equal(
    validateAttachments([{ name: "empty", size: 0 }], attachmentLimits),
    "empty is empty",
  );
  assert.equal(
    validateAttachments([{ name: "", size: 0 }], attachmentLimits),
    "An attachment is empty",
  );
  assert.equal(
    validateAttachments([{ name: "large", size: 1025 }], attachmentLimits),
    "large is larger than 1.0 KB",
  );
  assert.equal(
    validateAttachments([{ name: "", size: 1025 }], attachmentLimits),
    "An attachment is larger than 1.0 KB",
  );
  assert.equal(
    validateAttachments(
      [{ name: "third", size: 1 }],
      { ...attachmentLimits, existingCount: 2, pendingFiles: [{ size: 1 }] },
    ),
    "A task can have at most 3 attachments",
  );
  assert.equal(
    validateAttachments(
      [{ name: "second", size: 1024 }],
      { ...attachmentLimits, pendingFiles: [{ size: 1024 }] },
    ),
    "Attach fewer files at once (combined limit 2.0 KB)",
  );
  assert.equal(
    validateAttachments([{ name: "valid", size: 10 }], attachmentLimits),
    "",
  );
});


test("orders tasks by rank and smart priority", () => {
  const low = task({ taskId: "2", rankKey: "10" });
  const high = task({ taskId: "1", rankKey: "20" });
  assert.equal(compareRank(low, high), -1);
  assert.equal(compareRank(high, low), 1);
  assert.equal(compareRank(task({ taskId: "1" }), task({ taskId: "2" })), -1);

  assert.equal(smartStateOrder({ status: "done", blocked: "false" }), 3);
  assert.equal(smartStateOrder({ status: "todo", blocked: "true" }), 1);
  assert.equal(smartStateOrder({ status: "in_progress", blocked: "false" }), 0);
  assert.equal(smartStateOrder({ status: "todo", blocked: "false" }), 2);

  const dueFollowUp = task({ taskId: "2", followUpDue: "true", followUpDate: "2026-09-20" });
  assert.equal(compareSmart(dueFollowUp, task()), -1);
  assert.equal(compareSmart(
    task({ followUpDue: "true", followUpDate: "2026-09-20", followUpTime: "10:00" }),
    task({ followUpDue: "true", followUpDate: "2026-09-21", followUpTime: "09:00" }),
  ), -1);
  assert.equal(compareSmart(
    task({ followUpDue: "true", followUpDate: "2026-09-20", followUpTime: "09:00" }),
    task({ followUpDue: "true", followUpDate: "2026-09-20", followUpTime: "10:00" }),
  ), -1);
  assert.equal(compareSmart(task({ status: "in_progress" }), task({ status: "todo" })), -2);
  assert.equal(compareSmart(task({ dueDate: "2026-09-20" }), task({ dueDate: "2026-09-21" })), -1);
  assert.equal(compareSmart(task({ taskId: "2" }), task({ taskId: "1" })), -1);
});


test("merges visible order and calculates rank neighbors", () => {
  const first = task({ taskId: "1" });
  const hidden = task({ taskId: "2" });
  hidden.hidden = true;
  const third = task({ taskId: "3" });
  assert.equal(sameTaskOrder([first], [first]), true);
  assert.equal(sameTaskOrder([first], [first, third]), false);
  assert.equal(sameTaskOrder([first, third], [third, first]), false);
  assert.deepEqual(mergeVisibleOrder([first, hidden, third], [third, first]), [third, hidden, first]);
  assert.deepEqual(rankNeighbors([first, hidden, third], first), {
    afterTaskId: null,
    beforeTaskId: 2,
  });
  assert.deepEqual(rankNeighbors([first, hidden, third], hidden), {
    afterTaskId: 1,
    beforeTaskId: 3,
  });
  assert.deepEqual(rankNeighbors([first, hidden, third], third), {
    afterTaskId: 2,
    beforeTaskId: null,
  });
  assert.throws(() => rankNeighbors([first], third), /not in the task order/);
});


test("detects newly due follow-ups and excludes non-actionable tasks", () => {
  const now = { date: "2026-09-20", minute: "10:00" };
  assert.equal(pendingFollowUpBecameDue([
    task({ waitingOn: "Alex", followUpDate: "2026-09-20", followUpTime: "10:00" }),
  ], now), true);
  for (const values of [
    { status: "done", waitingOn: "Alex", followUpDate: "2026-09-20" },
    { waitingOn: "", followUpDate: "2026-09-20" },
    { waitingOn: "Alex", followUpDue: "true", followUpDate: "2026-09-20" },
    { waitingOn: "Alex", followUpDate: "" },
    { waitingOn: "Alex", followUpDate: "2026-09-21" },
  ]) {
    assert.equal(pendingFollowUpBecameDue([task(values)], now), false);
  }
});


test("normalizes persisted display choices", () => {
  assert.equal(normalizeTheme("light"), "light");
  assert.equal(normalizeTheme("dark"), "dark");
  assert.equal(normalizeTheme("system"), "");
  assert.equal(normalizeSortMode("rank"), "rank");
  assert.equal(normalizeSortMode("smart"), "smart");
  assert.equal(normalizeSortMode(null), "smart");
});


test("summarizes multi-select state", () => {
  const labels = { all: "All", empty: "None", singular: "choice", plural: "choices" };
  assert.deepEqual(multiSelectState(0, 0, labels), {
    allSelected: false, indeterminate: false, summary: "None",
  });
  assert.deepEqual(multiSelectState(2, 2, labels), {
    allSelected: true, indeterminate: false, summary: "All",
  });
  assert.deepEqual(multiSelectState(3, 1, labels), {
    allSelected: false, indeterminate: true, summary: "1 choice",
  });
  assert.equal(multiSelectState(3, 2, labels).summary, "2 choices");
});


test("matches tasks against every filter dimension", () => {
  const baseTask = {
    title: "prepare launch",
    status: "todo",
    labelIds: "1 2",
    blocked: false,
    overdue: false,
    stalled: false,
    followUpDue: false,
  };
  const baseFilters = {
    query: "",
    statuses: new Set(["todo"]),
    labels: new Set(["1", "2"]),
    labelsAreFiltered: false,
    overdueOnly: false,
    stalledOnly: false,
    followUpsOnly: false,
    blockedOnly: false,
  };
  const matches = (taskChange = {}, filterChange = {}) => taskMatchesFilters(
    { ...baseTask, ...taskChange },
    { ...baseFilters, ...filterChange },
  );
  assert.equal(matches(), true);
  assert.equal(matches({}, { query: "launch" }), true);
  assert.equal(matches({}, { query: "missing" }), false);
  assert.equal(matches({}, { statuses: new Set(["done"]) }), false);
  assert.equal(matches({ blocked: true }, { statuses: new Set(["blocked"]) }), true);
  assert.equal(matches({}, { labelsAreFiltered: true, labels: new Set(["2"]) }), true);
  assert.equal(matches({}, { labelsAreFiltered: true, labels: new Set(["3"]) }), false);
  assert.equal(matches({ labelIds: "" }, { labelsAreFiltered: true }), false);
  assert.equal(matches({}, { overdueOnly: true }), false);
  assert.equal(matches({ overdue: true }, { overdueOnly: true }), true);
  assert.equal(matches({}, { stalledOnly: true }), false);
  assert.equal(matches({ stalled: true }, { stalledOnly: true }), true);
  assert.equal(matches({}, { followUpsOnly: true }), false);
  assert.equal(matches({ followUpDue: true }, { followUpsOnly: true }), true);
  assert.equal(matches({}, { blockedOnly: true }), false);
  assert.equal(matches({ blocked: true }), true);
});


test("describes navigation locks and configured local time", () => {
  assert.deepEqual(navigationLockState(0, 0, false), {
    locked: false,
    copy: "Finish editing to switch views",
  });
  assert.equal(navigationLockState(1, 0, false).locked, true);
  assert.equal(navigationLockState(0, 1, false).locked, true);
  assert.deepEqual(navigationLockState(0, 0, true), {
    locked: true,
    copy: "Agent is updating tasks",
  });
  assert.deepEqual(
    dateTimeInTimezone("Asia/Calcutta", new Date("2026-09-20T18:35:00Z")),
    { date: "2026-09-21", minute: "00:05" },
  );
});


test("chooses navigation and focused task states", () => {
  assert.equal(appViewNavigation("focus"), "focus");
  assert.equal(appViewNavigation("manage"), "manage");
  assert.equal(appViewNavigation("blocked"), "manage");
  assert.equal(appViewNavigation("agent"), "agent");
  assert.equal(appViewNavigation("unknown"), "focus");

  const first = task({ taskId: "1" });
  const second = task({ taskId: "2" });
  assert.deepEqual(focusViewState([], "missing"), {
    current: null, canChooseAnother: false,
  });
  assert.deepEqual(focusViewState([first], "missing"), {
    current: first, canChooseAnother: false,
  });
  assert.deepEqual(focusViewState([first, second], "2"), {
    current: second, canChooseAnother: true,
  });
  assert.equal(nextTask([first], first), null);
  assert.equal(nextTask([first, second], first), second);
  assert.equal(nextTask([first, second], second), first);
  assert.equal(nextTask([first, second], task()), first);
});


test("calculates pointer and keyboard rank moves without mutating snapshots", () => {
  const dragged = task({ taskId: "1" });
  const first = task({ taskId: "2" });
  const second = task({ taskId: "3" });
  const hidden = task({ taskId: "4" });
  hidden.hidden = true;
  first.getBoundingClientRect = () => ({ top: 100, height: 20 });
  second.getBoundingClientRect = () => ({ top: 200, height: 20 });
  hidden.getBoundingClientRect = () => ({ top: 0, height: 20 });
  dragged.getBoundingClientRect = () => ({ top: 0, height: 20 });
  const rows = [dragged, first, hidden, second];
  assert.equal(dragInsertionRow(rows, 50, dragged), first);
  assert.equal(dragInsertionRow(rows, 150, dragged), second);
  assert.equal(dragInsertionRow(rows, 250, dragged), null);

  assert.equal(keyboardRankMove(rows, dragged, "up"), null);
  assert.deepEqual(keyboardRankMove(rows, dragged, "down"), [first, dragged, hidden, second]);
  assert.deepEqual(keyboardRankMove(rows, second, "up"), [dragged, second, hidden, first]);
  assert.equal(keyboardRankMove(rows, second, "down"), null);
  assert.deepEqual(rows, [dragged, first, hidden, second]);
});
