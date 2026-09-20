from __future__ import annotations

import logging
import runpy
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import start


class FakeFunction:
    def __init__(self, result=None, side_effect=None):
        self.result = result
        self.side_effect = side_effect
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.side_effect:
            return self.side_effect(*args)
        return self.result


class FakeKernel32:
    def __init__(self, **results):
        names = {
            "OpenProcess",
            "WaitForSingleObject",
            "CloseHandle",
            "SetConsoleCtrlHandler",
            "CreateJobObjectW",
            "SetInformationJobObject",
            "AssignProcessToJobObject",
            "TerminateJobObject",
        }
        for name in names:
            value = results.get(name, True)
            setattr(
                self,
                name,
                value if isinstance(value, FakeFunction) else FakeFunction(value),
            )


class FakeProcess:
    def __init__(self, polls=(), *, returncode=0, pid=4321, wait_results=()):
        self._polls = list(polls)
        self.returncode = returncode
        self.pid = pid
        self._handle = 9876
        self._wait_results = list(wait_results)
        self.killed = False
        self.wait_calls = []

    def poll(self):
        if self._polls:
            value = self._polls.pop(0)
            if value is not None:
                self.returncode = value
            return value
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._wait_results:
            result = self._wait_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            self.returncode = result
        return self.returncode

    def kill(self):
        self.killed = True


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def shutdown(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class FakeServer:
    def __init__(self, run_action=None):
        self.task_dispatcher = FakeDispatcher()
        self._map = {"server": self}
        self.run_action = run_action
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def run(self):
        if self.run_action:
            self.run_action()


class PlatformOS:
    """Override the platform name without mutating the process-wide os module."""

    def __init__(self, backing_os, name):
        self._backing_os = backing_os
        self.name = name

    def __getattr__(self, name):
        return getattr(self._backing_os, name)


def set_platform(monkeypatch, name):
    platform_os = PlatformOS(start.os, name)
    monkeypatch.setattr(start, "os", platform_os)
    return platform_os


@pytest.fixture()
def runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(start, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(start, "LOCK_FILE", tmp_path / "aur-bataao.lock")
    monkeypatch.setattr(start, "PID_FILE", tmp_path / "aur-bataao.pid")
    monkeypatch.setattr(start, "STOP_FILE", tmp_path / "aur-bataao.stop")
    return tmp_path


def test_logging_configuration_adds_and_reuses_queue_handler(monkeypatch):
    root = logging.getLogger()
    queue = logging.getLogger("waitress.queue")
    old_root_handlers = root.handlers[:]
    old_queue_handlers = queue.handlers[:]
    old_propagate = queue.propagate
    try:
        root.handlers.clear()
        queue.handlers.clear()
        start.configure_logging()
        start.configure_logging()

        assert len(root.handlers) == 1
        assert len(queue.handlers) == 1
        assert queue.propagate is False
        assert root.handlers[0].formatter._fmt == start.LOG_FORMAT
    finally:
        root.handlers[:] = old_root_handlers
        queue.handlers[:] = old_queue_handlers
        queue.propagate = old_propagate


def test_logging_configuration_updates_existing_root_handler():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    handler = logging.NullHandler()
    try:
        root.handlers[:] = [handler]
        start.configure_logging()
        assert handler.formatter._fmt == start.LOG_FORMAT
    finally:
        root.handlers[:] = old_handlers


@pytest.mark.parametrize("args", [(), ("bad",), (object(),)])
def test_queue_filter_ignores_unrelated_or_invalid_records(args):
    record = logging.LogRecord("queue", logging.WARNING, "", 0, "other", args, None)
    if args:
        record.msg = "Task queue depth is %d"
    assert start.WaitressQueueLogFilter().filter(record) is True


def test_instance_lock_releases_identity_and_handles_repeated_exit(runtime_paths):
    with start.InstanceLock(start.LOCK_FILE, start.PID_FILE) as lock:
        assert start.PID_FILE.read_text(encoding="utf-8").strip().isdigit()
        assert lock.file is not None
    assert lock.file is None
    assert not start.PID_FILE.exists()
    lock.__exit__()


def test_instance_lock_releases_lock_when_pid_write_fails(runtime_paths, monkeypatch):
    lock = start.InstanceLock(start.LOCK_FILE, start.PID_FILE)
    monkeypatch.setattr(Path, "write_text", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        lock.__enter__()
    assert lock.file is None


def test_instance_lock_uses_posix_flock(tmp_path, monkeypatch):
    lock_path = tmp_path / "posix.lock"
    flock = Mock()
    fake_fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_NB=2,
        LOCK_UN=4,
        flock=flock,
    )
    monkeypatch.setattr(
        start, "os", SimpleNamespace(name="posix", getpid=lambda: 1234)
    )
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)

    with start.InstanceLock(lock_path, pid_path=None):
        pass

    assert flock.call_args_list[0].args[1] == 3
    assert flock.call_args_list[1].args[1] == 4


def test_parent_watcher_posix_detects_parent_change_and_closes(monkeypatch):
    reasons = []
    watcher = start.ParentWatcher(100, reasons.append)
    set_platform(monkeypatch, "posix")
    monkeypatch.setattr(start.os, "getppid", lambda: 200)
    watcher._watch()
    assert reasons == ["launcher parent exited"]

    thread = Mock()
    watcher.thread = thread
    watcher.close()
    thread.join.assert_called_once_with(timeout=0.5)


def test_parent_watcher_dispatches_windows_and_waits_on_stable_posix_parent(monkeypatch):
    watcher = start.ParentWatcher(100, Mock())
    windows_watch = Mock()
    watcher._watch_windows = windows_watch
    set_platform(monkeypatch, "nt")
    watcher._watch()
    windows_watch.assert_called_once_with()

    monkeypatch.setattr(
        start, "os", SimpleNamespace(name="posix", getppid=lambda: 100)
    )
    watcher = start.ParentWatcher(100, Mock())
    watcher.done.wait = Mock(side_effect=[False, True])
    watcher._watch()
    watcher.on_exit.assert_not_called()


def test_parent_watcher_start_and_current_thread_close(monkeypatch):
    created = []

    class Thread:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def start(self):
            created.append("started")

    watcher = start.ParentWatcher(1, lambda _: None)
    monkeypatch.setattr(start.threading, "Thread", Thread)
    watcher.start()
    assert created[-1] == "started"
    monkeypatch.setattr(start.threading, "current_thread", lambda: watcher.thread)
    watcher.close()


@pytest.mark.parametrize(
    ("handle", "error", "wait_results", "expected"),
    [
        (0, 87, [], ["launcher parent exited"]),
        (0, 5, [], []),
        (44, 0, [0], ["launcher parent exited"]),
        (44, 0, [0xFFFFFFFF], []),
    ],
)
def test_parent_watcher_windows_paths(monkeypatch, handle, error, wait_results, expected):
    waits = iter(wait_results)
    kernel = FakeKernel32(
        OpenProcess=handle,
        WaitForSingleObject=FakeFunction(side_effect=lambda *_: next(waits)),
        CloseHandle=True,
    )
    monkeypatch.setattr(start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(start.ctypes, "get_last_error", lambda: error, raising=False)
    watcher = start.ParentWatcher(10, Mock())
    watcher._watch_windows()
    assert watcher.on_exit.call_args_list == [((reason,), {}) for reason in expected]
    assert watcher.process_handle is None


def test_parent_watcher_windows_ignores_timeouts_until_closed(monkeypatch):
    kernel = FakeKernel32(
        OpenProcess=44,
        WaitForSingleObject=258,
        CloseHandle=True,
    )
    monkeypatch.setattr(
        start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False
    )
    watcher = start.ParentWatcher(10, Mock())
    watcher.done.is_set = Mock(side_effect=[False, True])

    watcher._watch_windows()

    watcher.on_exit.assert_not_called()
    assert kernel.WaitForSingleObject.calls == [(44, 250)]
    assert kernel.CloseHandle.calls == [(44,)]


def test_windows_console_handler_non_windows_is_noop(monkeypatch):
    handler = start.WindowsConsoleCloseHandler(Mock(), threading.Event())
    set_platform(monkeypatch, "posix")
    handler.install()
    handler.close()
    assert handler.callback is None


def test_windows_console_handler_installs_dispatches_and_uninstalls(monkeypatch):
    kernel = FakeKernel32(SetConsoleCtrlHandler=True)
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(
        start.ctypes, "WINFUNCTYPE", lambda *_: lambda function: function, raising=False
    )
    cleanup = Mock()
    request = Mock()
    handler = start.WindowsConsoleCloseHandler(request, cleanup)
    handler.install()

    assert handler.callback(1) is False
    assert handler.callback(2) is True
    request.assert_called_once_with("terminal closed")
    cleanup.wait.assert_called_once_with(start.GRACEFUL_STOP_TIMEOUT)
    handler.close()
    assert len(kernel.SetConsoleCtrlHandler.calls) == 2


def test_windows_console_handler_reports_registration_failure(monkeypatch):
    kernel = FakeKernel32(SetConsoleCtrlHandler=False)
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(
        start.ctypes, "WINFUNCTYPE", lambda *_: lambda function: function, raising=False
    )
    monkeypatch.setattr(start.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        start.ctypes,
        "WinError",
        lambda *_: OSError("registration failed"),
        raising=False,
    )
    with pytest.raises(OSError, match="registration failed"):
        start.WindowsConsoleCloseHandler(Mock(), Mock()).install()


def test_windows_job_lifecycle(monkeypatch):
    kernel = FakeKernel32(
        CreateJobObjectW=55,
        SetInformationJobObject=True,
        AssignProcessToJobObject=True,
        TerminateJobObject=True,
        CloseHandle=True,
    )
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    job = start.WindowsJob()
    assert job.handle == 55
    assert job.assign(99) is True
    job.terminate(7)
    job.close()
    job.close()
    assert kernel.TerminateJobObject.calls == [(55, 7)]
    assert job.handle is None


def test_windows_job_no_handle_paths(monkeypatch):
    set_platform(monkeypatch, "posix")
    job = start.WindowsJob()
    assert job.assign(1) is False
    job.terminate()
    job.close()


@pytest.mark.parametrize("create_result,set_result", [(0, True), (77, False)])
def test_windows_job_creation_failures(monkeypatch, create_result, set_result):
    kernel = FakeKernel32(
        CreateJobObjectW=create_result,
        SetInformationJobObject=set_result,
        CloseHandle=True,
    )
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(start.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    monkeypatch.setattr(start.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        start.ctypes, "WinError", lambda *_: OSError("job failed"), raising=False
    )
    with pytest.raises(OSError, match="job failed"):
        start.WindowsJob()
    if create_result:
        assert kernel.CloseHandle.calls == [(create_result,)]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "http://127.0.0.1:8080/"),
        ("::", "http://127.0.0.1:8080/"),
        ("::1", "http://[::1]:8080/"),
        ("localhost", "http://localhost:8080/"),
    ],
)
def test_browser_url(host, expected):
    assert start._browser_url(host, 8080) == expected


