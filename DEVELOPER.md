# Aur Bataao developer guide

This guide is the technical reference for configuring, running, developing,
testing, debugging, and integrating Aur Bataao. The user-oriented introduction
and basic installation steps are in [README.md](README.md).

## Contents

- [Technology and requirements](#technology-and-requirements)
- [Development setup](#development-setup)
- [Running and stopping the app](#running-and-stopping-the-app)
- [Configuration](#configuration)
- [LLM endpoint configuration](#llm-endpoint-configuration)
- [Behavioral model](#behavioral-model)
- [Architecture](#architecture)
- [Persistence and migrations](#persistence-and-migrations)
- [HTTP interface](#http-interface)
- [Testing and coverage](#testing-and-coverage)
- [Debugging and troubleshooting](#debugging-and-troubleshooting)
- [Git hooks and CI](#git-hooks-and-ci)

## Technology and requirements

The application is intentionally small and server rendered:

- Python 3.11 or newer
- Flask 3.1, Waitress 3, the OpenAI Python client, and `tzdata`
- SQLite, accessed through Python's standard library
- Jinja templates and vanilla browser JavaScript/CSS
- Node.js 20.19 or newer, npm, and Chromium only for JavaScript and browser
  testing

There is no frontend build step and no external database service.

## Development setup

Clone the repository, change into its root, and create a virtual environment.

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
npm ci
npx playwright install chromium
git config --local core.hooksPath .githooks
```

### Linux or macOS

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[test]"
npm ci
npx playwright install chromium
git config --local core.hooksPath .githooks
```

The editable install exposes changes to the top-level Python modules without a
reinstall. Run `npm ci` again whenever `package-lock.json` changes. The bundled
Git hooks are opt-in because Git does not enable repository-provided hooks when
cloning.

## Running and stopping the app

The supported launcher starts a supervised Waitress worker:

```powershell
.\.venv\Scripts\python.exe start.py
```

```bash
./.venv/bin/python start.py
```

It binds to `127.0.0.1:8080` by default, opens a browser, prints logs to the
terminal, and remains in the foreground. Stop it with `Ctrl+C`, a normal
termination signal, or from another terminal:

```powershell
.\.venv\Scripts\python.exe start.py --stop
```

The launcher uses an instance lock to prevent duplicate servers. If an instance
is already running, an interactive launch offers to stop it and start a new
one. Stale PID and control files from a forced termination are cleaned on the
next launch. The supervisor also stops its worker on launcher exit; on Windows,
it additionally uses a kill-on-close Job Object and handles console-close
events.

`start.py` accepts no public arguments other than `--stop`. Its `--worker`
argument and the `AUR_BATAAO_INTERNAL_WORKER`, `AUR_BATAAO_SUPERVISOR_PID`,
`AUR_BATAAO_WORKER_STOP`, and `AUR_BATAAO_WORKER_READY` variables are private
launcher protocol and must not be set manually.

### Development server

For Flask's debugger and automatic reload, use a disposable database rather
than normal task data:

```powershell
$env:AUR_BATAAO_DATABASE = "$PWD\instance\development.sqlite3"
$env:AUR_BATAAO_ATTACHMENTS_DIR = "$PWD\instance\development-attachments"
.\.venv\Scripts\python.exe -m flask --app app:create_app run --debug --port 8080
```

```bash
export AUR_BATAAO_DATABASE="$PWD/instance/development.sqlite3"
export AUR_BATAAO_ATTACHMENTS_DIR="$PWD/instance/development-attachments"
./.venv/bin/python -m flask --app app:create_app run --debug --port 8080
```

The Flask development server does not provide the launcher's process
supervision, browser opening, duplicate-instance check, or remote `--stop`.
Never expose debug mode to an untrusted network.

## Configuration

Configuration is read from the server process environment. PowerShell variables
apply to the current terminal session:

```powershell
$env:AUR_BATAAO_TIMEZONE = "Asia/Kolkata"
$env:AUR_BATAAO_PORT = "8080"
.\.venv\Scripts\python.exe start.py
```

On Linux or macOS, use `export NAME=value` before running `start.py`.
There is no built-in `.env` file loader; use the shell, a process manager, or a
secret manager to populate the environment inherited by the launcher.

### Runtime and storage

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUR_BATAAO_HOST` | `127.0.0.1` | Waitress bind address. Used by `start.py`. |
| `AUR_BATAAO_PORT` | `8080` | Waitress port, from 1 through 65535. Used by `start.py`. |
| `AUR_BATAAO_OPEN_BROWSER` | `1` | Set to `0`, `false`, or `no` to suppress automatic browser opening. |
| `AUR_BATAAO_DATABASE` | `instance/tasks.sqlite3` | SQLite database path. Parent directories are created. |
| `AUR_BATAAO_ATTACHMENTS_DIR` | directory named `attachments` beside the database | Uploaded-file storage. |
| `AUR_BATAAO_TIMEZONE` | `Asia/Kolkata` | IANA timezone used for local dates, stalled activity, and reminders. |

The application creates the database and attachment directories during app
factory initialization. An invalid timezone or nonnumeric port fails startup
with an explanatory error.

The application has no authentication or authorization layer. Binding to a
non-loopback address exposes both the UI and its mutation APIs. Do that only
behind an appropriate VPN, private network, or authenticated reverse proxy.

### Agent defaults

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUR_BATAAO_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | OpenAI-compatible API base URL. |
| `AUR_BATAAO_LLM_MODEL` | unset | Model used to seed the first saved profile. No profile is seeded when unset. |
| `AUR_BATAAO_LLM_API_KEY_ENV` | unset | Name of the environment variable that contains the endpoint secret. |
| `AUR_BATAAO_LLM_API_KEY` | unset | Direct key used only by the environment-seeded profile; the saved profile points back to this variable name. |
| `AUR_BATAAO_LLM_TIMEOUT_SECONDS` | `60` | Provider timeout; valid values are 1 through 600 seconds. |
| `AUR_BATAAO_LLM_SUPPORTS_TOOLS` | `1` | Set exactly to `0` to seed a chat-only profile. |
| `AUR_BATAAO_AGENT_MAX_STEPS` | `8` | Maximum model/tool rounds in one run; valid values are 1 through 32. |

Environment defaults seed a profile only when `AUR_BATAAO_LLM_MODEL` is set and
there are no saved profiles. They never overwrite profiles already stored in
SQLite. After the first seed, manage endpoint details in **Agent > Endpoint
settings**.

### Fixed upload limits

These are application constants rather than environment variables:

- 10 MB per attachment
- 20 attachments per task
- 25 MB for one HTTP upload request

Changing them requires updating `create_app()` in `app.py` and the relevant
tests.

## LLM endpoint configuration

Aur Bataao uses two portable OpenAI-compatible operations:

- Chat Completions for conversations and tool calls
- Models listing for the optional **Discover** action

Model discovery may fail on an otherwise usable endpoint that does not expose a
models-list operation. In that case, enter the model ID manually. A profile
with tool support disabled receives no task tools and remains usable for normal
chat.

### Local endpoint without authentication

```powershell
$env:AUR_BATAAO_LLM_BASE_URL = "http://127.0.0.1:1234/v1"
$env:AUR_BATAAO_LLM_MODEL = "your-local-model"
.\.venv\Scripts\python.exe start.py
```

### Authenticated remote endpoint

Keep the actual key in a separate variable and configure only its name:

```powershell
$env:REMOTE_LLM_KEY = "your-secret-key"
$env:AUR_BATAAO_LLM_BASE_URL = "https://llm.example.com/v1"
$env:AUR_BATAAO_LLM_MODEL = "your-model"
$env:AUR_BATAAO_LLM_API_KEY_ENV = "REMOTE_LLM_KEY"
.\.venv\Scripts\python.exe start.py
```

`AUR_BATAAO_LLM_API_KEY` is a convenience for the seeded profile, but a
provider-specific name such as `REMOTE_LLM_KEY` is preferable when managing
multiple profiles. Only the variable name is stored in SQLite or returned to
the browser. The server resolves its value when an agent run begins and redacts
that value from surfaced provider errors.

Base URLs must be absolute HTTP(S) URLs without embedded credentials, query
parameters, or fragments. Supplying a URL ending in `/chat/completions` is
accepted and normalized back to the API base.

Each conversation is bound to a saved profile. Every run snapshots that
profile's base URL, model, key-variable name, timeout, and tool capability, so a
later profile edit cannot change the configuration recorded for an older run.

The agent exposes `list_tasks` and `get_task` as read-only tools. `list_tasks`
is the single composable query surface: it supports stable `limit`/`offset`
pagination plus workflow, exact-label, blocked, blocking, overdue-follow-up,
overdue-due-date, inclusive due-date range, and title/description/comment text
filters. Filters combine with AND. `get_task` returns both blockers and
dependents so either direction of a dependency chain can be inspected. Creating
or updating tasks,
comments, waiting state, follow-ups, labels, and dependencies all require
explicit approval. Tool calls execute inside SQLite savepoints, and the model
receives either the committed result or a safe error. Conversations, messages,
runs, events, token usage, and approvals are durable. Pending approvals survive
a restart; a run interrupted while actively contacting the model is marked
failed during startup.

## Behavioral model

### Task state and blocking

The only stored workflow states are `todo`, `in_progress`, and `done`.
`blocked` is a derived UI state. An unfinished task is blocked when either:

- it is actively waiting on a person; or
- at least one task dependency is not done.

Completing a task resolves its active waiting record and makes it cease to block
dependent tasks. Reopening it makes those dependencies active again. Dependency
cycles, self-dependencies, and completed active blockers are rejected.

### Focus and smart ordering

The Focused view chooses the first due follow-up, otherwise the first unfinished
and unblocked task from smart order. Due follow-ups remain actionable even
though the underlying task is waiting.

Smart backlog order prioritizes due follow-ups, then workflow/blocking state,
due date, and recency. Rank order uses a persisted sparse integer `rank_key`.
Drag or keyboard reordering supplies both neighboring task IDs; the server
rejects stale adjacency and rebalances all ranks only when there is no safe gap.

Task creation accepts an optional UUID `request_id`. Repeating the same ID
returns the original task rather than creating a duplicate. New tasks are
placed at the front of manual rank order.

### Waiting reminders and activity

A new waiting record defaults its next follow-up to three local calendar days
later. Date-only reminders use 09:00; an exact `HH:MM` time is optional. Dates
in the past and a time without a date are rejected.

Follow-ups create progress events and retain their note and scheduling history.
Resolving waiting does not alter workflow state or task dependencies.

When an in-progress task has qualifying progress, the service attaches an
automatic `active:YYYY-MM-DD` label in the configured timezone. Active-date
labels are reconciled on startup, at most once per minute during task-list
loads, and when the browser detects local midnight or a due timed reminder.
`Stalled` means a task was in progress yesterday but had no progress event that
day.

Manual labels are normalized to lowercase and limited to 32 characters. The
`active:` prefix is reserved for automatic labels.

## Architecture

| Path | Responsibility |
| --- | --- |
| `start.py` | Cross-platform supervisor, Waitress worker, browser launch, instance lock, and graceful shutdown. |
| `app.py` | Flask app factory, schema initialization/migrations, serialization, HTML routes, JSON APIs, uploads, and view selection. |
| `task_service.py` | Transaction-neutral task-domain validation and mutations shared by routes and agent tools. |
| `llm_provider.py` | Small adapter around OpenAI-compatible Chat Completions and Models APIs. |
| `llm_profiles.py` | Endpoint validation, profile persistence, and runtime key resolution. |
| `agent_tools.py` | Model-visible task tool schemas and their read/mutation boundary. |
| `agent_store.py` | Durable conversations, folders, messages, runs, events, usage, and approvals. |
| `agent_runner.py` | Bounded tool loop, approval pause/resume, error sanitization, and savepoint execution. |
| `schema.sql` | Idempotent schema definition, constraints, and indexes. |
| `templates/` | Server-rendered task and agent workspaces. |
| `static/app.js` | Task workspace controller and browser interactions. |
| `static/agent.js` | Agent workspace controller, polling, profiles, folders, and approvals. |
| `static/*_logic.js`, `static/http.js`, `static/markdown.js` | Testable browser-independent decisions, networking, and safe Markdown rendering. |
| `tests/` | Python unit, integration, edge, and launcher tests. |
| `tests-js/` | Node unit tests with numerical coverage gates. |
| `tests-e2e/` | Chromium user journeys and accountable browser coverage. |

Route handlers and agent tools both delegate domain changes to `TaskService`.
Callers own the transaction so task changes and their audit events commit or
roll back together. SQLite connections enable foreign keys and use a five
second busy timeout.

## Persistence and migrations

The default runtime paths are:

```text
instance/
  tasks.sqlite3
  attachments/
  aur-bataao.lock
  aur-bataao.pid
  aur-bataao.stop
```

The last three entries are launcher control files, not user data. Per-worker
ready/stop files may exist briefly while the app runs.

`create_app()` executes `schema.sql` on every startup and then applies small,
idempotent in-place migrations for legacy subtasks, rank keys, waiting times,
retry-safe task IDs, complete agent-run configuration, and conversation
folders. There is no separate migration command.

Before changing schema or migration code:

1. Stop the app and back up the SQLite file plus the attachment directory.
2. Make additive and restart-safe changes where possible.
3. Add a test that opens the pre-change shape and initializes the new app.
4. Verify startup twice against the same migrated database to prove
   idempotence.

For a consistent manual backup, stop the app and copy both the database and
attachments. Do not copy just one: attachment metadata is stored in SQLite,
while bytes are stored on disk.

## HTTP interface

The JSON interface is unversioned and primarily supports the bundled UI and
agent. It has no authentication and should not be treated as a public Internet
API. JSON errors use `{"error": "..."}` with status 400 or 404. Provider
failures use 502. Oversized requests use 413.

### Task and file routes

| Method and path | Purpose |
| --- | --- |
| `GET /?view=focus\|manage\|blocked\|agent` | Render the application shell and selected view. |
| `POST /api/tasks` | Create a task from JSON or multipart data. Supports `title`, optional `blocks_task_id`, UUID `request_id`, and multipart `attachments`. |
| `PATCH /api/tasks/<task_id>` | Update any subset of `title`, `description`, `status`, and nullable `due_date`. |
| `PATCH /api/tasks/<task_id>/rank` | Move a task between required nullable `after_task_id` and `before_task_id` neighbors. |
| `POST /api/tasks/<task_id>/comments` | Add `body` and optional `counts_as_progress`. |
| `POST /api/tasks/<task_id>/attachments` | Add multipart `attachments`. |
| `GET /api/attachments/<attachment_id>` | Preview supported images or download a file; `?download=1` forces download. |
| `DELETE /api/tasks/<task_id>/attachments/<attachment_id>` | Delete attachment metadata and bytes. |
| `PUT /api/tasks/<task_id>/waiting` | Create or update `person_name`, `note`, nullable follow-up date, and nullable time. |
| `POST /api/tasks/<task_id>/follow-ups` | Record a follow-up and its next schedule. |
| `POST /api/tasks/<task_id>/waiting/resolve` | Resolve the active person-based wait. Body must be empty. |
| `POST /api/tasks/<task_id>/labels` | Add a manual label by `name`. |
| `DELETE /api/tasks/<task_id>/labels/<label_id>` | Remove an active task label. |
| `POST /api/tasks/<task_id>/dependencies` | Add `blocker_task_id` for the task. |
| `DELETE /api/tasks/<task_id>/dependencies/<blocker_id>` | Remove a dependency. |
| `POST /api/reconcile` | Reconcile automatic active-date labels and return the change count. |

Equivalent form routes exist for non-JavaScript operation and redirect back to
the selected task view. `GET /` is currently the supported way to retrieve the
complete rendered backlog; there is no general `GET /api/tasks` route.

### Profile and agent routes

| Method and path | Purpose |
| --- | --- |
| `GET /api/llm-profiles` | List public profile fields and whether the named key is available. |
| `POST /api/llm-profiles` | Create a profile from `name`, `base_url`, `model`, nullable `api_key_env`, `timeout_seconds`, `supports_tools`, and `is_default`. |
| `PATCH /api/llm-profiles/<profile_id>` | Update profile fields or make it the default. |
| `GET /api/llm-profiles/<profile_id>/models` | Ask the configured endpoint for model IDs. |
| `GET /api/agent/folders` | List conversation folders. |
| `POST /api/agent/folders` | Create a folder by `name`. |
| `DELETE /api/agent/folders/<folder_id>` | Delete a folder; contained conversations become unfiled. |
| `GET /api/agent/sessions` | List active conversations. |
| `POST /api/agent/sessions` | Create a conversation with optional `profile_id` and `title`. |
| `GET /api/agent/sessions/<session_id>` | Read a conversation, messages, and runs. |
| `PATCH /api/agent/sessions/<session_id>` | Move a conversation using nullable `folder_id`. |
| `POST /api/agent/sessions/<session_id>/messages` | Send `content` and run the bounded agent loop. |
| `POST /api/agent/approvals/<approval_id>` | Decide a tool request with boolean `approved`. |
| `POST /api/agent/runs/<run_id>/resume` | Resume a waiting run after all approvals are decided. Body must be empty. |
| `GET /api/agent/runs/<run_id>` | Inspect run state, durable events, and approvals. |

A message or approval request returns 202 while more approvals are pending, 200
when complete, or 502 when the run fails. The UI polls durable session state so
it can pair tool requests and responses even if the initiating HTTP response is
delayed.

### API example

This PowerShell example creates a task and then adds a due date using the
returned ID:

```powershell
$created = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/api/tasks" `
  -ContentType "application/json" `
  -Body '{"title":"Review the release notes"}'

Invoke-RestMethod `
  -Method Patch `
  -Uri "http://127.0.0.1:8080/api/tasks/$($created.task.id)" `
  -ContentType "application/json" `
  -Body '{"due_date":"2026-09-30"}'
```

Use this interface only in the same trusted environment as the UI. There are no
API tokens, user identities, or per-request permissions.

## Testing and coverage

The commands below use the Windows virtual-environment path. On Linux or macOS,
replace `.\.venv\Scripts\python.exe` with `./.venv/bin/python`; the npm commands
are unchanged.

### Testing philosophy

The project uses 100% coverage as an accountability mechanism, not as a proxy
for correctness. A test is valuable only when it checks an observable contract
or invariant. Merely executing a line to turn it green is not sufficient.

Meaningful assertions include:

- committed state after a successful mutation;
- rolled-back database and filesystem state after a failure;
- cleanup after startup, shutdown, upload, or process errors;
- HTTP status, payload, and error contracts;
- secret redaction and the absence of credentials in public data;
- restored controls and preserved user input after a browser failure;
- ignored stale asynchronous results and suppression of duplicate work; and
- durable run, event, approval, and usage state across agent boundaries.

Every coverage gap must be resolved in exactly one of these ways:

1. Exercise observable behavior with a focused test.
2. Simplify or remove redundant or dead implementation.
3. Add a narrow, documented exclusion only when the branch is genuinely
   platform-specific or unreachable in the process that measures it.

Prefer public routes, services, and browser interactions. Call lower-level
helpers directly only for defensive invariants that cannot be reached through a
valid public workflow. Do not broaden production APIs or add implausible user
flows solely to reach defensive code.

Coverage proves that code executed; it does not prove that assertions would
catch a bad implementation. Review assertion quality with the same care as the
percentage, and consider mutation testing as a periodic diagnostic after the
normal gates are clean.

### Layered test design

Each layer has a distinct job. Put a regression at the lowest layer that can
prove the behavior, then add higher-level coverage only when integration or
real-browser behavior is itself part of the contract.

| Layer | Primary ownership |
| --- | --- |
| Python domain tests | Task, profile, provider, agent-store, tool, and runner validation; state transitions; transaction-neutral behavior; durable invariants. |
| Flask integration tests | Configuration, schema initialization and migrations, serialization, routes, HTTP errors, transaction rollback, file cleanup, and cross-component behavior. |
| Launcher tests | Locks, stale control files, worker lifecycle, stop/start races, signals, browser-disable mode, parent death, and cleanup ordering. |
| JavaScript unit tests | Pure or extracted decisions for Markdown, HTTP failures, task ordering/edit state, agent polling/approvals, stale work, preferences, and coverage reporting. |
| Playwright journeys | Real DOM events, navigation, accessibility state, downloads, persistence, browser storage, rollback UI, races, conversation organization, and approval flows. |

Python domain and Flask tests should assert database and file postconditions,
not just return values or response codes. Where rollback branches are
mechanically duplicated, centralize the transaction boundary instead of
building a collection of mocks that only satisfy coverage.

Browser controllers should stay thin. Extract a controller decision when doing
so creates a cohesive, reusable boundary—such as editing/dirty state,
attachments, ranking, sessions/approvals, polling, or persisted preferences.
Test that boundary with dependency injection and jsdom where DOM primitives are
needed. Keep Playwright responsible for behavior that depends on the real DOM,
navigation lifecycle, file downloads, browser storage, focus/accessibility, or
Chromium timing.

Launcher behavior is split by platform. Mocked unit tests make local failure
paths deterministic, Windows CI verifies Windows behavior, and Ubuntu CI is the
authoritative POSIX check. A platform-only exclusion is acceptable only when
the branch is exercised on its owning platform or is an unavoidable process
entry point.

### Isolation and deterministic boundaries

- Python tests receive a temporary SQLite database and attachment directory
  from `tests/conftest.py`.
- Browser tests run against the isolated server in `tests/e2e_server.py`, not a
  developer's normal database.
- The E2E server resets state between journeys and uses a deterministic fake
  model provider, so tests never require a live LLM or API key.
- Playwright uses one worker because the E2E server is shared. Coverage must not
  depend on one worker surviving the run: Playwright replaces a worker after a
  failed attempt or retry.
- Operating-system calls, process boundaries, clocks, and provider clients are
  mocked or injected when the real dependency would make a unit test unsafe or
  nondeterministic.
- Tests must not depend on ordering, state, or generated files left by a
  previous test run.

### Python

Run the authoritative statement and branch coverage gate:

```powershell
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
```

Coverage is configured in `pyproject.toml`, is measured across application
modules, and fails below 100%. For a focused diagnostic run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_service.py -k waiting -vv
```

Tests use temporary SQLite databases and attachment directories. Launcher tests
mock operating-system boundaries rather than starting orphan processes.

### JavaScript unit tests

```powershell
npm run test:unit
```

Node's built-in test runner enforces 100% lines, branches, and functions for
Markdown, HTTP, task decisions, agent decisions, and the coverage gate and
reporter.

For immediate feedback while editing reusable JavaScript, run the non-gating
watch command in a separate terminal:

```powershell
npm run test:unit:watch
```

The watch command deliberately omits numerical coverage so it can rerun
quickly. `npm run test:unit`, pre-commit, pre-push, and CI remain authoritative.

### Browser tests

```powershell
npm run test:e2e
npm run test:e2e:coverage
```

Both commands run the Chromium journey suite against `tests/e2e_server.py` at
`127.0.0.1:4173`. `test:e2e` reports accountable controller coverage for
diagnosis; `test:e2e:coverage` also enforces the 100% gate. Set `E2E_PYTHON` to
an alternate Python executable when the Playwright server should not use the
repository virtual environment. Set `BROWSER_COVERAGE_GATE=1` to enforce the
gate around a direct Playwright invocation.

The browser suite uses one worker and a deterministic fake model provider. It
fails on user-journey regressions, uncaught page errors, uncalled controller
functions, or uncovered accountable V8 ranges. Exact V8 ranges may be excluded
only for documented schema/DOM invariants or navigation teardown behavior; a
stale exclusion fails the gate. Broad file exclusions are not supported.

Each test attaches its controller V8 entries to its Playwright result. The
run-level coverage reporter merges those entries across worker replacements and
retries. It prints a diagnostic map for every run, but enforces the numerical
gate only when the underlying test run passed. Do not move this assertion back
into a worker fixture or `afterAll`: Playwright tears down and replaces workers
after failures, so worker-local coverage is necessarily partial and would mask
the original error with false uncovered ranges and stale exclusions.

CI retries a failed browser test once, after it has already run in a clean
worker. A retry is diagnostic containment for runner noise, not permission to
leave a known race. Reproduce and fix every flaky first attempt; local runs stay
retry-free so failures remain visible while developing.

Playwright retains traces and screenshots on failure under
`test-results/playwright/`. GitHub Actions uploads that directory together with
`playwright-report/` whenever the JavaScript job fails. To inspect a trace:

```powershell
npx playwright show-trace path\to\trace.zip
```

### Complete local gate

```powershell
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
npm test
```

`npm test` runs the JavaScript unit gate followed by the authoritative browser
coverage gate.

### Coverage accounting and exclusions

Python coverage measures first-party modules with statement and branch
coverage. Tests, virtual environments, and repository hook scripts are omitted;
the numerical threshold is 100%. If a Python branch is platform-specific,
verify it in the appropriate CI job instead of weakening the general gate.

The Node gate measures lines, branches, and functions in the reusable logic,
Markdown, HTTP, and browser-coverage modules. All three thresholds are 100%.
Substantive decisions belong in these accountable modules rather than being
hidden in difficult-to-measure DOM callbacks.

The unit command is wrapped so a runtime warning or missing coverage report is
a hard failure. This prevents Node from returning success after executing tests
without actually enabling its experimental coverage collector.

Playwright collects Chromium V8 coverage for `static/app.js` and
`static/agent.js`. Its report distinguishes:

- **function coverage:** every controller function must run;
- **raw V8 ranges:** diagnostic execution data reported directly by Chromium;
  and
- **accountable ranges:** raw uncovered ranges minus individually justified
  exceptions.

Raw V8 range coverage is informative and browser-version-dependent. The
authoritative browser requirement is 100% function coverage and 100%
accountable range coverage, with no uncaught page errors.

An entry in `BROWSER_COVERAGE_EXCLUSIONS` is permitted only when all of the
following are true:

- it identifies one controller path, function, and source excerpt, with a line
  discriminator where needed;
- its reason explains the schema/DOM invariant, defensive normalization, or
  browser-navigation teardown artifact;
- the user-visible alternative or postcondition is asserted elsewhere; and
- driving the exact V8 range would require an invalid payload, impossible DOM
  state, or loss of the counter during document teardown.

Do not exclude a whole file, function, feature, or merely awkward branch. The
reporter rejects any accountable uncovered range and any exclusion that no
longer matches uncovered code. This stale-exclusion rule forces the allowlist
to evolve with the source instead of silently accumulating obsolete entries.
Use `BROWSER_COVERAGE_VERBOSE=1` to print excluded and uncovered range details
while maintaining the allowlist.

### Test design and testing hygiene

#### Synchronize on completion contracts

A browser action completing means that Chromium dispatched the action; it does
not mean an asynchronous event handler, API request, state refresh, or render
has completed. Every asynchronous transition needs a completion barrier that
proves the state required by the next step.

- Register `waitForResponse`, `waitForEvent`, or a route gate before triggering
  the action so a fast response cannot be missed.
- Check the exact method and resource, assert that the response succeeded, and
  then assert an observable DOM postcondition when response handling performs a
  second asynchronous render.
- Wait on a value that changes because of the action. An attribute or text that
  was already correct before the action is not a completion barrier.
- Do not use arbitrary sleeps or `networkidle` as readiness signals. Background
  requests make network idleness unrelated to the contract, while a sleep only
  changes the probability of a race.
- Drain pending writes before directly manipulating DOM state for a narrow
  controller test. A late response may otherwise run `applyTask` or an
  equivalent renderer and overwrite the synthetic state.

Prefer real routes and user interactions over synthetic DOM mutation. When a
defensive controller branch cannot be reached through a valid workflow, keep
the mutation local, explain the invariant, and trigger recomputation through a
specific observable action.

#### Make concurrency deterministic

Use a deferred promise in a Playwright route when a test must observe an
in-flight operation. Assert both the intermediate contract and the final
postcondition:

- only one request is issued for repeated submission;
- controls that could conflict are disabled and the container exposes its busy
  state;
- Escape, cancel, or repeated submit cannot discard an owned operation;
- success commits and rerenders the authoritative state; and
- failure restores ownership, controls, user input, and a usable retry path.

Production handlers should claim or clear operation ownership synchronously
before their first `await`, restore retryable state in `catch`, and restore
controls in `finally`. Tests that hold a request must release the gate and
remove the route in `finally`; otherwise an assertion failure can leave a
pending handler that obscures the original error during teardown.

Folder and conversation organization are representative examples. Do not open
a dependent picker until folder creation and the following session refresh are
observable. Disable organization controls while that refresh is pending so a
fast user cannot open a picker backed by stale state.

#### Keep journeys focused and identities stable

One Playwright test should describe one behavioral contract. Split a journey
when its setup, failures, and assertions cover independent features; large
coverage-driven journeys amplify timeouts, hide the first broken contract, and
make retries expensive. Share only deterministic setup helpers, such as
creating a conversation or waiting for a folder to render. Keep the behavior's
assertions in the test that owns them.

Playwright locators are live queries. A locator such as `.is-focus-task` can
silently resolve to a different element after rerendering. When identity
matters, read the stable task or session ID and construct a locator for that
specific entity before continuing.

Prefer roles, accessible names, labels, and public controls. Assert relevant
accessibility state (`aria-busy`, `aria-checked`, `aria-expanded`, disabled
controls) as well as visual state. For persisted preferences, assert the value
after reload. For optional browser facilities such as storage, force the API to
throw and verify that the feature remains usable rather than merely suppressing
the exception.

#### Preserve isolation and platform ownership

Patch the narrow dependency reference owned by the module under test. Do not
mutate process-wide modules such as `os.name`: imported modules and `pathlib`
share that object, so a platform simulation can corrupt unrelated pytest and
temporary-directory behavior. Use an injected boundary or a proxy assigned to
the target module, and let the test fixture restore it.

Mock operating-system APIs to cover deterministic local branches, but keep the
native CI platform authoritative for actual process, signal, lock, and path
semantics. The Ubuntu browser job is also a Python integration job because
Playwright launches `tests/e2e_server.py`; it must install the project and test
dependencies before running npm commands.

#### Keep coverage diagnostic, accountable, and secondary

Coverage may fail an otherwise successful run; it must never replace the first
behavioral failure. Per-test coverage attachments and the run-level reporter
are the cross-worker boundary. If the test run failed, retain the behavioral
error and use partial coverage only as a diagnostic. If the test run passed,
missing or malformed controller data, uncalled functions, accountable gaps,
and stale exclusions must fail the run.

Coverage exclusions are source-sensitive review records, not permanent line
number suppressions. Controller edits can move an exact V8 range. Update an
exclusion only after confirming that its documented invariant and user-visible
alternative still hold; remove it when the range becomes covered. Never infer
that an exclusion is stale from a partial worker run.

#### Change and verification cadence

When behavior changes:

1. Identify the owning layer and add a failing assertion for the observable
   contract.
2. Add integration or Playwright coverage only for boundaries the lower layer
   cannot prove.
3. Cover success, validation failure, rollback/cleanup, and stale or repeated
   actions where they are meaningful.
4. Run the smallest focused test while iterating.
5. Run the affected numerical gate and inspect the report; do not rely only on
   its exit code.
6. Run the complete local gate before pushing.

For a timing-sensitive regression, repeat the smallest affected Playwright
selection with `--repeat-each` after the focused test passes. This is additional
evidence, not a replacement for the complete gate. On CI failure, start with
the first behavioral assertion, then inspect the uploaded error context,
screenshot, and trace; downstream coverage diagnostics may describe only the
work completed before that failure.

Work in small, reviewable batches and inspect coverage after each batch. Avoid
broad refactors motivated only by the metric, preserve unrelated user changes,
and do not introduce generated fixtures, experimental dependencies, or large
throwaway scaffolds without a clear need. CI and pre-push should execute the
same authoritative gates so local success has the same meaning as repository
success.

The repository enforces this cadence mechanically:

- pre-commit runs staged-file validation, the complete Python suite without
  coverage instrumentation, and the 100% JavaScript unit gate;
- pre-push runs full Python statement/branch coverage and the authoritative
  JavaScript unit plus Playwright coverage gates;
- repository policy tests fail if thresholds, hooks, workflow commands, or the
  pull-request review prompts drift; and
- the pull-request template requires evidence that a regression test failed
  before the implementation and checks meaningful postconditions afterward.

Local hooks are prompt feedback, not the merge guarantee: `--no-verify` can
bypass them. Protect `main` in the repository host, require pull requests and
all jobs from `.github/workflows/tests.yml`, require the branch to be current,
and disallow force pushes or direct pushes. Those repository settings are the
authoritative boundary that prevents bypassed or missing local checks from
entering `main`.

## Debugging and troubleshooting

### Startup and process issues

- **Port is invalid:** `AUR_BATAAO_PORT` must be an integer from 1 to 65535.
- **Port is already in use:** stop the existing Aur Bataao instance with
  `start.py --stop`, or choose a different port. A process unrelated to Aur
  Bataao must be stopped separately.
- **Browser does not open:** visit the printed URL manually or verify
  `AUR_BATAAO_OPEN_BROWSER` is not set to `0`, `false`, or `no`.
- **A duplicate instance is reported after a crash:** the next supervised
  launch removes stale control files after acquiring the kernel-backed lock.
  Do not delete lock files while a server may still be running.
- **Startup fails on timezone:** use a valid IANA name such as `Asia/Kolkata`
  or `UTC`.

### Database and attachment issues

- Use a separate `AUR_BATAAO_DATABASE` and attachment directory for destructive
  experiments or debugger sessions.
- If an attachment record returns 404, confirm that its stored file was restored
  along with the matching database backup.
- Windows may refuse attachment removal while another program holds the file
  open. Close the preview or external viewer and retry.
- SQLite foreign keys are enabled per connection. If debugging with a separate
  SQLite client, run `PRAGMA foreign_keys = ON` before attempting mutations.
- A write waits up to five seconds for a SQLite lock. Persistent lock failures
  usually indicate another process or database tool holding a transaction.

### Agent issues

- **No endpoint is available:** create one in Endpoint settings or seed the
  first profile with `AUR_BATAAO_LLM_MODEL`.
- **API key variable is not set:** define the exact variable named by the saved
  profile in the terminal that launches Aur Bataao, then restart the app.
- **Discover fails:** verify the base URL and key. If chat works but the endpoint
  lacks a Models API, enter the model ID manually.
- **Tool calls fail or are malformed:** confirm the model supports OpenAI-style
  function tools. Otherwise disable **Endpoint supports tool calling** and use
  it for chat only.
- **Run exceeded the step limit:** inspect the durable run events, then increase
  `AUR_BATAAO_AGENT_MAX_STEPS` within the 1-32 range or use a model that
  completes tool sequences more reliably.
- **Run failed after restart:** runs in the middle of provider/tool processing
  are intentionally marked failed. Runs already waiting for approval retain
  their approval state and can be resumed.

Provider exceptions are truncated before reaching the UI, and the resolved API
key is replaced if it appears in the message. Do not add logging that serializes
the environment or provider client configuration.

### Browser and UI issues

- The selected theme, rank/smart sort choice, and agent width are stored in
  browser `localStorage`; clearing site data resets them without deleting task
  data.
- Navigation is intentionally locked while an inline edit is dirty. Finish or
  revert the edit before switching views.
- A visible agent warning badge can indicate a pending approval or a failed run;
  open Agent and inspect the conversation.
- Use the browser console together with `npm run test:e2e` when diagnosing DOM
  controller failures. The suite treats uncaught page errors as failures.

## Git hooks and CI

Enable hooks once per clone:

```powershell
git config --local core.hooksPath .githooks
```

The hooks are:

- **pre-commit:** checks staged whitespace, conflict markers, generated/local
  paths, files over 100 KiB, and staged Python syntax without writing bytecode;
  it then runs all Python tests without coverage instrumentation and the 100%
  JavaScript unit gate.
- **commit-msg:** requires a Conventional Commit type, a subject no longer than
  50 characters, a blank line before an optional body, and body lines no longer
  than 72 characters. Merge/revert and `fixup!`/`squash!` subjects are exempt
  from the prefix rule.
- **pre-push:** runs Python coverage and `npm test`; the push stops on any
  behavior or coverage failure.

Allowed commit types are `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`,
`refactor`, `revert`, `style`, and `test`. Lowercase scopes and breaking-change
markers are supported:

```text
feat(tasks)!: change ranking behavior
```

Hook scripts locate Python in `.venv/Scripts/python.exe`, then
`.venv/bin/python`, then `python3` or `python` on `PATH`. Both behavioral hooks
require npm dependencies; pre-push also requires the managed Chromium build.
When changing hook entry points on Windows, preserve executable mode for other
platforms:

```powershell
git add --chmod=+x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push
```

GitHub Actions repeats the checks on every push and pull request. Python runs on
Windows with Python 3.12 and full coverage, plus Ubuntu with Python 3.11 for
cross-platform behavior. JavaScript and Chromium run on Ubuntu with Node 24 and
Python 3.11 because the browser suite starts the Python E2E server. That job
installs both dependency sets, retries a failed browser test once, and uploads
the Playwright HTML report, error context, screenshots, and traces on failure.
Local hooks can be bypassed with `--no-verify`, but CI remains the authoritative
merge gate.
