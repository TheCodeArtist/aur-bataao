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
pytest --basetemp=.pytest-tmp
```

The app initializes its schema automatically. Task-list loads reconcile active date labels at most once per minute, and an open browser checks for local midnight once per minute.

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