@pytest.mark.parametrize(
    ("port", "expected"),
    [("8080", ("example.test", 8080)), ("bad", None), ("0", None), ("65536", None)],
)
def test_read_configuration(monkeypatch, capsys, port, expected):
    monkeypatch.setenv("AUR_BATAAO_HOST", "example.test")
    monkeypatch.setenv("AUR_BATAAO_PORT", port)
    assert start._read_configuration() == expected
    if expected is None:
        assert "AUR_BATAAO_PORT" in capsys.readouterr().err


def test_remove_stale_control_files(runtime_paths):
    start.STOP_FILE.write_text("stop", encoding="utf-8")
    start.PID_FILE.write_text("1", encoding="utf-8")
    (runtime_paths / "aur-bataao-worker-a.ready").write_text("ready", encoding="utf-8")
    (runtime_paths / "aur-bataao-worker-a.stop").write_text("stop", encoding="utf-8")
    start._remove_stale_control_files(include_identity=False)
    assert start.PID_FILE.exists()
    start._remove_stale_control_files(include_identity=True)
    assert list(runtime_paths.glob("aur-bataao*")) == []


def test_instance_running_and_stale_cleanup(runtime_paths, monkeypatch):
    monkeypatch.setattr(start, "InstanceLock", Mock(side_effect=start.AlreadyRunningError))
    assert start.instance_is_running() is True

    lock = Mock()
    lock.return_value.__enter__ = Mock(return_value=None)
    lock.return_value.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(start, "InstanceLock", lock)
    cleanup = Mock()
    monkeypatch.setattr(start, "_remove_stale_control_files", cleanup)
    assert start.instance_is_running() is False
    cleanup.assert_called_once_with(include_identity=True)


