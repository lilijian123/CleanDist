"""Owned Job Objects. Closing them never terminates managed processes."""
import ctypes
from ctypes import wintypes as w
import os

from ...core.processes import matching_process


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_size = ctypes.c_size_t


class _Basic(ctypes.Structure):
    _fields_ = [('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
                ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', _size),
                ('MaximumWorkingSetSize', _size), ('ActiveProcessLimit', w.DWORD),
                ('Affinity', _size), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]


class _Extended(ctypes.Structure):
    _fields_ = [('Basic', _Basic), ('IoInfo', ctypes.c_ulonglong * 6),
                ('ProcessMemoryLimit', _size), ('JobMemoryLimit', _size),
                ('PeakProcessMemoryUsed', _size), ('PeakJobMemoryUsed', _size)]


class _Cpu(ctypes.Structure):
    _fields_ = [('ControlFlags', w.DWORD), ('CpuRate', w.DWORD)]


_create = _kernel.CreateJobObjectW
_create.argtypes, _create.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
_open = _kernel.OpenProcess
_open.argtypes, _open.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
_assign = _kernel.AssignProcessToJobObject
_assign.argtypes, _assign.restype = [w.HANDLE, w.HANDLE], w.BOOL
_set = _kernel.SetInformationJobObject
_set.argtypes, _set.restype = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL
_query = _kernel.QueryInformationJobObject
_query.argtypes, _query.restype = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL
_close = _kernel.CloseHandle
_close.argtypes, _close.restype = [w.HANDLE], w.BOOL
_wait = _kernel.WaitForSingleObject
_wait.argtypes, _wait.restype = [w.HANDLE, w.DWORD], w.DWORD


def _check(ok):
    if not ok: raise ctypes.WinError(ctypes.get_last_error())


class Controller:
    def __init__(self):
        self.jobs = {}

    def capabilities(self):
        return {'platform': 'Windows', 'cpu': True, 'memory': True, 'memory_soft': False, 'io': False,
                'reason': 'Job 的 I/O 限速接口在 Windows 10 1607+ 不受支持；附加已有不兼容 Job 的进程可能失败',
                'scope': '所选进程；以后创建的子进程通常继承 Job，CPU 限制作用于整个 Job；已有子进程不加入',
                'memory_label': '选中进程私有提交量（Job 提交内存上限，不等同 RSS）',
                'io_label': '进程 I/O 计数，包含非物理磁盘 I/O；不支持限速',
                'clear_note': '清除本应用 Job 限制；运行中进程仍保留 Job 成员身份，上级 Job 限制仍有效，关闭句柄不结束进程'}

    def apply(self, key, *, cpu_cores, memory_bytes, hard_memory, device, read_bps, write_bps):
        if device is not None or read_bps is not None or write_bps is not None:
            raise ValueError('当前 Windows 不支持 Job I/O 限速')
        if memory_bytes is not None and not hard_memory:
            raise ValueError('Windows 仅支持提交内存硬上限；请明确选择可能导致分配失败的硬限制')
        rate = round(cpu_cores / (os.cpu_count() or 1) * 10000) if cpu_cores is not None else 0
        if cpu_cores is not None and not 1 <= rate <= 10000:
            raise ValueError('CPU 限制低于 Windows Job 最小精度，或超过整机容量')
        fresh = key not in self.jobs
        if fresh:
            handle = _open(0x100000 | 0x1000 | 0x100 | 0x1, False, key[0])
            if not handle: raise ctypes.WinError(ctypes.get_last_error())
            job = _create(None, None)
            if not job:
                _close(handle)
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            job, handle = self.jobs[key]
        try:
            matching_process(key)
            if _wait(handle, 0) != 258: raise ValueError('进程已退出，未应用限制')
            old_cpu, old_memory = _Cpu(), _Extended()
            _check(_query(job, 15, ctypes.byref(old_cpu), ctypes.sizeof(old_cpu), None))
            _check(_query(job, 9, ctypes.byref(old_memory), ctypes.sizeof(old_memory), None))
            cpu = _Cpu(5 if rate else 0, rate)
            memory = _Extended()
            if memory_bytes is not None:
                memory.Basic.LimitFlags = 0x100  # PROCESS_MEMORY; never KILL_ON_JOB_CLOSE.
                memory.ProcessMemoryLimit = memory_bytes
            try:
                _check(_set(job, 15, ctypes.byref(cpu), ctypes.sizeof(cpu)))
                _check(_set(job, 9, ctypes.byref(memory), ctypes.sizeof(memory)))
                if fresh: _check(_assign(job, handle))
            except OSError:
                _check(_set(job, 15, ctypes.byref(old_cpu), ctypes.sizeof(old_cpu)))
                _check(_set(job, 9, ctypes.byref(old_memory), ctypes.sizeof(old_memory)))
                raise
        except (OSError, ValueError):
            if fresh:
                _close(job)
                _close(handle)
            raise
        self.jobs[key] = job, handle
        return {'scope': self.capabilities()['scope'], 'cpu_rate': rate,
                'memory_metric': 'private_commit', 'memory_mode': 'per_process_commit_hard',
                'memory_bytes': memory_bytes}

    def inspect(self, key):
        job, _ = self.jobs[key]
        cpu, memory = _Cpu(), _Extended()
        _check(_query(job, 15, ctypes.byref(cpu), ctypes.sizeof(cpu), None))
        _check(_query(job, 9, ctypes.byref(memory), ctypes.sizeof(memory), None))
        return {'cpu_rate': cpu.CpuRate if cpu.ControlFlags else 0,
                'memory_bytes': memory.ProcessMemoryLimit if memory.Basic.LimitFlags & 0x100 else None}

    def clear(self, key):
        job, _ = self.jobs[key]
        cpu, memory = _Cpu(), _Extended()
        _check(_set(job, 15, ctypes.byref(cpu), ctypes.sizeof(cpu)))
        _check(_set(job, 9, ctypes.byref(memory), ctypes.sizeof(memory)))

    def memory_usage(self, key):
        process = matching_process(key)
        return getattr(process.memory_info(), 'private', None)

    def shutdown(self):
        errors = []
        for key, (job, handle) in tuple(self.jobs.items()):
            try: self.clear(key)
            except OSError as error: errors.append(str(error))
            else:
                _close(job)
                _close(handle)
                del self.jobs[key]
        # On failure keep handles alive so cleanup is retryable; never claim limits cleared.
        if errors: raise OSError('清除 Job 限制失败：' + '; '.join(errors))
