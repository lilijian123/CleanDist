"""Immutable file syscall observations and the unsupported-platform fallback."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TraceEvent:
    timestamp: float
    tid: int
    operation: str
    path: str
    bytes_count: int
    duration_seconds: float | None = None


@dataclass(frozen=True)
class TraceState:
    key: tuple[int, float]
    active: bool
    status: str
    events: tuple[TraceEvent, ...] = ()
    dropped: int = 0
    unparsed: int = 0
    finished: bool = True


class FileTrace:
    """Windows/other platforms do not provide historical syscall attribution."""
    def __init__(self, key):
        self.key = key

    def start(self):
        pass

    def snapshot(self):
        return TraceState(self.key, False, '当前平台不支持文件读写系统调用跟踪；打开文件快照不代表历史写入归因')

    def close(self):
        pass
