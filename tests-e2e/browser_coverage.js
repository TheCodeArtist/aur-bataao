const CONTROLLER_PATHS = new Set(["/static/app.js", "/static/agent.js"]);

// These are deliberately narrow: exact function/range matches with a reviewable reason.
// User-visible alternatives stay accountable even when they are awkward to drive in a browser.
export const BROWSER_COVERAGE_EXCLUSIONS = [
  {
    pathname: "/static/agent.js", name: "showError", excerpt: ": String(error)",
    reason: "All callers pass Error instances from requestJson or catch clauses; string input is defensive normalization.",
  },
  {
    pathname: "/static/agent.js", name: "setActiveModelOption", excerpt: "return;",
    reason: "Every caller first verifies that rendered model options exist.",
  },
  {
    pathname: "/static/agent.js", name: "renderModelOptions", excerpt: "return closeModelOptions();",
    reason: "Every event caller checks the model cache and discovery populates it before rendering.",
  },
  {
    pathname: "/static/agent.js", name: "<anonymous>", line: 320, excerpt: "return;",
    reason: "Stale discovery success is asserted by the stale-model-discovery browser test; Chromium coalesces the async callback range.",
  },
  {
    pathname: "/static/agent.js", name: "createTimestamp", excerpt: "|| \"\"",
    reason: "Session timestamps are schema-required and messages without timestamps skip createTimestamp.",
  },
  {
    pathname: "/static/agent.js", name: "<anonymous>", line: 847, excerpt: "return;",
    reason: "Empty and busy submissions are asserted through requestSubmit/double-action browser tests; Chromium coalesces the async handler guard.",
  },
  {
    pathname: "/static/agent.js", name: "decideApproval", excerpt: "return;",
    reason: "The approval failure test double-clicks while busy and verifies only one request is processed.",
  },
  {
    pathname: "/static/agent.js", name: "decideApproval", excerpt: "catch (error)",
    reason: "The approval failure test asserts that a rejected request is surfaced and the UI recovers.",
  },
  {
    pathname: "/static/agent.js", name: "resumeRun", excerpt: "return;",
    reason: "The resume failure test double-clicks while busy and verifies guarded execution.",
  },
  {
    pathname: "/static/agent.js", name: "resumeRun", excerpt: "catch (error)",
    reason: "The resume failure test asserts that a rejected resume is surfaced and the UI recovers.",
  },
  {
    pathname: "/static/agent.js", name: "render", excerpt: "?.status",
    reason: "Polling only starts after a run exists; the optional access is defensive against malformed session payloads.",
  },
  {
    pathname: "/static/app.js", name: "taskControlValue", excerpt: "return String(control.checked);",
    reason: "All current dirty-tracked controls are text/select; task checkboxes are deliberately excluded by taskEditControl.",
  },
  {
    pathname: "/static/app.js", name: "updateTaskControlDirtyState", excerpt: "taskControlBaselines.set",
    reason: "Real input/change events follow focusin, which records the baseline first; this guards synthetic events.",
  },
  {
    pathname: "/static/app.js", name: "syncTaskLabelPicker", excerpt: "return;",
    reason: "Every server-rendered task row contains its label picker; the check protects future partial-row rendering.",
  },
  {
    pathname: "/static/app.js", name: "applyTask", excerpt: "|| []",
    reason: "The task API always serializes blocked_by as an array; the fallback protects malformed payloads.",
  },
  {
    pathname: "/static/app.js", name: "updateActiveTaskCount", excerpt: "return;",
    reason: "The application template always renders the active-task-count element.",
  },
  {
    pathname: "/static/app.js", name: "<anonymous>", line: 1005, excerpt: "location.assign",
    reason: "The stale-task navigation test asserts the reload; document teardown prevents Chromium from returning the final range count.",
  },
  {
    pathname: "/static/app.js", name: "<anonymous>", line: 1022, excerpt: "location.reload",
    reason: "History reload behavior is covered; document teardown prevents Chromium from returning the final range count.",
  },
  {
    pathname: "/static/app.js", name: "<anonymous>", line: 1236, excerpt: "location.assign",
    reason: "Reconciliation navigation is integration-tested; document teardown prevents Chromium from returning the final range count.",
  },
  {
    pathname: "/static/app.js", name: "<anonymous>", line: 433, excerpt: "?? \"\"",
    reason: "Editable task fields are normalized to strings by the task API; the null fallback protects malformed payloads.",
  },
];


