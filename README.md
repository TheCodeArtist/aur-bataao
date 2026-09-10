# Aur Bataao

A compact, single-user task tracker built with Flask, server-rendered HTML, vanilla JavaScript, and SQLite. New tasks are captured in a focused dialog, every task can hold files added through the picker, drag and drop, or clipboard paste, and rank sorting supports persistent drag or keyboard reordering.

## Run locally (Windows PowerShell)

```powershell
.\.venv\Scripts\Activate.ps1
aur-bataao
```

Open <http://127.0.0.1:8080>. The SQLite database is created at `instance/tasks.sqlite3`.

Configuration is optional:

```powershell
$env:AUR_BATAAO_TIMEZONE = "Asia/Kolkata"
$env:AUR_BATAAO_HOST = "127.0.0.1"
$env:AUR_BATAAO_PORT = "8080"
$env:AUR_BATAAO_ATTACHMENTS_DIR = "D:\AurBataao\attachments"
aur-bataao
```

Keep the default loopback host unless access is protected by a private network, VPN, or authenticated reverse proxy.

Attachments default to `instance/attachments`. Each file is limited to 10 MB, each task to 20 attachments, and each upload request to 25 MB. Backups should include both the SQLite database and the attachments directory.

## Development

```powershell
.\.venv\Scripts\Activate.ps1
pytest --basetemp=.pytest-tmp
```

The app initializes its schema automatically. Task-list loads reconcile active date labels at most once per minute, and an open browser checks for local midnight once per minute.

## One-click launcher (Windows)

Double-click `Start Aur Bataao.cmd` in the project folder. It opens
<http://127.0.0.1:8080> in the default browser and keeps the local server running
in a terminal window. Close that terminal window to stop the app.

For desktop access, right-click the launcher, choose **Show more options → Send
to → Desktop (create shortcut)**, and optionally set the shortcut's **Run**
property to **Minimized**.
