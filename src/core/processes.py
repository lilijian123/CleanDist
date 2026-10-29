"""选中进程的只读详情和观察窗口分析；平台动作由 system 提供。"""
from dataclasses import dataclass
import math
import time

import psutil


@dataclass(frozen=True)
class MemorySummary:
    current: int
    minimum: int
    peak: int
    mean: float
    delta: int
    rate: float | None
    seconds: float
    samples: int


def summarize_memory(points):
    if not points:
        return None
    values = [value for _, value in points]
    seconds = max(0, points[-1][0] - points[0][0])
    delta = values[-1] - values[0]
    return MemorySummary(values[-1], min(values), max(values), sum(values) / len(values),
                         delta, delta / seconds if seconds else None, seconds, len(values))


@dataclass(frozen=True)
class ProcessDetails:
    key: tuple
    name: str = ''
    path: str | None = None
    parent_pid: int | None = None
    parent_name: str | None = None
    status: str | None = None
    threads: int | None = None
    handles: int | None = None
    rss: int | None = None
    private: int | None = None
    cpu_time: float | None = None
    captured_at: float = 0
    errors: tuple = ()
    started_at: float | None = None


@dataclass(frozen=True)
class StopResult:
    key: tuple
    exited: bool
    message: str


def matching_process(key):
    from ..platforms import system
    pid, created = key
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not math.isfinite(created) or created <= 0:
        raise ValueError('无效的进程身份')
    process = psutil.Process(pid)
    if system.process_created(process) != created or not process.is_running():
        raise ValueError('进程已退出或 PID 已被复用，请重新选择')
    return process


def sample_process_details(key):
    from ..platforms import system
    process = matching_process(key)
    errors = []

    def read(field, getter):
        try:
            return getter()
        except (psutil.AccessDenied, psutil.ZombieProcess) as error:
            errors.append(f'{field}：{type(error).__name__}')
            return None

    with process.oneshot():
        name = read('名称', process.name)
        path = read('路径', process.exe)
        parent_pid = read('父进程', process.ppid)
        status = read('状态', process.status)
        threads = read('线程', process.num_threads)
        handles = read(system.PROCESS_COUNTER_LABEL, lambda: system.process_counter(process))
        memory = read('内存', process.memory_info)
        private = read(system.PRIVATE_MEMORY_LABEL, lambda: system.private_memory(process, memory))
        cpu = read('CPU', process.cpu_times)
        started_at = read('启动时间', process.create_time)
    parent_name = None
    if parent_pid:
        try:
            parent = psutil.Process(parent_pid)
            if system.process_created(parent) <= key[1]:
                parent_name = parent.name()
        except psutil.Error:
            pass
    if not process.is_running():
        raise ValueError('进程已退出或 PID 已被复用，请重新选择')
    return ProcessDetails(key, name or f'PID {key[0]}', path, parent_pid, parent_name, status,
                          threads, handles, memory.rss if memory is not None else None,
                          private,
                          cpu.user + cpu.system if cpu is not None else None,
                          time.monotonic(), tuple(errors), started_at)


def cpu_percentage(previous, current, logical_cpus=None):
    if previous is None or previous.key != current.key or previous.cpu_time is None or current.cpu_time is None:
        return None
    seconds = current.captured_at - previous.captured_at
    used = current.cpu_time - previous.cpu_time
    if seconds <= 0 or used < 0:
        return None
    count = logical_cpus or psutil.cpu_count() or 1
    return min(100, used / seconds / count * 100)
