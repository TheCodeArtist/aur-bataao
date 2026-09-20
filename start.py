from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from types import FrameType
from typing import IO, Callable

from waitress import create_server, wasyncore

from app import create_app


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "instance"
LOCK_FILE = RUNTIME_DIR / "aur-bataao.lock"
PID_FILE = RUNTIME_DIR / "aur-bataao.pid"
STOP_FILE = RUNTIME_DIR / "aur-bataao.stop"
STARTUP_TIMEOUT = 20.0
GRACEFUL_STOP_TIMEOUT = 5.0
LOG_FORMAT = "[%(asctime)s.%(msecs)03d] %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def log_formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


class WaitressQueueLogFilter(logging.Filter):
    """Give Waitress's queue warning user-facing context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "Task queue depth is %d" or not record.args:
            return True

        try:
            depth = int(record.args[0])
        except (TypeError, ValueError):
            return True

        if depth <= 2:
            record.levelno = logging.INFO
            record.levelname = "INFO"
            record.msg = (
                "%d request%s waiting for a server worker; "
                "no action needed unless frequent or growing"
            )
            record.args = (depth, "" if depth == 1 else "s")
        else:
            record.msg = (
                "Server request backlog: %d requests waiting for workers; "
                "investigate if this persists or keeps growing"
            )
            record.args = (depth,)
        return True


def configure_logging() -> None:
    """Timestamp application logs and clarify Waitress queue messages."""
    root_logger = logging.getLogger()
    if root_logger.handlers:
        for root_handler in root_logger.handlers:
            root_handler.setFormatter(log_formatter())
    else:
        root_handler = logging.StreamHandler()
        root_handler.setFormatter(log_formatter())
        root_logger.addHandler(root_handler)

    queue_logger = logging.getLogger("waitress.queue")
    if any(
        getattr(handler, "_aur_bataao_queue_handler", False)
        for handler in queue_logger.handlers
    ):
        return

    handler = logging.StreamHandler()
    handler._aur_bataao_queue_handler = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO)
    handler.setFormatter(log_formatter())
    handler.addFilter(WaitressQueueLogFilter())
    queue_logger.addHandler(handler)
    queue_logger.propagate = False


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """Cross-platform single-instance lock that records the supervisor PID."""

    def __init__(self, path: Path, pid_path: Path | None = PID_FILE) -> None:
        self.path = path
        self.pid_path = pid_path
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

        if self.pid_path is not None:
            try:
                self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            except BaseException:
                self.__exit__()
                raise
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
            if self.pid_path is not None:
                self.pid_path.unlink(missing_ok=True)


class ParentWatcher:
    """Requests shutdown when the process that launched us disappears."""

    def __init__(self, parent_pid: int, on_exit: Callable[[str], None]) -> None:
        self.parent_pid = parent_pid
        self.on_exit = on_exit
        self.done = threading.Event()
        self.thread: threading.Thread | None = None
        self.process_handle: int | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._watch,
            name="aur-bataao-parent-watcher",
            daemon=True,
        )
        self.thread.start()

    def _watch(self) -> None:
        if os.name == "nt":
            self._watch_windows()
            return

        while not self.done.wait(0.25):
            if os.getppid() != self.parent_pid:
                self.on_exit("launcher parent exited")
                return

    def _watch_windows(self) -> None:
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_object_0 = 0x00000000
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(synchronize, False, self.parent_pid)
        if not handle:
            # Access can be denied for protected parents. Signal only when the
            # process is known to have disappeared.
            if ctypes.get_last_error() == 87:
                self.on_exit("launcher parent exited")
            return

        self.process_handle = int(handle)
        try:
            while not self.done.is_set():
                result = kernel32.WaitForSingleObject(handle, 250)
                if result == wait_object_0:
                    self.on_exit("launcher parent exited")
                    return
                if result == wait_failed:
                    return
        finally:
            kernel32.CloseHandle(handle)
            self.process_handle = None

    def close(self) -> None:
        self.done.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)


class WindowsConsoleCloseHandler:
    """Handle terminal close/logoff/shutdown events omitted by signal.signal."""

    handled_events = {2: "terminal closed", 5: "user logged off", 6: "system shutdown"}

    def __init__(
        self,
        request_shutdown: Callable[[str], None],
        cleanup_finished: threading.Event,
    ) -> None:
        self.request_shutdown = request_shutdown
        self.cleanup_finished = cleanup_finished
        self.callback: object | None = None
        self.kernel32: object | None = None

    def install(self) -> None:
        if os.name != "nt":
            return

        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
        kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL

        @handler_type
        def callback(event: int) -> bool:
            reason = self.handled_events.get(event)
            if reason is None:
                return False
            self.request_shutdown(reason)
            # Windows allows only a short interval for console-close cleanup.
            self.cleanup_finished.wait(GRACEFUL_STOP_TIMEOUT)
            return True

        if not kernel32.SetConsoleCtrlHandler(callback, True):
            raise ctypes.WinError(ctypes.get_last_error())
        self.callback = callback
        self.kernel32 = kernel32

    def close(self) -> None:
        if self.kernel32 is not None and self.callback is not None:
            self.kernel32.SetConsoleCtrlHandler(self.callback, False)
        self.callback = None
        self.kernel32 = None


class WindowsJob:
    """Kernel container that kills the worker tree if its supervisor dies."""

    kill_on_job_close = 0x00002000
    extended_limit_information = 9

    def __init__(self) -> None:
        self.handle: int | None = None
        self.assigned = False
        if os.name == "nt":
            self._create()

    def _create(self) -> None:
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = self.kill_on_job_close
        if not kernel32.SetInformationJobObject(
            handle,
            self.extended_limit_information,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        self.handle = int(handle)

    def assign(self, process_handle: int) -> bool:
        if self.handle is None:
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.assigned = bool(kernel32.AssignProcessToJobObject(self.handle, process_handle))
        return self.assigned

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle is None or not self.assigned:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject(self.handle, exit_code)

    def close(self) -> None:
        if self.handle is None:
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)
        self.handle = None


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/"


def _read_configuration() -> tuple[str, int] | None:
    host = os.getenv("AUR_BATAAO_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("AUR_BATAAO_PORT", "8080"))
    except ValueError:
        print("AUR_BATAAO_PORT must be a number.", file=sys.stderr)
        return None
    if not 1 <= port <= 65535:
        print("AUR_BATAAO_PORT must be between 1 and 65535.", file=sys.stderr)
        return None
    return host, port


def _remove_stale_control_files(*, include_identity: bool) -> None:
    STOP_FILE.unlink(missing_ok=True)
    for pattern in ("aur-bataao-worker-*.ready", "aur-bataao-worker-*.stop"):
        for path in RUNTIME_DIR.glob(pattern):
            path.unlink(missing_ok=True)
    if include_identity:
        PID_FILE.unlink(missing_ok=True)


def instance_is_running() -> bool:
    try:
        with InstanceLock(LOCK_FILE, pid_path=None):
            pass
    except AlreadyRunningError:
        return True

    # A hard kill cannot run finally blocks. Once the kernel lock is free,
    # these files are conclusively stale and safe to remove.
    _remove_stale_control_files(include_identity=True)
    return False


def request_stop() -> int:
    if not instance_is_running():
        print("Aur Bataao is not running.")
        return 0

    STOP_FILE.write_text("stop\n", encoding="utf-8")
    deadline = time.monotonic() + 10
    while instance_is_running() and time.monotonic() < deadline:
        time.sleep(0.1)
    if instance_is_running():
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


def _resume_windows_process(process_handle: int) -> None:
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(process_handle)
    if status < 0:
        raise OSError(f"NtResumeProcess failed with status 0x{status & 0xFFFFFFFF:08x}")


def _spawn_worker(
    token: str,
    stop_path: Path,
    ready_path: Path,
) -> tuple[subprocess.Popen[bytes], WindowsJob | None]:
    environment = os.environ.copy()
    environment.update(
        AUR_BATAAO_INTERNAL_WORKER=token,
        AUR_BATAAO_SUPERVISOR_PID=str(os.getpid()),
        AUR_BATAAO_WORKER_STOP=str(stop_path),
        AUR_BATAAO_WORKER_READY=str(ready_path),
        AUR_BATAAO_OPEN_BROWSER="0",
    )
    command = [sys.executable, str(Path(__file__).resolve()), "--worker"]

    if os.name != "nt":
        return subprocess.Popen(command, env=environment, start_new_session=True), None

    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
    job: WindowsJob | None
    try:
        job = WindowsJob()
    except OSError as exc:
        print(f"Warning: Windows process containment is unavailable: {exc}", file=sys.stderr)
        job = None

    process = subprocess.Popen(command, env=environment, creationflags=creation_flags)
    try:
        if job is not None and not job.assign(process._handle):
            error = ctypes.WinError(ctypes.get_last_error())
            print(f"Warning: worker Job Object assignment failed: {error}", file=sys.stderr)
        _resume_windows_process(process._handle)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        if job is not None:
            job.close()
        raise
    return process, job


def _force_stop_worker(
    process: subprocess.Popen[bytes],
    job: WindowsJob | None,
) -> None:
    if job is not None and job.assigned:
        job.terminate()
    elif os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _stop_worker(
    process: subprocess.Popen[bytes],
    job: WindowsJob | None,
    stop_path: Path,
) -> None:
    if process.poll() is not None:
        return
    stop_path.write_text("stop\n", encoding="utf-8")
    try:
        process.wait(timeout=GRACEFUL_STOP_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        print("Worker did not stop gracefully; terminating its process tree.", file=sys.stderr)

    _force_stop_worker(process, job)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _worker_main() -> int:
    token = os.getenv("AUR_BATAAO_INTERNAL_WORKER")
    stop_value = os.getenv("AUR_BATAAO_WORKER_STOP")
    ready_value = os.getenv("AUR_BATAAO_WORKER_READY")
    supervisor_value = os.getenv("AUR_BATAAO_SUPERVISOR_PID")
    if not token or not stop_value or not ready_value or not supervisor_value:
        print("The internal worker must be started by the Aur Bataao supervisor.", file=sys.stderr)
        return 2

    configuration = _read_configuration()
    if configuration is None:
        return 2
    host, port = configuration
    stop_path = Path(stop_value)
    ready_path = Path(ready_value)
    try:
        supervisor_pid = int(supervisor_value)
    except ValueError:
        return 2

    configure_logging()
    try:
        server = create_server(create_app(), host=host, port=port, threads=4)
    except OSError as exc:
        print(f"Could not start Aur Bataao on {host}:{port}: {exc}", file=sys.stderr)
        return 1

    stopping = False
    stopping_lock = threading.Lock()
    watcher_done = threading.Event()
    cleanup_finished = threading.Event()

    def request_shutdown(reason: str) -> None:
        nonlocal stopping
        with stopping_lock:
            if stopping:
                return
            stopping = True
        print(f"\nStopping Aur Bataao worker ({reason})...")
        server.close()
        server.task_dispatcher.shutdown(cancel_pending=False, timeout=3)
        wasyncore.close_all(server._map)

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        request_shutdown(f"signal {signum}")

    def watch_for_stop_request() -> None:
        while not watcher_done.wait(0.1):
            if stop_path.exists():
                request_shutdown("stop request")
                return

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)

    console_handler = WindowsConsoleCloseHandler(request_shutdown, cleanup_finished)
    console_handler.install()
    parent_watcher = ParentWatcher(supervisor_pid, request_shutdown)
    parent_watcher.start()
    stop_watcher = threading.Thread(
        target=watch_for_stop_request,
        name="aur-bataao-worker-stop-watcher",
        daemon=True,
    )
    stop_watcher.start()

    try:
        ready_path.write_text(f"{token}\n{os.getpid()}\n", encoding="utf-8")
        server.run()
    finally:
        watcher_done.set()
        server.close()
        server.task_dispatcher.shutdown(timeout=3)
        wasyncore.close_all(server._map)
        parent_watcher.close()
        console_handler.close()
        stop_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        cleanup_finished.set()
    return 0


def _supervisor_main(host: str, port: int) -> int:
    token = uuid.uuid4().hex
    worker_stop = RUNTIME_DIR / f"aur-bataao-worker-{token}.stop"
    worker_ready = RUNTIME_DIR / f"aur-bataao-worker-{token}.ready"
    for path in (STOP_FILE, worker_stop, worker_ready):
        path.unlink(missing_ok=True)

    shutdown_requested = threading.Event()
    cleanup_finished = threading.Event()
    shutdown_reason = ["stop request"]

    def request_shutdown(reason: str) -> None:
        shutdown_reason[0] = reason
        shutdown_requested.set()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        request_shutdown(f"signal {signum}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_signal)

    console_handler = WindowsConsoleCloseHandler(request_shutdown, cleanup_finished)
    console_handler.install()
    parent_watcher = ParentWatcher(os.getppid(), request_shutdown)
    parent_watcher.start()
    process: subprocess.Popen[bytes] | None = None
    job: WindowsJob | None = None
    exit_code = 0

    try:
        process, job = _spawn_worker(token, worker_stop, worker_ready)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not worker_ready.exists():
            if process.poll() is not None:
                return process.returncode or 1
            if STOP_FILE.exists():
                request_shutdown("stop request")
            if shutdown_requested.is_set():
                break
            if time.monotonic() >= deadline:
                print("Aur Bataao did not become ready within 20 seconds.", file=sys.stderr)
                exit_code = 1
                request_shutdown("startup timeout")
                break
            time.sleep(0.1)

        if not shutdown_requested.is_set():
            url = _browser_url(host, port)
            print(f"Aur Bataao is running at {url}")
            print("Press Ctrl+C to stop it cleanly.")
            if os.getenv("AUR_BATAAO_OPEN_BROWSER", "1").lower() not in {
                "0",
                "false",
                "no",
            }:
                webbrowser.open(url)

        while process.poll() is None and not shutdown_requested.wait(0.1):
            if STOP_FILE.exists():
                request_shutdown("stop request")

        if shutdown_requested.is_set():
            print(f"\nStopping Aur Bataao ({shutdown_reason[0]})...")
            _stop_worker(process, job, worker_stop)
        elif process.returncode:
            print(
                f"Aur Bataao worker exited unexpectedly with code {process.returncode}.",
                file=sys.stderr,
            )
            exit_code = process.returncode
        return exit_code
    finally:
        if process is not None and process.poll() is None:
            _stop_worker(process, job, worker_stop)
        if job is not None:
            job.close()
        parent_watcher.close()
        console_handler.close()
        for path in (STOP_FILE, worker_stop, worker_ready):
            path.unlink(missing_ok=True)
        cleanup_finished.set()


def main() -> int:
    if sys.argv[1:] == ["--worker"]:
        return _worker_main()
    if sys.argv[1:] == ["--stop"]:
        return request_stop()
    if sys.argv[1:]:
        print("Usage: python start.py [--stop]", file=sys.stderr)
        return 2

    configuration = _read_configuration()
    if configuration is None:
        return 2
    host, port = configuration

    try:
        with InstanceLock(LOCK_FILE):
            _remove_stale_control_files(include_identity=False)
            exit_code = _supervisor_main(host, port)
    except AlreadyRunningError:
        if not confirm_restart():
            return 0
        if request_stop() != 0:
            return 1
        print("Launching a new Aur Bataao instance...")
        return main()

    if exit_code == 0:
        print("Aur Bataao stopped cleanly.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
