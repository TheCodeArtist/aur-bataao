import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assertBrowserCoverage,
  formatBrowserCoverage,
  summarizeBrowserCoverage,
} from "../tests-e2e/browser_coverage.js";


function entry(url, source, functions) {
  return { url, source, functions };
}


function completeSummary(pathname) {
  return {
    pathname,
    functions: { covered: 1, total: 1 },
    uncoveredFunctions: [],
    blocks: { covered: 1, total: 1 },
    accountable: { covered: 1, total: 1 },
    uncovered: [],
    excluded: [],
    unusedExclusions: [],
  };
}


test("browser coverage summaries merge repeated controller samples", () => {
  const source = "first\nsecond\nthird";
  const first = entry("http://127.0.0.1/static/app.js", "", [{
    functionName: "",
    ranges: [
      { startOffset: 0, endOffset: 18, count: 0 },
      { startOffset: 6, endOffset: 12, count: 0 },
    ],
  }]);
  const second = entry("http://127.0.0.1/static/app.js", source, [{
    functionName: "",
    ranges: [
      { startOffset: 0, endOffset: 18, count: 2 },
      { startOffset: 6, endOffset: 12, count: 0 },
    ],
  }]);
  const agent = entry("http://127.0.0.1/static/agent.js", source, [
    {
      functionName: "initialize",
      ranges: [{ startOffset: 6, endOffset: 18, count: 1 }],
    },
    {
      functionName: "neverRuns",
      ranges: [{ startOffset: 13, endOffset: 18, count: 0 }],
    },
  ]);
  const ignored = entry("http://127.0.0.1/static/markdown.js", source, []);

  const summaries = summarizeBrowserCoverage([first, second, agent, ignored], []);
  assert.deepEqual(summaries, [
    {
      pathname: "/static/agent.js",
      functions: { covered: 1, total: 2 },
      uncoveredFunctions: [{ name: "neverRuns", line: 3 }],
      blocks: { covered: 1, total: 2 },
      accountable: { covered: 1, total: 2 },
      uncovered: [{ name: "neverRuns", line: 3, endLine: 3, excerpt: "third" }],
      excluded: [],
      unusedExclusions: [],
    },
    {
      pathname: "/static/app.js",
      functions: { covered: 1, total: 1 },
      uncoveredFunctions: [],
      blocks: { covered: 2, total: 3 },
      accountable: { covered: 2, total: 3 },
      uncovered: [{ name: "<anonymous>", line: 2, endLine: 2, excerpt: "second" }],
      excluded: [],
      unusedExclusions: [],
    },
  ]);
  assert.doesNotMatch(formatBrowserCoverage(summaries), /more uncovered ranges/);
});


test("browser coverage unions differently shaped V8 ranges by source interval", () => {
  const source = "first\nsecond\nthird";
  const first = entry("http://127.0.0.1/static/app.js", source, [{
    functionName: "handler",
    ranges: [
      { startOffset: 0, endOffset: 18, count: 1 },
      { startOffset: 6, endOffset: 12, count: 0 },
    ],
  }]);
  const second = entry("http://127.0.0.1/static/app.js", source, [{
    functionName: "handler",
    ranges: [
      { startOffset: 0, endOffset: 18, count: 2 },
      { startOffset: 12, endOffset: 18, count: 0 },
    ],
  }]);

  const [summary] = summarizeBrowserCoverage([first, second], []);
  assert.deepEqual(summary.blocks, { covered: 3, total: 3 });
  assert.deepEqual(summary.accountable, { covered: 3, total: 3 });
  assert.deepEqual(summary.uncovered, []);
});


test("browser coverage applies exact justified exclusions and gates accountable ranges", () => {
  const source = "zero\nskip\ncovered";
  const sample = entry("http://127.0.0.1/static/app.js", source, [{
    functionName: "handler",
    ranges: [
      { startOffset: 0, endOffset: source.length, count: 1 },
      { startOffset: 5, endOffset: 9, count: 0 },
    ],
  }]);
  const exclusions = [
    { pathname: "/static/agent.js", name: "handler", excerpt: "skip", reason: "wrong path" },
    { pathname: "/static/app.js", name: "other", excerpt: "skip", reason: "wrong name" },
    { pathname: "/static/app.js", name: "handler", line: 3, excerpt: "skip", reason: "wrong line" },
    { pathname: "/static/app.js", name: "handler", excerpt: "absent", reason: "wrong source" },
    { pathname: "/static/app.js", name: "handler", line: 2, excerpt: "skip", reason: "schema invariant" },
  ];

  const [summary] = summarizeBrowserCoverage([sample], exclusions);
  assert.deepEqual(summary.blocks, { covered: 2, total: 3 });
  assert.deepEqual(summary.accountable, { covered: 2, total: 2 });
  assert.deepEqual(summary.uncovered, []);
  assert.equal(summary.unusedExclusions.length, 3);
  assert.equal(summary.excluded[0].reason, "schema invariant");
  assert.throws(
    () => assertBrowserCoverage([summary, completeSummary("/static/agent.js")]),
    /stale exclusion/,
  );

  const [exactSummary] = summarizeBrowserCoverage([sample], [exclusions.at(-1)]);
  assert.deepEqual(exactSummary.unusedExclusions, []);
  assert.doesNotThrow(() => assertBrowserCoverage([
    exactSummary,
    completeSummary("/static/agent.js"),
  ]));
});