def test_request_stop_outcomes(runtime_paths, monkeypatch, capsys):
    monkeypatch.setattr(start, "instance_is_running", Mock(return_value=False))
    assert start.request_stop() == 0
    assert "not running" in capsys.readouterr().out

    monkeypatch.setattr(start, "instance_is_running", Mock(side_effect=[True, False, False]))
    monkeypatch.setattr(start.time, "sleep", Mock())
    assert start.request_stop() == 0
    assert start.STOP_FILE.exists()
    assert "stopped cleanly" in capsys.readouterr().out

    monkeypatch.setattr(start, "instance_is_running", Mock(return_value=True))
    monkeypatch.setattr(start.time, "monotonic", Mock(side_effect=[0, 11, 11]))
    assert start.request_stop() == 1
    assert "did not stop" in capsys.readouterr().err

    running = Mock(side_effect=[True, True, False, False])
    sleep = Mock()
    monkeypatch.setattr(start, "instance_is_running", running)
    monkeypatch.setattr(start.time, "monotonic", Mock(side_effect=[0, 1]))
    monkeypatch.setattr(start.time, "sleep", sleep)
    assert start.request_stop() == 0
    sleep.assert_called_once_with(0.1)


def test_confirm_restart_handles_interrupted_input(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
    assert start.confirm_restart() is False
    assert "Launch skipped" in capsys.readouterr().out


def test_resume_windows_process_success_and_failure(monkeypatch):
    resume = FakeFunction(0)
    monkeypatch.setattr(
        start.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: SimpleNamespace(NtResumeProcess=resume),
        raising=False,
    )
    start._resume_windows_process(10)
    resume.result = -1
    with pytest.raises(OSError, match="NtResumeProcess failed"):
        start._resume_windows_process(10)


def test_spawn_worker_posix(monkeypatch, tmp_path):
    process = FakeProcess()
    popen = Mock(return_value=process)
    fake_os = SimpleNamespace(
        name="posix", environ=start.os.environ, getpid=start.os.getpid
    )
    monkeypatch.setattr(start, "os", fake_os)
    monkeypatch.setattr(start.subprocess, "Popen", popen)
    result, job = start._spawn_worker("token", tmp_path / "stop", tmp_path / "ready")
    assert result is process and job is None
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"]["AUR_BATAAO_INTERNAL_WORKER"] == "token"


def test_spawn_worker_windows_success_warning_and_cleanup(monkeypatch, tmp_path, capsys):
    process = FakeProcess()
    job = Mock(assigned=False)
    job.assign.return_value = False
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(
        start.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(start, "WindowsJob", Mock(return_value=job))
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(start, "_resume_windows_process", Mock())
    monkeypatch.setattr(start.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        start.ctypes, "WinError", lambda *_: OSError("assign failed"), raising=False
    )

    result, returned_job = start._spawn_worker("token", tmp_path / "stop", tmp_path / "ready")
    assert result is process and returned_job is job
    assert "assignment failed" in capsys.readouterr().err

    monkeypatch.setattr(start, "_resume_windows_process", Mock(side_effect=OSError("resume")))
    with pytest.raises(OSError, match="resume"):
        start._spawn_worker("token", tmp_path / "stop", tmp_path / "ready")
    assert process.killed is True
    job.close.assert_called_once()


def test_spawn_worker_windows_without_job_containment(monkeypatch, tmp_path, capsys):
    process = FakeProcess()
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(
        start.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(start, "WindowsJob", Mock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(start, "_resume_windows_process", Mock())
    _, job = start._spawn_worker("token", tmp_path / "stop", tmp_path / "ready")
    assert job is None
    assert "containment is unavailable" in capsys.readouterr().err


def test_spawn_worker_failure_without_job_still_reaps_process(
    monkeypatch, tmp_path
):
    process = FakeProcess()
    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(
        start.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False
    )
    monkeypatch.setattr(start, "WindowsJob", Mock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(
        start, "_resume_windows_process", Mock(side_effect=OSError("resume failed"))
    )

    with pytest.raises(OSError, match="resume failed"):
        start._spawn_worker("token", tmp_path / "stop", tmp_path / "ready")

    assert process.killed is True
    assert process.wait_calls == [5]


def test_force_stop_worker_uses_job_taskkill_or_process_group(monkeypatch):
    process = FakeProcess()
    job = Mock(assigned=True)
    start._force_stop_worker(process, job)
    job.terminate.assert_called_once()

    set_platform(monkeypatch, "nt")
    monkeypatch.setattr(start.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    run = Mock()
    monkeypatch.setattr(start.subprocess, "run", run)
    start._force_stop_worker(process, None)
    assert run.call_args.args[0][:2] == ["taskkill", "/PID"]

    fake_os = SimpleNamespace(name="posix", killpg=Mock(side_effect=[None, ProcessLookupError]))
    monkeypatch.setattr(start, "os", fake_os)
    monkeypatch.setattr(start.signal, "SIGKILL", 9, raising=False)
    start._force_stop_worker(process, None)
    start._force_stop_worker(process, None)


def test_stop_worker_paths(monkeypatch, tmp_path, capsys):
    stop_path = tmp_path / "stop"
    exited = FakeProcess(polls=[0])
    start._stop_worker(exited, None, stop_path)
    assert not stop_path.exists()

    graceful = FakeProcess(polls=[None], wait_results=[0])
    start._stop_worker(graceful, None, stop_path)
    assert stop_path.exists()

    timeout = subprocess.TimeoutExpired("worker", 1)
    stubborn = FakeProcess(polls=[None], wait_results=[timeout, timeout, 0])
    force = Mock()
    monkeypatch.setattr(start, "_force_stop_worker", force)
    start._stop_worker(stubborn, None, stop_path)
    force.assert_called_once_with(stubborn, None)
    assert stubborn.killed is True
    assert "terminating" in capsys.readouterr().err


def _worker_environment(monkeypatch, tmp_path):
    stop_path = tmp_path / "worker.stop"
    ready_path = tmp_path / "worker.ready"
    monkeypatch.setenv("AUR_BATAAO_INTERNAL_WORKER", "token")
    monkeypatch.setenv("AUR_BATAAO_WORKER_STOP", str(stop_path))
    monkeypatch.setenv("AUR_BATAAO_WORKER_READY", str(ready_path))
    monkeypatch.setenv("AUR_BATAAO_SUPERVISOR_PID", "123")
    monkeypatch.setattr(start, "_read_configuration", lambda: ("127.0.0.1", 8080))
    monkeypatch.setattr(start, "configure_logging", Mock())
    monkeypatch.setattr(start, "create_app", Mock(return_value=object()))
    monkeypatch.setattr(start.signal, "signal", Mock())
    monkeypatch.setattr(start, "WindowsConsoleCloseHandler", Mock())
    monkeypatch.setattr(start, "ParentWatcher", Mock())
    monkeypatch.setattr(start.threading, "Thread", Mock())
    monkeypatch.setattr(start.wasynccore if hasattr(start, "wasynccore") else start.wasyncore, "close_all", Mock())
    return stop_path, ready_path


def test_worker_main_rejects_invalid_configuration(monkeypatch, tmp_path, capsys):
    for name in (
        "AUR_BATAAO_INTERNAL_WORKER",
        "AUR_BATAAO_WORKER_STOP",
        "AUR_BATAAO_WORKER_READY",
        "AUR_BATAAO_SUPERVISOR_PID",
    ):
        monkeypatch.delenv(name, raising=False)
    assert start._worker_main() == 2
    assert "must be started" in capsys.readouterr().err

    _worker_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(start, "_read_configuration", lambda: None)
    assert start._worker_main() == 2
    monkeypatch.setattr(start, "_read_configuration", lambda: ("127.0.0.1", 8080))
    monkeypatch.setenv("AUR_BATAAO_SUPERVISOR_PID", "bad")
    assert start._worker_main() == 2


def test_worker_main_reports_bind_failure(monkeypatch, tmp_path, capsys):
    _worker_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(start, "create_server", Mock(side_effect=OSError("in use")))
    assert start._worker_main() == 1
    assert "Could not start" in capsys.readouterr().err


def test_worker_main_runs_and_cleans_up(monkeypatch, tmp_path):
    stop_path, ready_path = _worker_environment(monkeypatch, tmp_path)
    signal_handlers = {}
    monkeypatch.setattr(start.signal, "signal", lambda signum, callback: signal_handlers.setdefault(signum, callback))
    server = FakeServer(lambda: (signal_handlers[signal.SIGINT](signal.SIGINT, None), signal_handlers[signal.SIGINT](signal.SIGINT, None)))
    monkeypatch.setattr(start, "create_server", Mock(return_value=server))

    assert start._worker_main() == 0
    assert not stop_path.exists()
    assert not ready_path.exists()
    assert server.close_calls >= 2
    assert len(server.task_dispatcher.calls) >= 2
    start.wasyncore.close_all.assert_called()


def test_worker_stop_watcher_runs_without_sigbreak(monkeypatch, tmp_path, capsys):
    stop_path, ready_path = _worker_environment(monkeypatch, tmp_path)
    stop_path.write_text("stop", encoding="utf-8")
    fake_signal = SimpleNamespace(SIGINT=2, SIGTERM=15, signal=Mock())
    monkeypatch.setattr(start, "signal", fake_signal)

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(start.threading, "Thread", ImmediateThread)
    server = FakeServer()
    monkeypatch.setattr(start, "create_server", Mock(return_value=server))

    assert start._worker_main() == 0
    assert "stop request" in capsys.readouterr().out
    assert not stop_path.exists()
    assert not ready_path.exists()


def test_worker_stop_watcher_waits_until_cleanup(monkeypatch, tmp_path):
    real_thread = threading.Thread
    _stop_path, _ready_path = _worker_environment(monkeypatch, tmp_path)
    threads = []

    def create_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(start.threading, "Thread", create_thread)
    server = FakeServer(lambda: time.sleep(0.15))
    monkeypatch.setattr(start, "create_server", Mock(return_value=server))

    assert start._worker_main() == 0
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()


def _supervisor_fakes(monkeypatch, runtime_paths, process):
    monkeypatch.setattr(start, "_spawn_worker", Mock(return_value=(process, None)))
    monkeypatch.setattr(start, "_stop_worker", Mock())
    monkeypatch.setattr(start, "WindowsConsoleCloseHandler", Mock())
    monkeypatch.setattr(start, "ParentWatcher", Mock())
    monkeypatch.setattr(start.signal, "signal", Mock())
    monkeypatch.setattr(start.webbrowser, "open", Mock())
    monkeypatch.setattr(start.time, "sleep", Mock())
    return runtime_paths


def test_supervisor_success_and_unexpected_exit(monkeypatch, runtime_paths, capsys):
    process = FakeProcess(polls=[None, 0, 0], returncode=0)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    original_spawn = start._spawn_worker

    def spawn(*args):
        ready_path = args[2]
        ready_path.write_text("ready", encoding="utf-8")
        return original_spawn(*args)

    monkeypatch.setattr(start, "_spawn_worker", spawn)
    assert start._supervisor_main("0.0.0.0", 8080) == 0
    assert "running at" in capsys.readouterr().out
    start.webbrowser.open.assert_called_once()

    process = FakeProcess(polls=[None, 4, 4], returncode=4)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    original_spawn = start._spawn_worker

    def spawn_crashing(*args):
        args[2].write_text("ready", encoding="utf-8")
        return original_spawn(*args)

    monkeypatch.setattr(start, "_spawn_worker", spawn_crashing)
    assert start._supervisor_main("127.0.0.1", 8080) == 4
    assert "unexpectedly" in capsys.readouterr().err


def test_supervisor_handles_early_exit_stop_and_timeout(monkeypatch, runtime_paths, capsys):
    process = FakeProcess(polls=[3, 3], returncode=3)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    assert start._supervisor_main("127.0.0.1", 8080) == 3

    process = FakeProcess(polls=[None, None], returncode=0)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    original_spawn = start._spawn_worker

    def spawn_stopping(*args):
        start.STOP_FILE.write_text("stop", encoding="utf-8")
        return original_spawn(*args)

    monkeypatch.setattr(start, "_spawn_worker", spawn_stopping)
    assert start._supervisor_main("127.0.0.1", 8080) == 0
    start._stop_worker.assert_called()
    assert "stop request" in capsys.readouterr().out

    process = FakeProcess(polls=[None, None], returncode=0)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    monkeypatch.setattr(start.time, "monotonic", Mock(side_effect=[0, start.STARTUP_TIMEOUT + 1]))
    assert start._supervisor_main("127.0.0.1", 8080) == 1
    assert "did not become ready" in capsys.readouterr().err


def test_supervisor_signal_handler_without_sigbreak(monkeypatch, runtime_paths, capsys):
    process = FakeProcess(polls=[None, None, 0], returncode=0)
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    handlers = {}
    fake_signal = SimpleNamespace(
        SIGINT=2,
        SIGTERM=15,
        signal=lambda signum, callback: handlers.setdefault(signum, callback),
    )
    monkeypatch.setattr(start, "signal", fake_signal)
    original_spawn = start._spawn_worker

    def spawn_and_signal(*args):
        result = original_spawn(*args)
        handlers[fake_signal.SIGTERM](fake_signal.SIGTERM, None)
        return result

    monkeypatch.setattr(start, "_spawn_worker", spawn_and_signal)

    assert start._supervisor_main("127.0.0.1", 8080) == 0
    assert "signal 15" in capsys.readouterr().out
    start._stop_worker.assert_called()


def test_supervisor_waits_for_ready_skips_browser_and_honors_stop_file(
    monkeypatch, runtime_paths
):
    class StopFileProcess(FakeProcess):
        def __init__(self):
            super().__init__(returncode=0)
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.poll_count == 2:
                start.STOP_FILE.write_text("stop", encoding="utf-8")
            if self.poll_count >= 4:
                self.returncode = 0
                return 0
            return None

    process = StopFileProcess()
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    ready_path = None
    original_spawn = start._spawn_worker

    def capture_ready(*args):
        nonlocal ready_path
        ready_path = args[2]
        return original_spawn(*args)

    def become_ready(_delay):
        ready_path.write_text("ready", encoding="utf-8")

    monkeypatch.setattr(start, "_spawn_worker", capture_ready)
    monkeypatch.setattr(start.time, "monotonic", Mock(side_effect=[0, 1]))
    monkeypatch.setattr(start.time, "sleep", become_ready)
    monkeypatch.setenv("AUR_BATAAO_OPEN_BROWSER", "0")

    assert start._supervisor_main("127.0.0.1", 8080) == 0
    start.webbrowser.open.assert_not_called()
    start._stop_worker.assert_called()


def test_supervisor_cleans_live_worker_and_job_after_startup_exception(
    monkeypatch, runtime_paths
):
    process = FakeProcess(polls=[None], returncode=0)
    job = Mock()
    _supervisor_fakes(monkeypatch, runtime_paths, process)
    monkeypatch.setattr(start, "_spawn_worker", Mock(return_value=(process, job)))
    monkeypatch.setattr(
        start.time, "monotonic", Mock(side_effect=RuntimeError("clock failed"))
    )

    with pytest.raises(RuntimeError, match="clock failed"):
        start._supervisor_main("127.0.0.1", 8080)

    start._stop_worker.assert_called_once_with(
        process,
        job,
        start._stop_worker.call_args.args[2],
    )
    job.close.assert_called_once_with()


def test_main_dispatch_and_restart_paths(monkeypatch, capsys):
    monkeypatch.setattr(start.sys, "argv", ["start.py", "--worker"])
    monkeypatch.setattr(start, "_worker_main", Mock(return_value=7))
    assert start.main() == 7

    monkeypatch.setattr(start.sys, "argv", ["start.py", "--stop"])
    monkeypatch.setattr(start, "request_stop", Mock(return_value=6))
    assert start.main() == 6

    monkeypatch.setattr(start.sys, "argv", ["start.py", "bad"])
    assert start.main() == 2
    assert "Usage" in capsys.readouterr().err

    monkeypatch.setattr(start.sys, "argv", ["start.py"])
    monkeypatch.setattr(start, "_read_configuration", Mock(return_value=None))
    assert start.main() == 2


def test_main_normal_and_existing_instance_paths(monkeypatch, capsys):
    monkeypatch.setattr(start.sys, "argv", ["start.py"])
    monkeypatch.setattr(start, "_read_configuration", Mock(return_value=("host", 80)))
    lock = Mock()
    lock.return_value.__enter__ = Mock(return_value=None)
    lock.return_value.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(start, "InstanceLock", lock)
    monkeypatch.setattr(start, "_remove_stale_control_files", Mock())
    monkeypatch.setattr(start, "_supervisor_main", Mock(return_value=0))
    assert start.main() == 0
    assert "stopped cleanly" in capsys.readouterr().out

    monkeypatch.setattr(start, "InstanceLock", Mock(side_effect=start.AlreadyRunningError))
    monkeypatch.setattr(start, "confirm_restart", Mock(return_value=False))
    assert start.main() == 0

    monkeypatch.setattr(start, "confirm_restart", Mock(return_value=True))
    monkeypatch.setattr(start, "request_stop", Mock(return_value=1))
    assert start.main() == 1


def test_main_restarts_after_stopping_existing_instance(monkeypatch, capsys):
    monkeypatch.setattr(start.sys, "argv", ["start.py"])
    monkeypatch.setattr(start, "_read_configuration", Mock(return_value=("host", 80)))
    good_lock = Mock()
    good_lock.__enter__ = Mock(return_value=None)
    good_lock.__exit__ = Mock(return_value=None)
    monkeypatch.setattr(
        start,
        "InstanceLock",
        Mock(side_effect=[start.AlreadyRunningError, good_lock]),
    )
    monkeypatch.setattr(start, "confirm_restart", Mock(return_value=True))
    monkeypatch.setattr(start, "request_stop", Mock(return_value=0))
    monkeypatch.setattr(start, "_remove_stale_control_files", Mock())
    monkeypatch.setattr(start, "_supervisor_main", Mock(return_value=2))
    assert start.main() == 2
    assert "Launching a new" in capsys.readouterr().out


def test_script_entrypoint_dispatches_main(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["start.py", "unsupported"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(start.__file__, run_name="__main__")
    assert exc.value.code == 2