function sourceLine(source, offset) {
  return source.slice(0, offset).split("\n").length;
}


function sourceExcerpt(source, startOffset, endOffset) {
  return source.slice(startOffset, endOffset).replace(/\s+/g, " ").trim().slice(0, 160);
}


function percentage(covered, total) {
  return total === 0 ? "100.0" : ((covered / total) * 100).toFixed(1);
}


function effectiveCount(ranges, startOffset, endOffset) {
  let matchingRange = null;
  for (const range of ranges) {
    const containsSegment = range.startOffset <= startOffset && range.endOffset >= endOffset;
    const isMoreSpecific = !matchingRange
      || range.endOffset - range.startOffset < matchingRange.endOffset - matchingRange.startOffset;
    if (containsSegment && isMoreSpecific) matchingRange = range;
  }
  return matchingRange?.count || 0;
}


function functionSegments(functionCoverage) {
  const boundaries = new Set();
  functionCoverage.samples.forEach((ranges) => {
    ranges.forEach((range) => {
      boundaries.add(range.startOffset);
      boundaries.add(range.endOffset);
    });
  });
  const offsets = [...boundaries].sort((left, right) => left - right);
  return offsets.slice(0, -1).map((startOffset, index) => {
    const endOffset = offsets[index + 1];
    return {
      startOffset,
      endOffset,
      count: functionCoverage.samples.some(
        (ranges) => effectiveCount(ranges, startOffset, endOffset) > 0,
      ) ? 1 : 0,
    };
  });
}


function matchingExclusion(exclusions, pathname, item) {
  return exclusions.find((exclusion) => (
    exclusion.pathname === pathname
    && exclusion.name === item.name
    && (exclusion.line === undefined || exclusion.line === item.line)
    && item.excerpt.includes(exclusion.excerpt)
  ));
}


export function summarizeBrowserCoverage(entries, exclusions = BROWSER_COVERAGE_EXCLUSIONS) {
  const usedExclusions = new Set();
  const scripts = new Map();
  for (const entry of entries) {
    const pathname = new URL(entry.url).pathname;
    if (!CONTROLLER_PATHS.has(pathname)) continue;
    let script = scripts.get(pathname);
    if (!script) {
      script = {
        source: entry.source || "",
        functions: new Map(),
      };
      scripts.set(pathname, script);
    }
    if (!script.source && entry.source) script.source = entry.source;
    for (const functionCoverage of entry.functions) {
      const root = functionCoverage.ranges[0];
      const functionKey = [
        functionCoverage.functionName,
        root.startOffset,
        root.endOffset,
      ].join(":");
      let mergedFunction = script.functions.get(functionKey);
      if (!mergedFunction) {
        mergedFunction = {
          name: functionCoverage.functionName || "<anonymous>",
          startOffset: root.startOffset,
          count: 0,
          samples: [],
        };
        script.functions.set(functionKey, mergedFunction);
      }
      mergedFunction.count = Math.max(mergedFunction.count, root.count);
      mergedFunction.samples.push(functionCoverage.ranges);
    }
  }

  const summaries = [];
  for (const [pathname, script] of [...scripts].sort()) {
    const functions = [...script.functions.values()];
    const blocks = functions.flatMap((item) => functionSegments(item).map((range) => ({
      ...range,
      name: item.name,
    })));
    const uncovered = blocks
      .filter((block) => block.count === 0)
      .map((block) => ({
        name: block.name,
        line: sourceLine(script.source, block.startOffset),
        endLine: sourceLine(script.source, block.endOffset),
        excerpt: sourceExcerpt(script.source, block.startOffset, block.endOffset),
      }));
    const accountableUncovered = [];
    const excluded = [];
    uncovered.forEach((item) => {
      const exclusion = matchingExclusion(exclusions, pathname, item);
      if (exclusion) {
        usedExclusions.add(exclusion);
        excluded.push({ ...item, reason: exclusion.reason });
      }
      else accountableUncovered.push(item);
    });
    summaries.push({
      pathname,
      functions: {
        covered: functions.filter((item) => item.count > 0).length,
        total: functions.length,
      },
      uncoveredFunctions: functions
        .filter((item) => item.count === 0)
        .map((item) => ({
          name: item.name,
          line: sourceLine(script.source, item.startOffset),
        })),
      blocks: {
        covered: blocks.filter((item) => item.count > 0).length,
        total: blocks.length,
      },
      accountable: {
        covered: blocks.filter((item) => item.count > 0).length,
        total: blocks.length - excluded.length,
      },
      uncovered: accountableUncovered,
      excluded,
      unusedExclusions: exclusions.filter(
        (exclusion) => exclusion.pathname === pathname && !usedExclusions.has(exclusion),
      ),
    });
  }
  return summaries;
}


