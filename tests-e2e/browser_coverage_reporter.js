import {
  assertBrowserCoverage,
  formatBrowserCoverage,
  summarizeBrowserCoverage,
} from "./browser_coverage.js";


export const BROWSER_COVERAGE_ATTACHMENT = "browser-controller-coverage";


export default class BrowserCoverageReporter {
  constructor({
    gate = false,
    summarize = summarizeBrowserCoverage,
    format = formatBrowserCoverage,
    assert = assertBrowserCoverage,
  } = {}) {
    this.gate = gate;
    this.summarize = summarize;
    this.format = format;
    this.assert = assert;
    this.entries = [];
    this.collectionErrors = [];
  }

  onTestEnd(_test, result) {
    result.attachments
      .filter((attachment) => attachment.name === BROWSER_COVERAGE_ATTACHMENT)
      .forEach((attachment) => {
        try {
          const entries = JSON.parse(attachment.body?.toString("utf8") || "");
          if (!Array.isArray(entries)) throw new Error("Coverage attachment must contain an array.");
          this.entries.push(...entries);
        } catch (error) {
          this.collectionErrors.push(error);
        }
      });
  }

  onEnd(result) {
    const summaries = this.summarize(this.entries);
    console.log(this.format(summaries));

    // Preserve the original test failure. Coverage is meaningful only after the
    // complete suite has run, potentially across multiple worker processes.
    if (result.status !== "passed") return undefined;
    if (this.collectionErrors.length) {
      console.error(`Browser coverage collection failed: ${this.collectionErrors[0].message}`);
      return { status: "failed" };
    }
    if (!this.gate) return undefined;

    try {
      this.assert(summaries);
      return undefined;
    } catch (error) {
      console.error(error.stack || error.message);
      return { status: "failed" };
    }
  }
}
