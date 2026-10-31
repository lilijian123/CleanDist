"""Threaded desktop adapters over the existing read-only resource samplers."""
from dataclasses import asdict, is_dataclass
import base64
import math
from pathlib import Path
import threading
import time

from ..core.ai import summarize_report
from ..core.memory import MemoryHistory, sample_memory
from ..core.process_analysis import AnalysisHistory, sample_analysis
from ..core.processes import (cpu_percentage, matching_process, sample_process_details,
                              summarize_memory)
from ..platforms import system


def _json(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _page(offset, limit):
    if type(offset) is not int or type(limit) is not int or offset < 0 or limit < 1:
        raise ValueError('无效的分页参数')
    return offset, min(limit, 200)


def _key(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError('无效的进程身份')
    pid, created = value
    if (type(pid) is not int or pid <= 0 or type(created) not in (int, float)
            or not math.isfinite(created) or created <= 0):
        raise ValueError('无效的进程身份')
    return pid, created


def _report_json(report):
    result = _json({name: getattr(report, name) for name in
        ('key', 'metrics', 'seconds', 'samples', 'deep_age', 'deep_at')})
    result['curves'] = {}
    for name, points in report.curves.items():
        indices = sorted({round(index * (len(points) - 1) / 59) for index in range(60)}) if len(points) > 60 else range(len(points))
        result['curves'][name] = _json([points[index] for index in indices])
    for name in ('threads', 'open_files', 'mappings', 'findings', 'errors'):
        result[name] = _json(getattr(report, name)[:200])
    return result


class ProcessService:
    def __init__(self):
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._fast_wake, self._deep_wake = threading.Event(), threading.Event()
        self._icon_wake = threading.Event()
        self._icons, self._icon_queue, self._icon_pending = {}, {}, set()
        self._memory_history = MemoryHistory()
        self._history = AnalysisHistory()
        self._snapshot = None
        self._rows = ()
        self._key = None
        self._name = ''
        self._generation = 0
        self._details = self._previous_details = self._report = None
        self._sample_at = None
        self._trace = None
        self._trace_requested = False
        self._message = ''
        self._stopping = False
        # Deep reads can block on OS permissions and large mappings. They never
        # share a worker with the basic sampler or whole-process-list sampler.
        self._threads = [threading.Thread(target=lambda: self._loop(self._sample_all, 1),
                                          daemon=True, name='desktop-process-list'),
                         threading.Thread(target=lambda: self._loop(self._sample_selected, 1, self._fast_wake),
                                          daemon=True, name='desktop-process-basic'),
                         threading.Thread(target=lambda: self._loop(self._sample_deep, 5, self._deep_wake),
                                          daemon=True, name='desktop-process-deep'),
                         threading.Thread(target=lambda: self._loop(self._load_icons, 60, self._icon_wake),
                                          daemon=True, name='desktop-process-icons')]
        for worker in self._threads:
            worker.start()

    def _loop(self, action, interval, wake=None):
        wake = wake or self._closed
        while not self._closed.is_set():
            try:
                action()
            except Exception as error:
                # Sampling failures must not silently kill future refreshes.
                with self._lock:
                    if not self._closed.is_set() and self._key:
                        self._message = str(error)
            wake.wait(interval)
            if wake is not self._closed:
                wake.clear()

    def _sample_all(self):
        snapshot = sample_memory(self._memory_history)
        with self._lock:
            if not self._closed.is_set():
                self._snapshot, self._rows = snapshot, snapshot.processes
                active = {row.key for row in self._rows}
                self._icons = {key:data for key,data in self._icons.items() if key in active}
                self._icon_queue = {key:None for key in self._icon_queue if key in active}
                self._icon_pending.intersection_update(active)

    def icons(self, keys):
        if not isinstance(keys, list) or len(keys)>32:
            raise ValueError('每次最多请求32个可见进程图标')
        keys = tuple(dict.fromkeys(_key(key) for key in keys))
        with self._lock:
            if self._closed.is_set(): raise ValueError('资源服务已关闭')
            active = {row.key:row.key for row in self._rows}
            if any(key not in active for key in keys): raise ValueError('进程已退出或身份已变化')
            keys = tuple(active[key] for key in keys)
            for key in keys:
                if key not in self._icons and key not in self._icon_pending and len(self._icon_pending)<128:
                    self._icon_pending.add(key)
                    self._icon_queue[key] = None
            if self._icon_queue: self._icon_wake.set()
            return {'icons': {f'{key[0]}:{key[1]}':self._icons[key] for key in keys if key in self._icons},
                    'pending':any(key not in self._icons for key in keys)}

    def _load_icons(self):
        with self._lock:
            keys = tuple(self._icon_queue)[:32]
            for key in keys: self._icon_queue.pop(key, None)
        if not keys or self._closed.is_set(): return
        try:
            images = system.load_process_icons(keys, self._closed)
        except Exception:
            images = {}
        with self._lock:
            active = {row.key for row in self._rows}
            for key in keys:
                self._icon_pending.discard(key)
                if key not in active or self._closed.is_set(): continue
                data = images.get(key, b'')
                self._icons[key] = ('data:image/png;base64,'+base64.b64encode(data).decode('ascii')
                                   if isinstance(data,bytes) and data.startswith(b'\x89PNG\r\n\x1a\n') and len(data)<=65536 else '')
            if self._icon_queue: self._icon_wake.set()

    def _selection(self):
        with self._lock:
            return self._key, self._generation

    def _current(self, key, generation):
        return not self._closed.is_set() and key == self._key and generation == self._generation

    def _sample_selected(self):
        key, generation = self._selection()
        if not key:
            return
        try:
            sample = sample_analysis(key)
            with self._lock:
                if not self._current(key, generation):
                    return
                self._report = self._history.add(sample)
                self._sample_at = sample.captured_at
            details = sample_process_details(key)
            with self._lock:
                if self._current(key, generation):
                    self._previous_details, self._details = self._details, details
        except Exception as error:
            with self._lock:
                if self._current(key, generation):
                    self._message = str(error)

    def _sample_deep(self):
        key, generation = self._selection()
        if not key:
            return
        try:
            sample = sample_analysis(key, deep=True)
            with self._lock:
                if self._current(key, generation):
                    report = self._history.update_details(sample)
                    if report is not None:
                        self._report = report
        except Exception as error:
            with self._lock:
                if self._current(key, generation):
                    self._message = str(error)

    def state(self):
        with self._lock:
            snapshot = self._snapshot
            result = {'total': snapshot.total if snapshot else 0,
                      'available': snapshot.available if snapshot else 0,
                      'percent': snapshot.percent if snapshot else None,
                      'process_count': len(self._rows),
                      'updated': snapshot.timestamp if snapshot else None, 'selected': None}
            if self._key:
                details = _json(self._details) if self._details else None
                if details is not None:
                    row = next((row for row in self._rows if row.key == self._key), None)
                    details['memory'] = _json(summarize_memory(row.points)) if row else None
                    details['cpu_percent'] = cpu_percentage(self._previous_details, self._details)
                try:
                    summary = self._summary()
                except ValueError:
                    summary = None
                result['selected'] = {'key': list(self._key), 'name': self._name,
                    'details': details, 'report': _report_json(self._report) if self._report else None,
                    'summary': summary, 'trace': self._trace_state(), 'message': self._message}
            return _json(result)

    def rows(self, offset=0, limit=100, query='', sort='rss'):
        offset, limit = _page(offset, limit)
        if sort not in ('rss', 'delta', 'name', 'pid', 'seconds'):
            raise ValueError('无效的进程排序方式')
        query = str(query).strip().casefold()
        with self._lock:
            rows = [row for row in self._rows if not query or query in row.name.casefold()
                    or query in str(row.key[0])]
        if sort == 'name':
            rows.sort(key=lambda row: (row.name.casefold(), row.key))
        elif sort == 'pid':
            rows.sort(key=lambda row: row.key)
        else:
            field = 'observed_seconds' if sort == 'seconds' else sort
            rows.sort(key=lambda row: (getattr(row, field) is not None, getattr(row, field) or 0), reverse=True)
        return _json({'total': len(rows), 'rows': [{'id': f'{row.key[0]}:{row.key[1]}',
            'key': row.key, 'name': row.name, 'rss': row.rss, 'delta': row.delta,
            'seconds': row.observed_seconds} for row in rows[offset:offset + limit]]})

    def select(self, key):
        key = _key(key)
        with self._lock:
            if self._closed.is_set():
                raise ValueError('资源服务已关闭')
            row = next((row for row in self._rows if row.key == key), None)
            if row is None:
                raise ValueError('进程不在已采样列表中，请刷新后重选')
        matching_process(key)
        with self._lock:
            if self._closed.is_set():
                raise ValueError('资源服务已关闭')
            if self._stopping and key != self._key:
                raise ValueError('请等待当前进程结束请求返回后再切换')
            if key != self._key:
                old_trace, self._trace = self._trace, None
                self._trace_requested = False
                if old_trace:
                    old_trace.close()
                self._generation += 1
                self._key, self._name = key, row.name
                self._details = self._previous_details = self._report = self._sample_at = None
                self._history.reset()
                self._message = ''
        self._fast_wake.set()
        self._deep_wake.set()
        return {'key': list(key), 'name': row.name}

    def _summary(self):
        if (self._closed.is_set() or self._report is None or self._sample_at is None
                or not 0 <= time.monotonic() - self._sample_at <= 15):
            raise ValueError('尚无15秒内的有效进程样本，请等待采样后重试')
        return _json(summarize_report(self._report, system.NAME, self._name))

    def summary(self):
        with self._lock:
            return self._summary()

    def stop(self, key, force=False, confirmed=False):
        key = _key(key)
        if confirmed is not True or type(force) is not bool:
            raise ValueError('结束进程需要明确确认')
        if force and not getattr(system, 'CAN_FORCE_STOP', False):
            raise ValueError('当前平台不支持强制结束')
        with self._lock:
            if self._closed.is_set() or key != self._key:
                raise ValueError('只能结束当前选中的进程身份')
            if self._stopping:
                raise ValueError('正在等待结束请求结果')
            generation = self._generation
            self._stopping = True
            self._message = '正在发送结束请求，尚未确认退出'
        try:
            result = system.stop_process(key, force=True) if force else system.stop_process(key)
            with self._lock:
                if self._current(key, generation):
                    self._message = result.message
            return _json(result)
        except Exception as error:
            with self._lock:
                if self._current(key, generation):
                    self._message = str(error)
            raise
        finally:
            with self._lock:
                self._stopping = False

    def _trace_state(self):
        if self._trace is None:
            return None
        state = self._trace.snapshot()
        result = _json({name: getattr(state, name) for name in
                       ('key', 'active', 'status', 'dropped', 'unparsed', 'finished')})
        result['events'] = _json(state.events[-200:])
        return result

    def trace(self, enabled=True):
        if type(enabled) is not bool:
            raise ValueError('无效的跟踪开关')
        with self._lock:
            if self._closed.is_set() or not self._key:
                raise ValueError('请先选择进程')
            if enabled:
                if self._trace is None or not self._trace_requested:
                    matching_process(self._key)
                    if self._trace is not None:
                        self._trace.close()
                    self._trace = system.FileTrace(self._key)
                    self._trace.start()
                    self._trace_requested = True
            elif self._trace is not None:
                self._trace.close()
                self._trace_requested = False
            return self._trace_state()

    def shutdown(self):
        with self._lock:
            self._closed.set()
            self._generation += 1
            if self._trace is not None:
                self._trace.close()
                self._trace = None
        self._fast_wake.set()
        self._deep_wake.set()
        self._icon_wake.set()


class AppService:
    def __init__(self):
        self._lock = threading.RLock()
        self._closed = False
        self._cancel = threading.Event()
        self._generation = 0
        self._apps = ()
        self._busy = False
        self._error = ''
        self._errors = ()
        self._stage = '尚未扫描'
        self._skipped = 0
        self._elapsed = self._started = 0

    def state(self):
        with self._lock:
            return _json({'busy': self._busy, 'count': len(self._apps), 'error': self._error,
                    'errors': list(self._errors[:100]), 'stage': self._stage,
                    'skipped': self._skipped,
                    'elapsed': time.monotonic() - self._started if self._busy else self._elapsed})

    def scan(self, roots=None):
        if roots is not None:
            if not isinstance(roots, (list, tuple)) or not roots or any(not isinstance(root, (str, Path)) for root in roots):
                raise ValueError('无效的应用扫描目录')
            roots = tuple(Path(root) for root in roots)
        with self._lock:
            if self._closed:
                raise ValueError('应用服务已关闭')
            if self._busy:
                raise ValueError('应用扫描正在进行')
            self._generation += 1
            generation = self._generation
            self._cancel = cancel = threading.Event()
            self._apps, self._errors = (), ()
            self._error, self._stage = '', '正在扫描'
            self._skipped, self._elapsed = 0, 0
            self._started, self._busy = time.monotonic(), True

        def current():
            return not self._closed and not cancel.is_set() and generation == self._generation

        def progress(value):
            with self._lock:
                if current():
                    self._stage = str(value.stage)

        def run():
            try:
                result = system.scan_apps(roots, cancel, progress)
                with self._lock:
                    if current():
                        self._apps, self._errors = result.apps, result.errors
                        self._skipped, self._elapsed = result.skipped, result.elapsed
                        self._stage = '已停止' if result.cancelled else '部分结果' if result.errors or result.skipped else '扫描完成'
            except Exception as error:
                with self._lock:
                    if current():
                        self._error, self._stage = str(error), '扫描失败'
                        self._elapsed = time.monotonic() - self._started
            finally:
                with self._lock:
                    if current():
                        self._busy = False

        threading.Thread(target=run, daemon=True, name='desktop-app-scan').start()
        return self.state()

    def rows(self, offset=0, limit=100, query='', source=''):
        offset, limit = _page(offset, limit)
        query = str(query).strip().casefold()
        with self._lock:
            rows = [(index, app) for index, app in enumerate(self._apps)
                    if (not source or app.source == source) and (not query or query in
                        ' '.join((app.name, app.version, app.publisher, str(app.path or ''))).casefold())]
        return {'total': len(rows), 'rows': [{'id': str(index), 'name': app.name,
            'source': app.source, 'version': app.version, 'publisher': app.publisher,
            'drive': app.drive, 'path': str(app.path) if app.path else ''}
            for index, app in rows[offset:offset + limit]]}

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._cancel.set()
            if self._busy:
                self._stage = '已请求停止'
                self._elapsed = time.monotonic() - self._started
            self._busy = False
