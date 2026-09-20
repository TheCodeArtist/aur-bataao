export function formatFileSize(byteSize) {
  if (byteSize < 1024) return `${byteSize} B`;
  if (byteSize < 1024 * 1024) return `${(byteSize / 1024).toFixed(1)} KB`;
  return `${(byteSize / (1024 * 1024)).toFixed(1)} MB`;
}

export function uploadFilename(file, index, now = Date.now()) {
  if (file.name) return file.name;
  const extension = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
  }[file.type] || "";
  return `clipboard-${now}-${index + 1}${extension}`;
}

export function validateAttachments(files, {
  existingCount = 0,
  pendingFiles = [],
  maxAttachmentBytes,
  maxAttachmentsPerTask,
  maxUploadBytes,
}) {
  const emptyFile = files.find((file) => file.size === 0);
  if (emptyFile) return `${emptyFile.name || "An attachment"} is empty`;
  const oversizedFile = files.find((file) => file.size > maxAttachmentBytes);
  if (oversizedFile) {
    return `${oversizedFile.name || "An attachment"} is larger than ${formatFileSize(maxAttachmentBytes)}`;
  }
  if (existingCount + pendingFiles.length + files.length > maxAttachmentsPerTask) {
    return `A task can have at most ${maxAttachmentsPerTask} attachments`;
  }
  const uploadSize = [...pendingFiles, ...files]
    .reduce((total, file) => total + file.size, 0);
  if (uploadSize >= maxUploadBytes) {
    return `Attach fewer files at once (combined limit ${formatFileSize(maxUploadBytes)})`;
  }
  return "";
}

function data(task) {
  return task.dataset || task;
}

export function smartStateOrder(task) {
  const values = data(task);
  if (values.status === "done") return 3;
  if (values.blocked === "true") return 1;
  return values.status === "in_progress" ? 0 : 2;
}

export function compareRank(first, second) {
  const firstData = data(first);
  const secondData = data(second);
  const firstRank = BigInt(firstData.rankKey);
  const secondRank = BigInt(secondData.rankKey);
  if (firstRank < secondRank) return -1;
  if (firstRank > secondRank) return 1;
  return Number(firstData.taskId) - Number(secondData.taskId);
}

export function compareSmart(first, second) {
  const firstData = data(first);
  const secondData = data(second);
  const followUpDifference = Number(secondData.followUpDue === "true")
    - Number(firstData.followUpDue === "true");
  if (followUpDifference) return followUpDifference;
  if (firstData.followUpDue === "true" && secondData.followUpDue === "true") {
    const dateDifference = firstData.followUpDate.localeCompare(secondData.followUpDate);
    if (dateDifference) return dateDifference;
    const timeDifference = firstData.followUpTime.localeCompare(secondData.followUpTime);
    if (timeDifference) return timeDifference;
  }
  const statusDifference = smartStateOrder(firstData) - smartStateOrder(secondData);
  if (statusDifference) return statusDifference;
  const dueDateDifference = (firstData.dueDate || "9999-12-31")
    .localeCompare(secondData.dueDate || "9999-12-31");
  if (dueDateDifference) return dueDateDifference;
  return Number(secondData.taskId) - Number(firstData.taskId);
}

export function sameTaskOrder(first, second) {
  return first.length === second.length
    && first.every((task, index) => task === second[index]);
}

export function mergeVisibleOrder(snapshot, visibleOrder) {
  let visibleIndex = 0;
  return snapshot.map((task) => (task.hidden ? task : visibleOrder[visibleIndex++]));
}

export function rankNeighbors(orderedTasks, movedTask) {
  const position = orderedTasks.indexOf(movedTask);
  if (position < 0) throw new Error("Moved task is not in the task order");
  return {
    afterTaskId: position > 0 ? Number(data(orderedTasks[position - 1]).taskId) : null,
    beforeTaskId: position < orderedTasks.length - 1
      ? Number(data(orderedTasks[position + 1]).taskId)
      : null,
  };
}

