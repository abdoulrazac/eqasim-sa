"""Supervise a launched process together with every descendant it starts.

POSIX places the child in a new session and signals its whole process group.
Windows places the child in a Job Object before it executes a single
instruction: the child is created suspended, assigned to the job, then
resumed, so no descendant can start outside the job.  Closing the job handle
kills whatever is left (``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``).

The platform is read from ``eqasim_sa.utils.platform.IS_WINDOWS`` when a tree
is started.  The Windows API is loaded lazily, so this module imports (but
never calls ``kernel32``/``ntdll``) on POSIX.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence

from eqasim_sa.utils import platform

__all__ = ("CREATE_NEW_PROCESS_GROUP", "CREATE_SUSPENDED", "ProcessTree")

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
POLL_SECONDS = 0.02


class ProcessTree:
    """One launched process and all of its descendants, stopped as a unit."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._job: int | None = None
        self._api: _WindowsApi | None = None
        self._empty = False
        self._closed = False

    def start(self, command: Sequence[str], **popen_options) -> subprocess.Popen:  # type: ignore[no-untyped-def]
        with self._lock:
            if self._process is not None or self._closed:
                raise RuntimeError("process tree already started")
            if platform.IS_WINDOWS:
                self._process = self._start_windows(command, popen_options)
            else:
                self._process = subprocess.Popen(
                    command, start_new_session=True, **popen_options
                )
            return self._process

    def terminate(self, grace_seconds: float) -> None:
        """Stop every process of the tree; return only once the tree is empty.

        POSIX sends ``SIGTERM`` to the group, waits up to ``grace_seconds``,
        then sends ``SIGKILL``.  Windows terminates the whole job at once: a
        console interrupt cannot ask a JVM to stop cleanly.
        """
        with self._lock:
            if self.is_empty():
                return
            if self._api is None:
                self._signal_group(signal.SIGTERM)
                deadline = time.monotonic() + max(0.0, grace_seconds)
                while time.monotonic() < deadline:
                    if self.is_empty():
                        return
                    time.sleep(POLL_SECONDS)
                self._signal_group(signal.SIGKILL)
            else:
                assert self._job is not None
                self._api.terminate(self._job)
            while not self.is_empty():
                time.sleep(POLL_SECONDS)

    def is_empty(self) -> bool:
        """True once no process of the tree is alive (or none was started)."""
        with self._lock:
            if self._empty or self._process is None:
                return True
            self._process.poll()
            if self._api is None:
                # A reaped group cannot regain members, so emptiness is final.
                try:
                    os.killpg(self._process.pid, 0)
                except ProcessLookupError:
                    self._empty = True
            else:
                if self._job is None:
                    raise RuntimeError(
                        "process tree state is unknown: its job handle was "
                        "released after a failed termination"
                    )
                self._empty = self._api.active_processes(self._job) == 0
            return self._empty

    def close(self) -> None:
        """Stop anything still running and release the job handle."""
        with self._lock:
            if self._closed:
                return
            try:
                self.terminate(0.0)
            finally:
                self._closed = True
                self._release_job()

    def _signal_group(self, signum: int) -> None:
        assert self._process is not None
        try:
            os.killpg(self._process.pid, signum)
        except ProcessLookupError:
            self._empty = True

    def _start_windows(
        self, command: Sequence[str], popen_options: dict
    ) -> subprocess.Popen:
        api = _windows_api()
        job = api.create_job()
        flags = popen_options.pop("creationflags", 0)
        try:
            process = subprocess.Popen(
                command,
                creationflags=flags | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP,
                **popen_options,
            )
        except BaseException:
            api.close(job)
            raise
        try:
            api.assign(job, int(process._handle))  # type: ignore[attr-defined]
        except BaseException:
            process.kill()
            process.wait()
            api.close(job)
            raise
        self._api, self._job = api, job
        try:
            api.resume(int(process._handle))  # type: ignore[attr-defined]
        except BaseException:
            # The child never ran, so it has no descendants: once it is
            # reaped the tree is empty, and the job handle is released even
            # when TerminateJobObject itself fails.
            try:
                try:
                    api.terminate(job)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            finally:
                self._empty = True
                self._release_job()
            raise
        return process

    def _release_job(self) -> None:
        if self._api is not None and self._job is not None:
            job, self._job = self._job, None
            self._api.close(job)


_WINDOWS_API: _WindowsApi | None = None


def _windows_api() -> _WindowsApi:
    global _WINDOWS_API
    if _WINDOWS_API is None:
        _WINDOWS_API = _WindowsApi()
    return _WINDOWS_API


_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TERMINATED_EXIT_CODE = 1


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _WindowsApi:
    """Thin ``ctypes`` binding of the Job Object calls (Windows only)."""

    def __init__(self) -> None:
        handle, boolean, dword = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        self._create = kernel32.CreateJobObjectW
        self._create.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        self._create.restype = handle
        self._set = kernel32.SetInformationJobObject
        self._set.argtypes = (handle, ctypes.c_int, ctypes.c_void_p, dword)
        self._set.restype = boolean
        self._query = kernel32.QueryInformationJobObject
        self._query.argtypes = (
            handle,
            ctypes.c_int,
            ctypes.c_void_p,
            dword,
            ctypes.POINTER(dword),
        )
        self._query.restype = boolean
        self._assign = kernel32.AssignProcessToJobObject
        self._assign.argtypes = (handle, handle)
        self._assign.restype = boolean
        self._terminate = kernel32.TerminateJobObject
        self._terminate.argtypes = (handle, ctypes.c_uint)
        self._terminate.restype = boolean
        self._close = kernel32.CloseHandle
        self._close.argtypes = (handle,)
        self._close.restype = boolean
        self._resume = ntdll.NtResumeProcess
        self._resume.argtypes = (handle,)
        self._resume.restype = ctypes.c_long

    @staticmethod
    def _check(succeeded: object) -> None:
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]

    def create_job(self) -> int:
        job = self._create(None, None)
        self._check(job)
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self._check(
                self._set(
                    job,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
            )
        except BaseException:
            self._close(job)
            raise
        return job

    def assign(self, job: int, process: int) -> None:
        self._check(self._assign(job, process))

    def resume(self, process: int) -> None:
        status = self._resume(process)
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS {status & 0xFFFFFFFF:#010x}")

    def terminate(self, job: int) -> None:
        self._check(self._terminate(job, _TERMINATED_EXIT_CODE))

    def active_processes(self, job: int) -> int:
        information = _BasicAccountingInformation()
        self._check(
            self._query(
                job,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
                None,
            )
        )
        return int(information.ActiveProcesses)

    def close(self, job: int) -> None:
        self._check(self._close(job))