test("browser coverage gate rejects uncalled functions and accountable gaps", () => {
  const incomplete = {
    pathname: "/static/app.js",
    functions: { covered: 0, total: 1 },
    uncoveredFunctions: [{ name: "handler", line: 1 }],
    blocks: { covered: 0, total: 1 },
    accountable: { covered: 0, total: 1 },
    uncovered: [{ name: "handler", line: 1, endLine: 1, excerpt: "handler" }],
    excluded: [],
    unusedExclusions: [],
  };
  assert.throws(
    () => assertBrowserCoverage([incomplete, completeSummary("/static/agent.js")]),
    /Browser controller coverage gate failed/,
  );
});


test("browser coverage gate rejects missing controller data", () => {
  assert.throws(
    () => assertBrowserCoverage([completeSummary("/static/app.js")]),
    /Missing browser controller coverage: \/static\/agent\.js/,
  );
});


test("browser coverage formatting is concise, deduplicated, and percentage based", () => {
  const uncovered = Array.from({ length: 14 }, (_, index) => ({
    name: index < 2 ? "same" : `function-${index}`,
    line: index < 2 ? 5 : index + 5,
    endLine: index < 2 ? 5 : index + 5,
    excerpt: "branch",
  }));
  const formatted = formatBrowserCoverage([
    {
      pathname: "/static/app.js",
      functions: { covered: 1, total: 2 },
      uncoveredFunctions: Array.from({ length: 14 }, (_, index) => ({
        name: `uncalled-${index}`,
        line: index + 1,
      })),
      blocks: { covered: 0, total: 0 },
      accountable: { covered: 0, total: 0 },
      uncovered,
      excluded: [],
      unusedExclusions: [],
    },
  ]);
  assert.match(formatted, /diagnostic/);
  assert.match(formatted, /functions 50\.0% \(1\/2\)/);
  assert.match(formatted, /raw V8 ranges 100\.0% \(0\/0\)/);
  assert.match(formatted, /accountable 100\.0% \(0\/0; 0 justified exclusions\)/);
  assert.equal(formatted.match(/app\.js:5 \(same\)/g)?.length, 1);
  assert.match(formatted, /uncalled app\.js:1 \(uncalled-0\)/);
  assert.match(formatted, /uncalled app\.js:14 \(uncalled-13\)/);
  assert.match(formatted, /… 5 more uncovered block ranges/);
  assert.equal(formatBrowserCoverage([]), "Browser controller V8 execution map (diagnostic):");
});


test("browser coverage formatting can show every uncovered range for diagnosis", () => {
  const previous = process.env.BROWSER_COVERAGE_VERBOSE;
  process.env.BROWSER_COVERAGE_VERBOSE = "1";
  try {
    const uncovered = Array.from({ length: 10 }, (_, index) => ({
      name: `function-${index}`,
      line: index + 1,
      endLine: index === 9 ? 12 : index + 1,
      excerpt: `branch ${index}`,
    }));
    const formatted = formatBrowserCoverage([{
      pathname: "/static/app.js",
      functions: { covered: 1, total: 1 },
      uncoveredFunctions: [],
      blocks: { covered: 1, total: 11 },
      accountable: { covered: 1, total: 11 },
      uncovered,
      excluded: [{
        name: "defensive",
        line: 20,
        endLine: 20,
        excerpt: "fallback",
        reason: "defensive invariant",
      }],
      unusedExclusions: [],
    }]);
    assert.match(formatted, /app\.js:10-12 \(function-9\): branch 9/);
    assert.match(formatted, /excluded app\.js:20 \(defensive\): defensive invariant/);
    assert.doesNotMatch(formatted, /more uncovered block ranges/);
  } finally {
    if (previous === undefined) delete process.env.BROWSER_COVERAGE_VERBOSE;
    else process.env.BROWSER_COVERAGE_VERBOSE = previous;
  }
});