export function pendingFollowUpBecameDue(tasks, localDateTime) {
  const currentMinute = `${localDateTime.date}T${localDateTime.minute}`;
  return tasks.some((task) => {
    const values = data(task);
    return values.status !== "done"
      && Boolean(values.waitingOn)
      && values.followUpDue !== "true"
      && Boolean(values.followUpDate)
      && `${values.followUpDate}T${values.followUpTime}` <= currentMinute;
  });
}

export function normalizeTheme(value) {
  return value === "light" || value === "dark" ? value : "";
}

export function normalizeSortMode(value) {
  return value === "rank" ? "rank" : "smart";
}

export function multiSelectState(optionCount, selectedCount, labels) {
  const allSelected = optionCount > 0 && selectedCount === optionCount;
  let summary = labels.all;
  if (!allSelected) {
    if (selectedCount === 0) summary = labels.empty;
    else summary = `${selectedCount} ${selectedCount === 1 ? labels.singular : labels.plural}`;
  }
  return {
    allSelected,
    indeterminate: selectedCount > 0 && !allSelected,
    summary,
  };
}

export function taskMatchesFilters(task, {
  query,
  statuses,
  labels,
  labelsAreFiltered,
  overdueOnly,
  stalledOnly,
  followUpsOnly,
  blockedOnly,
}) {
  const taskLabels = task.labelIds.split(" ").filter(Boolean);
  return !(
    (query && !task.title.includes(query))
    || !(statuses.has(task.status) || (statuses.has("blocked") && task.blocked))
    || (labelsAreFiltered && !taskLabels.some((label) => labels.has(label)))
    || (overdueOnly && !task.overdue)
    || (stalledOnly && !task.stalled)
    || (followUpsOnly && !task.followUpDue)
    || (blockedOnly && !task.blocked)
  );
}

export function navigationLockState(dirtyCount, pendingWrites, agentMutationPending) {
  return {
    locked: dirtyCount > 0 || pendingWrites > 0 || agentMutationPending,
    copy: agentMutationPending ? "Agent is updating tasks" : "Finish editing to switch views",
  };
}

export function dateTimeInTimezone(timeZone, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return {
    date: `${values.year}-${values.month}-${values.day}`,
    minute: `${values.hour}:${values.minute}`,
  };
}

export function appViewNavigation(view) {
  const taskView = view === "manage" || view === "blocked" ? "manage" : "focus";
  return view === "agent" ? "agent" : taskView;
}

export function focusViewState(candidates, preferredTaskId = "") {
  const current = candidates.find((task) => task.dataset.taskId === preferredTaskId)
    || candidates[0]
    || null;
  return {
    current,
    canChooseAnother: candidates.length >= 2,
  };
}

export function nextTask(candidates, current) {
  if (candidates.length < 2) return null;
  const currentIndex = candidates.indexOf(current);
  return candidates[(currentIndex + 1) % candidates.length];
}

export function dragInsertionRow(rows, pointerY, draggedRow) {
  return rows
    .filter((row) => row !== draggedRow && !row.hidden)
    .reduce((closest, row) => {
      const box = row.getBoundingClientRect();
      const offset = pointerY - box.top - box.height / 2;
      return offset < 0 && offset > closest.offset ? { offset, row } : closest;
    }, { offset: Number.NEGATIVE_INFINITY, row: null }).row;
}

export function keyboardRankMove(snapshot, row, direction) {
  const visibleOrder = snapshot.filter((candidate) => !candidate.hidden);
  const currentIndex = visibleOrder.indexOf(row);
  const targetIndex = direction === "up" ? currentIndex - 1 : currentIndex + 1;
  if (targetIndex < 0 || targetIndex >= visibleOrder.length) return null;
  visibleOrder.splice(currentIndex, 1);
  visibleOrder.splice(targetIndex, 0, row);
  return mergeVisibleOrder(snapshot, visibleOrder);
}
