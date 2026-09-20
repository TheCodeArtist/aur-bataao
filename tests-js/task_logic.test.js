import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compareRank,
  compareSmart,
  formatFileSize,
  mergeVisibleOrder,
  pendingFollowUpBecameDue,
  rankNeighbors,
  sameTaskOrder,
  smartStateOrder,
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
