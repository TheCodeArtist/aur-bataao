# Aur Bataao

A compact, single-user task tracker built with Flask, server-rendered HTML,
vanilla JavaScript, and SQLite.

## What it does

- Opens in the focused **Aur Bataao** view with one actionable task and an
  instant next suggestion.
- Keeps the complete backlog available through **View all tasks**.
- Supports expandable updates and comments.
- Captures new tasks in a focused dialog.
- Accepts attachments from the file picker, drag and drop, or clipboard paste.
- Saves rank changes made by dragging tasks or using the keyboard in the
  backlog view.
- Tracks task dependencies and tasks that are **Waiting on** another person as
  blocking reasons, independently of workflow status.
- Includes an interactive LLM agent that can inspect tasks, discuss next steps,
  and propose task changes for explicit approval.

## Interactive agent

Open **Agent** from the shared view navigation, or browse directly to
<http://127.0.0.1:8080/?view=agent>. The workspace supports multiple named profiles
for local or remote OpenAI-compatible endpoints.

An endpoint needs the Chat Completions API for chat. Tool-capable models can
also inspect and update task data. Profiles without tool support remain usable
for ordinary conversation. Use **Discover** to search and select a model exposed
by the endpoint, or enter its model ID manually when discovery is unsupported.

Read-only tools run automatically. Every task mutation is shown with its exact
arguments and pauses until it is approved or rejected. Conversations, runs,
tool events, failures, token totals, and pending approvals are stored in SQLite
so an interrupted approval can be resumed.

API keys are never stored in SQLite or returned to the browser. A profile stores
only the name of an environment variable whose value the server resolves when
a run starts.

### Waiting-on reminders

When a task is waiting on another person, Aur Bataao records the last follow-up
and the next reminder. The task keeps its **To do** or **In progress** workflow
status while the active wait makes it unavailable for regular focused work.

- Reminders can include an exact time.
- Date-only reminders become due at 9:00 AM in the configured user timezone.
- Due follow-ups appear as actionable cards in the focused view.
- Completing a follow-up schedules the next reminder.
- **No longer waiting** removes only the person-based blocking reason and keeps
  the task's workflow status. Other task dependencies continue to block it.

Configure this workflow from the **Waiting on** section in a task's expanded
details. Workflow status remains limited to **To do**, **In progress**, and
**Done**.

## Run locally (Windows PowerShell)

```powershell
.\.venv\Scripts\python.exe start.py
```

Open <http://127.0.0.1:8080>. The SQLite database is created at `instance/tasks.sqlite3`.

### Configuration

Configuration is optional. The available environment variables are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUR_BATAAO_TIMEZONE` | `Asia/Kolkata` | Timezone used for reminders |
| `AUR_BATAAO_HOST` | `127.0.0.1` | Server bind address |
| `AUR_BATAAO_PORT` | `8080` | Server port |
| `AUR_BATAAO_ATTACHMENTS_DIR` | `instance/attachments` | Attachment storage directory |
| `AUR_BATAAO_OPEN_BROWSER` | `1` | Set to `0` to skip opening the browser |
| `AUR_BATAAO_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | Default compatible API base URL |
| `AUR_BATAAO_LLM_MODEL` | unset | Default model; setting this seeds the first profile |
| `AUR_BATAAO_LLM_API_KEY_ENV` | unset | Name of the variable containing the API key |
| `AUR_BATAAO_LLM_API_KEY` | unset | Direct key for the environment-seeded profile |
| `AUR_BATAAO_LLM_TIMEOUT_SECONDS` | `60` | LLM timeout, from 1 to 600 seconds |
| `AUR_BATAAO_LLM_SUPPORTS_TOOLS` | `1` | Set to `0` for a chat-only default model |
| `AUR_BATAAO_AGENT_MAX_STEPS` | `8` | Maximum model/tool rounds, from 1 to 32 |

Example:

```powershell
$env:AUR_BATAAO_TIMEZONE = "Asia/Kolkata"
$env:AUR_BATAAO_HOST = "127.0.0.1"
$env:AUR_BATAAO_PORT = "8080"
$env:AUR_BATAAO_ATTACHMENTS_DIR = "D:\AurBataao\attachments"
.\.venv\Scripts\python.exe start.py
```

To seed a local endpoint that does not require a key:

```powershell
$env:AUR_BATAAO_LLM_BASE_URL = "http://127.0.0.1:1234/v1"
$env:AUR_BATAAO_LLM_MODEL = "your-local-model"
.\.venv\Scripts\python.exe start.py
```