export function formatBrowserCoverage(summaries) {
  const lines = ["Browser controller V8 execution map (diagnostic):"];
  const verbose = process.env.BROWSER_COVERAGE_VERBOSE === "1";
  for (const summary of summaries) {
    const label = summary.pathname.split("/").pop();
    lines.push(
      `  ${label}: functions ${percentage(summary.functions.covered, summary.functions.total)}% `
        + `(${summary.functions.covered}/${summary.functions.total}), `
        + `raw V8 ranges ${percentage(summary.blocks.covered, summary.blocks.total)}% `
        + `(${summary.blocks.covered}/${summary.blocks.total}), `
        + `accountable ${percentage(summary.accountable.covered, summary.accountable.total)}% `
        + `(${summary.accountable.covered}/${summary.accountable.total}; ${summary.excluded.length} justified exclusions)`,
    );
    for (const item of summary.uncoveredFunctions) {
      lines.push(`    uncalled ${label}:${item.line} (${item.name})`);
    }
    const uniqueUncovered = summary.uncovered.filter((item, index, items) => (
      items.findIndex((candidate) => (
        candidate.line === item.line
        && candidate.endLine === item.endLine
        && candidate.name === item.name
        && candidate.excerpt === item.excerpt
      )) === index
    ));
    const detailLimit = verbose ? uniqueUncovered.length : 8;
    for (const item of uniqueUncovered.slice(0, detailLimit)) {
      const location = item.endLine > item.line ? `${item.line}-${item.endLine}` : String(item.line);
      const excerpt = verbose && item.excerpt ? `: ${item.excerpt}` : "";
      lines.push(`    uncovered block ${label}:${location} (${item.name})${excerpt}`);
    }
    if (uniqueUncovered.length > detailLimit) {
      lines.push(`    … ${uniqueUncovered.length - detailLimit} more uncovered block ranges`);
    }
    if (verbose) {
      summary.excluded.forEach((item) => {
        lines.push(`    excluded ${label}:${item.line} (${item.name}): ${item.reason}`);
      });
    }
    summary.unusedExclusions.forEach((item) => {
      const location = item.line === undefined ? "" : `:${item.line}`;
      lines.push(`    stale exclusion ${label}${location} (${item.name}): ${item.excerpt}`);
    });
  }
  return lines.join("\n");
}


export function assertBrowserCoverage(summaries) {
  const failures = summaries.filter((summary) => (
    summary.functions.covered !== summary.functions.total
    || summary.accountable.covered !== summary.accountable.total
    || summary.unusedExclusions.length > 0
  ));
  if (failures.length) {
    throw new Error(`Browser controller coverage gate failed.\n${formatBrowserCoverage(failures)}`);
  }
}
