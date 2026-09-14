from __future__ import annotations

import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import FrameType
from typing import IO

from waitress import create_server, wasyncore

from app import create_app


ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / "instance" / "aur-bataao.pid"
STOP_FILE = ROOT / "instance" / "aur-bataao.stop"


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """A small cross-platform lock that also records the launcher's PID."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: IO[str] | None = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+", encoding="utf-8")
        self.file.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise AlreadyRunningError("Aur Bataao is already running.") from exc

        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None
            self.path.unlink(missing_ok=True)


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def request_stop() -> int:
    if not PID_FILE.exists():
        STOP_FILE.unlink(missing_ok=True)
        print("Aur Bataao is not running.")
        return 0

    STOP_FILE.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while PID_FILE.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if PID_FILE.exists():
        print("Aur Bataao did not stop within 10 seconds.", file=sys.stderr)
        return 1
    print("Aur Bataao stopped cleanly.")
    return 0


def confirm_restart() -> bool:
    print("An Aur Bataao instance is already running.")
    try:
        answer = input(
            "Stop the existing instance gracefully and launch a new one? [y/N]: "
        )
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""

    if answer.strip().lower() in {"y", "yes"}:
        return True

    print("Launch skipped. The existing instance will continue running.")
    return False


def main() -> int:
    if sys.argv[1:] == ["--stop"]:
        return request_stop()
    if sys.argv[1:]:
        print("Usage: python start.py [--stop]", file=sys.stderr)
        return 2

    host = os.getenv("AUR_BATAAO_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("AUR_BATAAO_PORT", "8080"))
    except ValueError:
        print("AUR_BATAAO_PORT must be a number.", file=sys.stderr)
        return 2

    try:
        with InstanceLock(PID_FILE):
            STOP_FILE.unlink(missing_ok=True)
            try:
                server = create_server(create_app(), host=host, port=port, threads=4)
            except OSError as exc:
                print(f"Could not start Aur Bataao on {host}:{port}: {exc}", file=sys.stderr)
                return 1

            stopping = False
            stopping_lock = threading.Lock()
            watcher_done = threading.Event()

            def stop_server(signum: int, _frame: FrameType | None) -> None:
                nonlocal stopping
                with stopping_lock:
                    if stopping:
                        return
                    stopping = True
                reason = f"signal {signum}" if signum else "stop request"
                print(f"\nStopping Aur Bataao ({reason})...")
                server.close()
                server.task_dispatcher.shutdown(cancel_pending=False, timeout=5)
                wasyncore.close_all(server._map)

            def watch_for_stop_request() -> None:
                while not watcher_done.wait(0.2):
                    if STOP_FILE.exists():
                        stop_server(0, None)
                        return

            signal.signal(signal.SIGINT, stop_server)
            signal.signal(signal.SIGTERM, stop_server)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, stop_server)

            watcher = threading.Thread(
                target=watch_for_stop_request,
                name="aur-bataao-stop-watcher",
                daemon=True,
            )
            watcher.start()

            url = _browser_url(host, port)
            print(f"Aur Bataao is running at {url}")
            print("Press Ctrl+C to stop it cleanly.")
            if os.getenv("AUR_BATAAO_OPEN_BROWSER", "1").lower() not in {"0", "false", "no"}:
                webbrowser.open(url)

            try:
                server.run()
            finally:
                watcher_done.set()
                server.close()
                server.task_dispatcher.shutdown(timeout=5)
                wasyncore.close_all(server._map)
                STOP_FILE.unlink(missing_ok=True)
    except AlreadyRunningError:
        if not confirm_restart():
            return 0
        if request_stop() != 0:
            return 1
        print("Launching a new Aur Bataao instance...")
        return main()

    print("Aur Bataao stopped cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
