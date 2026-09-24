from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SandboxCapabilities:
    """Capabilities actually enforced by the selected OS backend."""

    backend: str
    hard_memory_limit: bool
    hard_cpu_limit: bool
    hard_process_count_limit: bool
    process_tree_termination: bool
    network_isolation: bool
    filesystem_isolation: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "hard_memory_limit": self.hard_memory_limit,
            "hard_cpu_limit": self.hard_cpu_limit,
            "hard_process_count_limit": self.hard_process_count_limit,
            "process_tree_termination": self.process_tree_termination,
            "network_isolation": self.network_isolation,
            "filesystem_isolation": self.filesystem_isolation,
        }


@dataclass(frozen=True)
class ProcessSandboxConfig:
    """Hard resource limits for child processes.

    The Windows backend uses a Job Object. On Linux/POSIX, the backend applies
    resource limits to the child with ``resource.prlimit`` when available.

    Network/filesystem isolation is deliberately represented as a required
    capability instead of being faked with environment variables. The built-in
    backends currently report those capabilities as unavailable; callers can
    choose compatibility mode (the default) or fail closed by setting either
    ``require_*_isolation`` flag.
    """

    memory_limit_mb: int = 1024
    cpu_time_seconds: int = 30
    max_processes: int = 16
    require_network_isolation: bool = False
    require_filesystem_isolation: bool = False

    def __post_init__(self) -> None:
        if self.memory_limit_mb < 64:
            raise ValueError("memory_limit_mb must be at least 64")
        if self.cpu_time_seconds < 1:
            raise ValueError("cpu_time_seconds must be positive")
        if self.max_processes < 1:
            raise ValueError("max_processes must be positive")


class ProcessSandboxHandle(Protocol):
    def terminate(self) -> None: ...

    def close(self) -> None: ...


class ProcessSandbox:
    """Attach OS-level resource controls to an already-started process.

    The attachment happens immediately after ``Popen``. This substantially
    narrows resource abuse, but it is not race-free in the same way that a
    container/cgroup/AppContainer launch would be. The later container backend
    can implement the same small interface without changing tool runtimes.
    """

    def __init__(self, config: ProcessSandboxConfig | None = None) -> None:
        self.config = config or ProcessSandboxConfig()
        if os.name == "nt":
            self._backend: _Backend = _WindowsJobBackend(self.config)
        else:
            self._backend = _PosixRlimitBackend(self.config)

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._backend.capabilities

    def validate_requirements(self) -> None:
        caps = self.capabilities
        if self.config.require_network_isolation and not caps.network_isolation:
            raise RuntimeError(
                "network isolation is required but unavailable in the current "
                f"sandbox backend ({caps.backend})"
            )
        if self.config.require_filesystem_isolation and not caps.filesystem_isolation:
            raise RuntimeError(
                "filesystem isolation is required but unavailable in the current "
                f"sandbox backend ({caps.backend})"
            )

    def attach(self, process: subprocess.Popen) -> ProcessSandboxHandle:
        self.validate_requirements()
        return self._backend.attach(process)


class _Backend(Protocol):
    @property
    def capabilities(self) -> SandboxCapabilities: ...

    def attach(self, process: subprocess.Popen) -> ProcessSandboxHandle: ...


class _PosixHandle:
    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process

    def terminate(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

    def close(self) -> None:
        return None


class _PosixRlimitBackend:
    def __init__(self, config: ProcessSandboxConfig) -> None:
        self.config = config
        try:
            import resource  # type: ignore
        except ImportError:
            self._resource = None
        else:
            self._resource = resource

    @property
    def capabilities(self) -> SandboxCapabilities:
        resource = self._resource
        hard_memory = bool(resource is not None and hasattr(resource, "prlimit") and hasattr(resource, "RLIMIT_AS"))
        hard_cpu = bool(resource is not None and hasattr(resource, "prlimit") and hasattr(resource, "RLIMIT_CPU"))
        return SandboxCapabilities(
            backend="posix-prlimit" if resource is not None and hasattr(resource, "prlimit") else "posix-basic",
            hard_memory_limit=hard_memory,
            hard_cpu_limit=hard_cpu,
            hard_process_count_limit=False,
            process_tree_termination=True,
            network_isolation=False,
            filesystem_isolation=False,
        )

    def attach(self, process: subprocess.Popen) -> ProcessSandboxHandle:
        resource = self._resource
        if resource is not None and hasattr(resource, "prlimit"):
            if hasattr(resource, "RLIMIT_AS"):
                memory_bytes = self.config.memory_limit_mb * 1024 * 1024
                resource.prlimit(
                    process.pid,
                    resource.RLIMIT_AS,
                    (memory_bytes, memory_bytes),
                )
            if hasattr(resource, "RLIMIT_CPU"):
                cpu = int(self.config.cpu_time_seconds)
                resource.prlimit(
                    process.pid,
                    resource.RLIMIT_CPU,
                    (cpu, cpu + 1),
                )
            if hasattr(resource, "RLIMIT_CORE"):
                try:
                    resource.prlimit(process.pid, resource.RLIMIT_CORE, (0, 0))
                except (OSError, ValueError):
                    pass
            if hasattr(resource, "RLIMIT_FSIZE"):
                try:
                    resource.prlimit(
                        process.pid,
                        resource.RLIMIT_FSIZE,
                        (64 * 1024 * 1024, 64 * 1024 * 1024),
                    )
                except (OSError, ValueError):
                    pass
        return _PosixHandle(process)


class _WindowsJobHandle:
    def __init__(self, kernel32, job_handle) -> None:
        self._kernel32 = kernel32
        self._job_handle = job_handle
        self._closed = False

    def terminate(self) -> None:
        if self._closed:
            return
        try:
            self._kernel32.TerminateJobObject(self._job_handle, 1)
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._kernel32.CloseHandle(self._job_handle)
        except Exception:
            pass


class _WindowsJobBackend:
    JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    def __init__(self, config: ProcessSandboxConfig) -> None:
        self.config = config

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend="windows-job-object",
            hard_memory_limit=True,
            hard_cpu_limit=True,
            hard_process_count_limit=True,
            process_tree_termination=True,
            network_isolation=False,
            filesystem_isolation=False,
        )

    def attach(self, process: subprocess.Popen) -> ProcessSandboxHandle:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
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
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(
            self.config.cpu_time_seconds * 10_000_000
        )
        info.BasicLimitInformation.ActiveProcessLimit = int(self.config.max_processes)
        info.ProcessMemoryLimit = self.config.memory_limit_mb * 1024 * 1024
        info.JobMemoryLimit = self.config.memory_limit_mb * 1024 * 1024
        info.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_PROCESS_TIME
            | self.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | self.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | self.JOB_OBJECT_LIMIT_JOB_MEMORY
            | self.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )

        if not kernel32.SetInformationJobObject(
            job,
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "SetInformationJobObject failed")

        process_handle = kernel32.OpenProcess(
            self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE,
            False,
            int(process.pid),
        )
        if not process_handle:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(job)
                raise OSError(error, "AssignProcessToJobObject failed")
        finally:
            kernel32.CloseHandle(process_handle)

        return _WindowsJobHandle(kernel32, job)
