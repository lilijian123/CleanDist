"""选中进程的只读采样及有界观察窗口；未知数值保持 None。"""
from collections import deque
from dataclasses import dataclass, field, replace
import time

import psutil

from .processes import matching_process


DEEP_METRICS = ('uss', 'pss', 'anonymous', 'file_pss', 'shared_pss', 'swap', 'private_commit')
RATES = {'read_bytes': 'read_bps', 'write_bytes': 'write_bps',
         'read_count': 'read_iops', 'write_count': 'write_iops',
         'read_chars': 'read_chars_bps', 'write_chars': 'write_chars_bps',
         'ctx_voluntary': 'ctx_voluntary_rate', 'ctx_involuntary': 'ctx_involuntary_rate'}
METRICS = ('rss', 'threads', 'resources', 'user_seconds', 'system_seconds',
           *DEEP_METRICS, *RATES, *RATES.values(), 'cpu_percent',
           'cpu_user_percent', 'cpu_system_percent')


@dataclass(frozen=True)
class ThreadUsage:
    tid: int
    user_seconds: float = 0
    system_seconds: float = 0
    cpu_percent: float | None = None
    user_percent: float | None = None
    system_percent: float | None = None
    birth_id: int | None = None


@dataclass(frozen=True)
class OpenFile:
    path: str
    fd: int = -1
    position: int | None = None
    mode: str | None = None


@dataclass(frozen=True)
class MemoryMapping:
    path: str
    rss: int | None = None
    pss: int | None = None
    private: int | None = None
    anonymous: int | None = None


@dataclass(frozen=True)
class Finding:
    title: str
    evidence: str


@dataclass(frozen=True)
class AnalysisSample:
    key: tuple = ()
    captured_at: float = 0
    metrics: dict = field(default_factory=dict)
    threads: tuple = ()
    open_files: tuple = ()
    mappings: tuple = ()
    deep_at: float | None = None
    errors: tuple = ()
    counter_at: float | None = None
    thread_at: float | None = None
    status: str | None = None


@dataclass(frozen=True)
class AnalysisReport:
    key: tuple = ()
    metrics: dict = field(default_factory=dict)
    curves: dict = field(default_factory=dict)
    threads: tuple = ()
    open_files: tuple = ()
    mappings: tuple = ()
    findings: tuple = ()
    errors: tuple = ()
    seconds: float = 0
    samples: int = 0
    deep_age: float | None = None
    deep_at: float | None = None


def sample_analysis(key, deep=False):
    from ..platforms import system
    process = matching_process(key)
    metrics = dict.fromkeys(METRICS)
    errors = []

    def read(label, getter):
        try:
            return getter()
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError, NotImplementedError) as error:
            reason = ('无读取权限' if isinstance(error, (psutil.AccessDenied, PermissionError)) else
                      '当前系统未提供该统计' if isinstance(error, NotImplementedError) else
                      '进程已退出' if isinstance(error, psutil.ZombieProcess) else '读取失败')
            message = f'{label}：{reason}'
            if message not in errors:
                errors.append(message)
            return None

    with process.oneshot():
        memory = read('RSS', process.memory_info)
        metrics['rss'] = memory.rss if memory is not None else None
        cpu = read('CPU', process.cpu_times)
        if cpu is not None:
            metrics.update(user_seconds=cpu.user, system_seconds=cpu.system)
        def io_counters():
            if not hasattr(process, 'io_counters'):
                raise NotImplementedError('当前内核不提供进程I/O统计')
            return process.io_counters()
        io = read('I/O', io_counters)
        if io is not None:
            metrics.update({name: getattr(io, name, None) for name in RATES if not name.startswith('ctx_')})
        contexts = read('上下文切换', lambda: system.context_counters(process))
        if contexts is not None:
            metrics.update(ctx_voluntary=contexts.voluntary, ctx_involuntary=contexts.involuntary)
        metrics['threads'] = read('线程数', process.num_threads)
        metrics['resources'] = read('FD/句柄数', lambda: system.process_counter(process))
        status = read('状态', process.status)
    counter_at = time.monotonic()
    threads, files, mappings = (), (), ()
    deep_at = None
    thread_at = None
    if deep:
        deep_at = time.monotonic()
        details = read('详细内存', lambda: system.sample_memory_breakdown(process))
        if details is not None:
            metrics.update({name: details.get(name) for name in DEEP_METRICS})
            mappings = tuple(details.get('mappings', ()))
            errors.extend(details.get('errors', ()))
        initial_threads = read('线程快照', process.threads)
        identities = {}
        if initial_threads is not None:
            identities = {item.id: read('线程身份', lambda tid=item.id: system.thread_identity(process, tid))
                          for item in initial_threads[:4096]}
        raw_threads = read('线程快照', process.threads)
        thread_at = time.monotonic()
        if raw_threads is not None:
            rows = []
            for item in raw_threads[:4096]:
                birth = read('线程身份', lambda tid=item.id: system.thread_identity(process, tid))
                confirmed = birth if birth is not None and identities.get(item.id) == birth else None
                rows.append(ThreadUsage(item.id, item.user_time, item.system_time, birth_id=confirmed))
            threads = tuple(rows)
            if len(raw_threads) > 4096:
                errors.append('线程快照仅保留前4096项')
        raw_files = read('打开文件', process.open_files)
        if raw_files is not None:
            files = tuple(OpenFile(item.path, item.fd, getattr(item, 'position', None),
                                   getattr(item, 'mode', None)) for item in raw_files[:1000])
            if len(raw_files) > 1000:
                errors.append('打开文件快照仅保留前1000项')
    # 再核对身份，避免一次详细采样跨越退出或 PID 复用。
    if not process.is_running():
        raise ValueError('进程已退出或 PID 已被复用，请重新选择')
    matching_process(key)
    return AnalysisSample(key, time.monotonic(), metrics, threads, files, mappings,
                          deep_at, tuple(errors), counter_at, thread_at, status)