For an authenticated remote endpoint, keep the secret in its own environment
variable and point the profile at that variable's name:

```powershell
$env:REMOTE_LLM_KEY = "your-secret-key"
$env:AUR_BATAAO_LLM_BASE_URL = "https://llm.example.com/v1"
$env:AUR_BATAAO_LLM_MODEL = "your-model"
$env:AUR_BATAAO_LLM_API_KEY_ENV = "REMOTE_LLM_KEY"
.\.venv\Scripts\python.exe start.py
```

If profiles already exist, startup does not replace them. Add or edit profiles
from **Endpoint settings** in the agent workspace.

Keep the default loopback host unless access is protected by a private network,
VPN, or authenticated reverse proxy.

### Attachments and backups

- Maximum file size: 10 MB
- Maximum attachments per task: 20
- Maximum upload request size: 25 MB

Backups should include both the SQLite database and the attachments directory.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
npm ci
git config --local core.hooksPath .githooks
.\.venv\Scripts\python.exe -m pytest --cov --cov-report=term-missing
npm test
```

Python coverage includes statement and branch coverage across the application
and fails below 95%. JavaScript coverage uses Node's built-in test runner and
fails below 95% for lines, branches, or functions in the standalone Markdown
renderer. The DOM-heavy controllers stay outside the numerical JavaScript gate;
their server contracts are covered by Flask integration tests. Extract reusable
logic into importable modules before adding it to the gate, and add a small real-
browser smoke suite only when cross-browser behavior warrants that extra cost.

The app initializes its schema automatically. Task-list loads reconcile active
date labels at most once per minute, and an open browser checks for local
midnight once per minute.

The agent implementation is split into small boundaries:

- `llm_provider.py` adapts the portable OpenAI-compatible API surface.
- `llm_profiles.py` validates endpoint profiles and resolves key variables.
- `agent_tools.py` defines the model-visible task capability boundary.
- `agent_store.py` persists sessions, runs, events, messages, and approvals.
- `agent_runner.py` owns the bounded tool loop and approval resume behavior.

### Git hooks

Enable the repository hooks once after cloning:

```powershell
git config --local core.hooksPath .githooks
```

Git does not copy local configuration during a clone or automatically enable
repository-provided hooks.

The hooks provide these checks:

- **`pre-commit`:** Rejects staged whitespace errors, conflict markers,
  generated or local files, and files larger than 100 KiB. It also compiles
  staged Python to catch syntax errors without writing bytecode.
- **`commit-msg`:** Limits the subject to 50 characters and description lines
  to 72 characters. It requires a Conventional Commit prefix and a blank line
  before an optional description.
- **`pre-push`:** Runs branch-aware Python coverage and the JavaScript test and
  coverage suite. The push stops if either suite fails or falls below 95%.

Supported Conventional Commit types are `build`, `chore`, `ci`, `docs`, `feat`,
`fix`, `perf`, `refactor`, `revert`, `style`, and `test`. Lowercase scopes and
breaking-change markers are supported:

```text
feat(tasks)!: change ranking behavior
```

Git-generated merge and revert subjects, plus `fixup!` and `squash!` subjects,
are exempt from the prefix rule.

The hooks look for Python in this order:

1. `.venv\Scripts\python.exe` on Windows
2. `.venv/bin/python` on Linux and macOS
3. `python3` or `python` on `PATH`

Commits and pushes fail with a setup message when Python is unavailable. Pushes
also require Node.js 20.19 or newer, npm, and dependencies installed with
`npm ci`.

When adding or updating hook entry points on Windows, preserve their executable
mode for other platforms:

```powershell
git add --chmod=+x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push
```

Local hooks can be bypassed with `--no-verify`. Authoritative enforcement
requires the same validation in CI as a mandatory merge check.

## Launcher

Start the launcher with:

```powershell
.\.venv\Scripts\python.exe start.py
```

The launcher:

- Opens the app in the default browser unless `AUR_BATAAO_OPEN_BROWSER=0`.
- Supervises the server worker in the foreground.
- Shuts down the complete process tree after Ctrl+C or a normal termination
  signal.
- Uses a kernel-backed instance lock to prevent duplicate servers.
- Discards stale PID and control files left by forced termination.
- Uses a kill-on-close Job Object on Windows.
- Handles console closure and parent death to prevent orphaned listeners.

To request a graceful shutdown from another terminal:

```powershell
.\.venv\Scripts\python.exe start.py --stop
```

If the launcher finds an existing instance, it offers to stop that instance and
start a fresh one. Declining leaves the running instance untouched.
