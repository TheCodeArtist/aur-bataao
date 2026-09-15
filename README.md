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
- Tracks tasks that are **Waiting on** another person.

### Waiting-on reminders

When a task is blocked by another person, Aur Bataao records the last follow-up
and the next reminder.

- Reminders can include an exact time.
- Date-only reminders become due at 9:00 AM in the configured user timezone.
- Due follow-ups appear as actionable cards in the focused view.
- Completing a follow-up schedules the next reminder.
- **No longer blocked** returns the underlying task to **To do**.

To configure this state, choose **Waiting on someone…** from a task's status
menu or use the **Waiting on** section in its expanded details.

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

Example:

```powershell
$env:AUR_BATAAO_TIMEZONE = "Asia/Kolkata"
$env:AUR_BATAAO_HOST = "127.0.0.1"
$env:AUR_BATAAO_PORT = "8080"
$env:AUR_BATAAO_ATTACHMENTS_DIR = "D:\AurBataao\attachments"
.\.venv\Scripts\python.exe start.py
```

Keep the default loopback host unless access is protected by a private network,
VPN, or authenticated reverse proxy.

### Attachments and backups

- Maximum file size: 10 MB
- Maximum attachments per task: 20
- Maximum upload request size: 25 MB

Backups should include both the SQLite database and the attachments directory.

## Development

```powershell
.\.venv\Scripts\Activate.ps1
git config --local core.hooksPath .githooks
pytest --basetemp=.pytest-tmp
```

The app initializes its schema automatically. Task-list loads reconcile active
date labels at most once per minute, and an open browser checks for local
midnight once per minute.

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
- **`pre-push`:** Runs `pytest --basetemp=.pytest-tmp`.

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

Commits and pushes fail with a setup message when Python is unavailable.

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
