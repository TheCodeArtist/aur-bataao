# Aur Bataao

A compact, single-user task tracker built with Flask, server-rendered HTML, vanilla JavaScript, and SQLite.

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
aur-bataao
```

Keep the default loopback host unless access is protected by a private network, VPN, or authenticated reverse proxy. For a backup, stop the process and copy the `instance/tasks.sqlite3` file.

## Development

```powershell
.\.venv\Scripts\Activate.ps1
pytest --basetemp=.pytest-tmp
```

The app initializes its schema automatically. Task-list loads reconcile active date labels at most once per minute, and an open browser checks for local midnight once per minute.