def _rate(previous, current, name, seconds):
    if previous.counter_at is not None and current.counter_at is not None:
        seconds = current.counter_at - previous.counter_at
    left, right = previous.metrics.get(name), current.metrics.get(name)
    if left is None or right is None or seconds <= 0 or right < left:
        return None
    return (right - left) / seconds


class AnalysisHistory:
    def __init__(self, window=300, max_samples=360, logical_cpus=None):
        self.window = max(1, window)
        self.logical_cpus = max(1, logical_cpus or psutil.cpu_count() or 1)
        self._points = deque(maxlen=max(2, max_samples))
        self._deep = None
        self._threads = ()

    def reset(self):
        self._points.clear()
        self._deep = None
        self._threads = ()

    def add(self, sample):
        previous = self._points[-1][0] if self._points else None
        if previous and (previous.key != sample.key or sample.captured_at <= previous.captured_at):
            self.reset()
            previous = None
        metrics = dict.fromkeys(METRICS)
        metrics.update(sample.metrics)
        if previous:
            seconds = sample.captured_at - previous.captured_at
            for name, target in RATES.items():
                metrics[target] = _rate(previous, sample, name, seconds)
            for name, target in (('user_seconds', 'cpu_user_percent'),
                                 ('system_seconds', 'cpu_system_percent')):
                value = _rate(previous, sample, name, seconds)
                metrics[target] = min(100, value * 100 / self.logical_cpus) if value is not None else None
            if metrics['cpu_user_percent'] is not None and metrics['cpu_system_percent'] is not None:
                metrics['cpu_percent'] = min(100, metrics['cpu_user_percent'] + metrics['cpu_system_percent'])
        if sample.deep_at is not None:
            self._update_threads(sample)
            self._deep = sample
        deep_age = sample.captured_at - self._deep.deep_at if self._deep else None
        fresh = deep_age is not None and 0 <= deep_age <= 15
        errors = list(sample.errors)
        if fresh:
            metrics.update({name: self._deep.metrics.get(name) for name in DEEP_METRICS})
            errors.extend(error for error in self._deep.errors if error not in errors)
        else:
            metrics.update(dict.fromkeys(DEEP_METRICS))
            if deep_age is not None:
                errors.append(f'详细快照已过期（{deep_age:.0f}秒前）')
        metrics.update(deep_age=deep_age, deep_at=self._deep.deep_at if self._deep else None)
        self._points.append((sample, metrics))
        cutoff = sample.captured_at - self.window
        while self._points and self._points[0][0].captured_at < cutoff:
            self._points.popleft()
        curves = {name: [(point.captured_at, values.get(name)) for point, values in self._points]
                  for name in METRICS}
        seconds = sample.captured_at - self._points[0][0].captured_at
        return AnalysisReport(sample.key, metrics, curves, self._threads if fresh else (),
                              self._deep.open_files if fresh else (),
                              self._deep.mappings if fresh else (), self._findings(),
                              tuple(errors), seconds, len(self._points), deep_age,
                              self._deep.deep_at if self._deep else None)

    def _update_threads(self, sample):
        previous = self._deep
        old = {thread.tid: thread for thread in previous.threads} if previous else {}
        current_at = sample.thread_at if sample.thread_at is not None else sample.deep_at
        previous_at = (previous.thread_at if previous.thread_at is not None else previous.deep_at) if previous else None
        seconds = current_at - previous_at if previous else 0
        threads = []
        for thread in sample.threads:
            before = old.get(thread.tid)
            if (before and seconds > 0 and before.birth_id is not None
                    and before.birth_id == thread.birth_id):
                user = thread.user_seconds - before.user_seconds
                system = thread.system_seconds - before.system_seconds
                if user >= 0 and system >= 0:
                    thread = replace(thread, cpu_percent=(user + system) / seconds * 100,
                                     user_percent=user / seconds * 100,
                                     system_percent=system / seconds * 100)
            threads.append(thread)
        self._threads = tuple(sorted(threads, key=lambda item: (
            item.cpu_percent is not None, item.cpu_percent or 0,
            item.user_seconds + item.system_seconds), reverse=True)[:20])

    def _findings(self):
        findings = []
        growth = {}
        for name, label, threshold in (('rss', '驻留内存', 1024 * 1024),
                                       ('resources', 'FD/句柄数', 5), ('threads', '线程数', 3),
                                       ('uss', '私有驻留（USS）', 1024 * 1024),
                                       ('anonymous', '匿名内存', 1024 * 1024),
                                       ('file_pss', '文件PSS', 1024 * 1024)):
            # 权限缺口后的连续有效区段才算观察窗口。
            points = []
            deep_seen = set()
            for sample, values in self._points:
                captured_at = sample.captured_at
                if name in DEEP_METRICS:
                    if sample.deep_at is None or sample.deep_at in deep_seen:
                        continue
                    deep_seen.add(sample.deep_at)
                    captured_at = sample.deep_at
                    values = sample.metrics if 0 <= sample.captured_at - sample.deep_at <= 15 else {}
                value = values.get(name)
                if value is None:
                    points = []
                else:
                    points.append((captured_at, value))
            if len(points) < 6:
                continue
            seconds = points[-1][0] - points[0][0]
            delta = points[-1][1] - points[0][1]
            changes = [right[1] - left[1] for left, right in zip(points, points[1:])]
            if seconds >= 30 and delta >= threshold and all(change >= 0 for change in changes) and sum(
                    change > 0 for change in changes) >= len(changes) * .8:
                evidence = (f'{seconds:.0f}秒、{len(points)}次连续有效采样：'
                            f'{points[0][1]:,.0f} → {points[-1][1]:,.0f}，增加{delta:,.0f}'
                            f'{"字节" if name == "rss" or name in DEEP_METRICS else "项"}；仅为观察趋势。')
                findings.append(Finding(f'{label}持续增长', evidence))
                growth[name] = (label, points[0][0], points[-1][0])
        if 'rss' in growth:
            _, rss_start, rss_end = growth['rss']
            companions = [label for name, (label, start, end) in growth.items() if name in DEEP_METRICS
                          and min(end, rss_end) - max(start, rss_start) >= 30]
            if companions:
                findings = [replace(finding, evidence=finding.evidence +
                            f'同一观察窗口伴随{"、".join(companions)}增长；不能确定具体对象或调用栈。')
                            if finding.title == '驻留内存持续增长' else finding for finding in findings]
        statuses = [sample.status for sample, _ in self._points if sample.status is not None]
        waiting = sum(status in ('disk-sleep', 'D') for status in statuses)
        if waiting:
            findings.append(Finding('观察到不可中断等待',
                f'{len(statuses)}次有效状态快照中有{waiting}次为D/disk-sleep；'
                '这是采样瞬间的状态，不能单凭状态归因磁盘瓶颈。'))
        if len(self._points) >= 6:
            first, _ = self._points[0]
            last, _ = self._points[-1]
            seconds = ((last.counter_at - first.counter_at)
                       if first.counter_at is not None and last.counter_at is not None
                       else last.captured_at - first.captured_at)
            if seconds >= 30:
                def complete_rate(name):
                    # 任意未知或计数回退都会中断该窗口的证据。
                    if any(values.get(name) is None for _, values in self._points):
                        return None
                    samples = [point for point, _ in self._points]
                    if any(right.metrics[name] < left.metrics[name]
                           for left, right in zip(samples, samples[1:])):
                        return None
                    return _rate(first, last, name, seconds)

                user, system = complete_rate('user_seconds'), complete_rate('system_seconds')
                if user is not None and system is not None and user + system >= .2:
                    findings.append(Finding('CPU活动',
                        f'{seconds:.0f}秒内累计使用{(user + system) * seconds:.2f} CPU秒，'
                        f'平均占整机{(user + system) / self.logical_cpus * 100:.1f}%；'
                        f'用户态{user * seconds:.2f}秒、内核态{system * seconds:.2f}秒。'))
                read, write = complete_rate('read_bytes'), complete_rate('write_bytes')
                if read is not None and write is not None and read + write > 0:
                    findings.append(Finding('I/O计数活动',
                        f'{seconds:.0f}秒内读取计数增加{read * seconds:,.0f}字节，'
                        f'写入计数增加{write * seconds:,.0f}字节；'
                        '该统计不能单独证明物理磁盘吞吐或I/O等待。'))
        return tuple(findings)
