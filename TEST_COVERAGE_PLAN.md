# Accountable 100% Coverage Plan

Status: complete and approved.

## Goal

Reach 100% statement and branch coverage for first-party Python and reusable
JavaScript while preserving meaningful browser coverage. Every remaining gap
must be handled in exactly one of three ways:

1. Exercise observable behavior with a focused test.
2. Simplify or remove redundant/dead implementation.
3. Apply a narrow, documented exclusion only when the branch is genuinely
   platform-specific or unreachable in the measuring process.

Coverage must never be raised with assertions that merely execute lines. Tests
must verify postconditions such as committed or rolled-back state, cleanup,
error contracts, secret redaction, restored UI controls, and stale-work
suppression.

## Baseline (before this work)

- Python: 2,573 statements, 73 missed; 722 branches, 47 partial; 96.18% total.
- Reusable JavaScript: 100% lines/functions and 97.82% branches.
- Browser: eight Playwright journeys pass, but the DOM-heavy entry controllers
  are behavior-tested rather than included in the numerical unit gate.

## Current progress

- Python: 250 tests pass; 2,573 statements and 722 branches are at 100.00%.
- Reusable JavaScript and the coverage reporter: 39 tests pass at 100% lines,
  branches, and functions.
- Browser: 39 end-to-end journeys pass with automatic uncaught-error checks,
  100% controller function execution, and 100% accountable V8 range coverage.
- Raw V8 ranges are 352/363 (97.0%) for `agent.js` and 558/567 (98.4%) for
  `app.js`. The 20 remaining exact ranges are documented schema/DOM invariants
  or browser-navigation teardown artifacts with behavior assertions. Broad
  exclusions are unsupported, and stale exclusions fail the gate.

## Work sequence

### 1. Core Python invariants

Status: complete.

- Cover archived-session, invalid approval/resume, non-object tool argument,
  profile validation, unknown database-integrity, and malformed usage paths.
- Exercise runner interruption/failure-state behavior and verify durable state.
- Prefer public APIs; use direct lower-level calls only for invariants that
  cannot be reached through valid public workflows.

Evidence: focused tests pass and the five core modules report 100% statement
and branch coverage.

### 2. Flask configuration, migrations, and API contracts

Status: complete.

- Cover configuration defaults/errors, existing-profile startup, schema
  additions, legacy dependency migration, corrupt audit-event recovery,
  reconciliation throttling, 404s, invalid views, ambiguous ranks, upload
  cleanup, agent failures, and nullable folder moves.
- Cover mutation rollback behavior. Where rollback branches are mechanically
  duplicated, centralize transaction handling instead of duplicating mocks.
- Assert database/file postconditions, not only response codes.

Evidence: `app.py` reaches 100% statement/branch coverage and integration tests
prove rollback and cleanup.

### 3. Launcher and platform behavior

Status: complete locally; Ubuntu CI remains the real POSIX verification.

- Cover stop/start races, worker startup failure, stop-file detection, signal
  handling, browser-disable mode, and cleanup ordering.
- Exercise POSIX behavior in Ubuntu CI and Windows behavior in Windows CI.
- Use narrowly justified exclusions only for process entry points or a branch
  that cannot execute on the measuring platform and is exercised elsewhere.

Evidence: `start.py` has 100% accountable coverage locally and both platform
test jobs pass.

### 4. Reusable JavaScript

Status: complete at 100/100/100.

- Add edge cases for serialization failures, poll lifecycle, Markdown parsing,
  attachment boundaries/fallbacks, and ordering equality/short circuits.
- Raise Node statement, branch, and function thresholds to 100 only after the
  report is clean.

Evidence: Node's built-in coverage report is 100/100/100.

### 5. DOM controllers and browser behavior

Status: complete. High-value endpoint/model, approval/tool-call, filter,
navigation-lock, focus, preference, timezone, and ranking decisions are under
the 100% unit gate. Browser journeys cover real DOM behavior, failure recovery,
rollback, stale async work, navigation, accessibility state, attachments,
conversation organization, and approval flows.

- Extract cohesive controller decisions only where that reduces coupling:
  editing/dirty state, attachments, ranking, agent sessions/approvals, and
  persisted preferences.
- Test extracted decisions with jsdom and dependency injection.
- Keep Playwright journeys for real DOM, navigation, download, persistence,
  and browser-error behavior.
- Add Chromium coverage collection for the remaining thin browser entrypoints
  if needed to make the total JavaScript accounting numerical rather than
  scenario-only.

Evidence: all substantive controller branches are under the 100% unit gate;
all controller functions execute in Playwright, and every uncovered V8 range
must match one exact documented exception. Unused exceptions fail the build.

### 6. Final gates and quality audit

Status: complete. The authoritative local runs are 250 Python tests at 100.00%
statement/branch coverage, 39 JavaScript tests at 100/100/100, and 39 passing
Chromium journeys with 100% accountable controller coverage and no uncaught
page errors.

- Set Python `fail_under = 100`.
- Set Node lines/branches/functions thresholds to 100.
- Run the full Python, Node, and Playwright suites from a clean worktree.
- Review every exclusion for a narrow rationale.
- Confirm CI and pre-push run the same authoritative gates.
- Consider mutation testing as a periodic diagnostic after coverage is 100%;
  execution coverage alone does not prove assertion quality.

## Change discipline

- Work in small batches and inspect coverage after each batch.
- Commit completed work in small, reviewable batches after the gates pass.
- Preserve existing user changes and avoid broad refactors motivated only by
  the metric.
- Ask before unexpected throwaway work such as temporary generated fixtures,
  experimental dependency installation, or disposable large refactors.
