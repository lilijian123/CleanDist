"""采样 Windows RSS（工作集内存），监测过程不修改其他进程。"""
from collections import deque
from dataclasses import dataclass
import time

import psutil


@dataclass(frozen=True)
class ProcessSample:
    pid: int
    created: float
    name: str
    rss: int | None

    @property
    def key(self):
        return self.pid, self.created


@dataclass(frozen=True)
class ProcessReading:
    key: tuple
    name: str
    rss: int | None
    delta: int | None
    observed_seconds: float
    points: tuple


@dataclass(frozen=True)
class MemorySnapshot:
    total: int
    available: int
    percent: float
    processes: tuple
    timestamp: float
    unreadable: int


class MemoryHistory:
    def __init__(self):
        self.histories = {}

    def update(self, samples, now):
        active = {sample.key for sample in samples}
        self.histories = {key: points for key, points in self.histories.items() if key in active}
        rows = []
        for sample in samples:
            points = self.histories.setdefault(sample.key, deque(maxlen=122))
            if sample.rss is None:
                rows.append(ProcessReading(sample.key, sample.name, None, None, 0, tuple(points)))
                continue
            points.append((now, sample.rss))
            cutoff = now - 60
            # 保留时间窗口起点处或其之前最近的一次采样。
            while len(points) > 1 and points[1][0] <= cutoff:
                points.popleft()
            rows.append(ProcessReading(
                sample.key, sample.name, sample.rss,
                sample.rss - points[0][1], max(0, now - points[0][0]), tuple(points),
            ))
        return sorted(rows, key=lambda row: (row.rss is not None, row.rss or 0), reverse=True)


def sample_memory(history):
    memory = psutil.virtual_memory()
    samples = []
    unreadable = 0
    for process in psutil.process_iter(['pid', 'name', 'create_time', 'memory_info'], ad_value=None):
        try:
            data = process.info
            if data['create_time'] is None:
                unreadable += 1
                continue
            info = data['memory_info']
            if info is None:
                unreadable += 1
            samples.append(ProcessSample(
                data['pid'], data['create_time'], data['name'] or f"进程 {data['pid']}",
                info.rss if info is not None else None,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            unreadable += 1
    now = time.monotonic()
    return MemorySnapshot(memory.total, memory.available, memory.percent,
                          tuple(history.update(samples, now)), time.time(), unreadable)
