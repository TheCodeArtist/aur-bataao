# Aur Bataao

A compact, single-user task tracker built with Flask, server-rendered HTML, vanilla JavaScript, and SQLite. It opens in the focused **Aur Bataao** view with one actionable task, an instant client-side next suggestion, and expandable updates and comments. The complete backlog remains available through **View all tasks**. New tasks are captured in a focused dialog, every task can hold files added through the picker, drag and drop, or clipboard paste, and rank sorting supports persistent drag or keyboard reordering in the backlog view.

Tasks can also be marked as **Waiting on** a person. The task remains blocked while Aur Bataao records the last follow-up and the next reminder. A reminder can optionally have an exact time; date-only reminders become due at 9:00 AM in the configured user timezone. When the reminder arrives, the follow-up becomes an actionable card in the focused view; completing it schedules the next reminder, while **No longer blocked** returns the underlying task to **To do**. Choose **Waiting on someone…** from a task's status menu for quick access, or use the Waiting on section in its expanded details.

## Run locally (Windows PowerShell)

```powershell
.\.venv\Scripts\python.exe start.py
```

Open <http://127.0.0.1:8080>. The SQLite database is created at `instance/tasks.sqlite3`.

Configuration is optional:

```powershell
$env:AUR_BATAAO_TIMEZONE = "Asia/Kolkata"
$env:AUR_BATAAO_HOST = "127.0.0.1"
$env:AUR_BATAAO_PORT = "8080"
$env:AUR_BATAAO_ATTACHMENTS_DIR = "D:\AurBataao\attachments"
.\.venv\Scripts\python.exe start.py
```

Keep the default loopback host unless access is protected by a private network, VPN, or authenticated reverse proxy.

Attachments default to `instance/attachments`. Each file is limited to 10 MB, each task to 20 attachments, and each upload request to 25 MB. Backups should include both the SQLite database and the attachments directory.

## Development

```powershell
.\.venv\Scripts\Activate.ps1
git config --local core.hooksPath .githooks
pytest --basetemp=.pytest-tmp
```

The app initializes its schema automatically. Task-list loads reconcile active date labels at most once per minute, and an open browser checks for local midnight once per minute.

### Git hooks

Run `git config --local core.hooksPath .githooks` once after cloning the
repository. Git does not copy local configuration during a clone and does not
automatically enable repository-provided hooks for security reasons.

The repository uses these hooks:

- `pre-commit` rejects staged whitespace errors, unresolved conflict markers,
  generated or local files, and files larger than 100 KiB. It also compiles
  staged Python content to catch syntax errors without writing bytecode.
- `commit-msg` limits the subject to 50 characters, requires Conventional
  Commit prefixes such as `feat:`, `fix:`, or `docs:`, and requires a blank line
  before an optional description. Description lines are limited to 72
  characters. Git-generated merge and revert subjects, plus `fixup!` and
  `squash!` subjects, are exempt from the prefix rule.
- `pre-push` runs the complete test suite with
  `pytest --basetemp=.pytest-tmp`.

The supported Conventional Commit types are `build`, `chore`, `ci`, `docs`,
`feat`, `fix`, `perf`, `refactor`, `revert`, `style`, and `test`. An optional
lowercase scope and breaking-change marker are supported, for example
`feat(tasks)!: change ranking behavior`.

The hooks use `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on
Linux and macOS, falling back to `python3` or `python` from `PATH`. Commits and
pushes fail with a setup message when Python is unavailable. When adding or
updating hook entry points on Windows, preserve their executable mode for other
platforms:

```powershell
git add --chmod=+x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push
```

Local hooks provide early feedback but can be bypassed with `--no-verify`.
Authoritative enforcement requires running the same validation in CI and making
that check mandatory before merging.

## Launcher

Run `.\.venv\Scripts\python.exe start.py`. The launcher opens the app in the
default browser and waits in the foreground while a supervised server worker
runs, so Ctrl+C or a normal termination signal shuts down the complete process
tree. It also uses a kernel-backed instance lock to prevent duplicate servers
and discards stale PID/control files left by a forced termination. On Windows,
a supervised worker runs inside a kill-on-close Job Object. Native console-close
and parent-death handling prevent an orphaned listener when the terminal or virtual
environment launcher is killed. Set `AUR_BATAAO_OPEN_BROWSER=0` to skip opening
the browser. From another terminal, run
`.\.venv\Scripts\python.exe start.py --stop` to request a graceful shutdown. If
`start.py` finds an instance already running, it offers to stop that instance
gracefully and launch a fresh one; declining leaves it untouched.
