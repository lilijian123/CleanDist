"""Explicitly confirmed resource limits; never restored automatically on startup."""
from collections import deque
import math
import os
import sys
import threading
import time

import psutil

from ..core.processes import matching_process
from ..core.process_analysis import AnalysisHistory, sample_analysis
from .resources import _key, _json, _report_json


class ProcessControl:
    def __init__(self, backend=None):
        if backend is None:
            if sys.platform == 'win32':
                from ..platforms.windows.control import Controller
            else:
                from ..platforms.linux.control import Controller
            backend = Controller()
        self._backend = backend
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._managed, self._histories = {}, {}
        self._previous_io = {}
        self._events = deque(maxlen=200)
        self._worker = threading.Thread(target=self._run, daemon=True, name='resource-limit-effects')
        self._worker.start()

    def state(self):
        with self._lock:
            return _json({'capabilities': {**self._backend.capabilities(), 'logical_cpus': os.cpu_count() or 1},
                          'managed': list(self._managed.values()), 'events': list(self._events)})

    def apply(self, key, cpu_cores=None, memory_bytes=None, hard_memory=False,
              device=None, read_bps=None, write_bps=None, confirmed=False):
        key = _key(key)
        if confirmed is not True:
            raise ValueError('应用资源限制需要明确确认')
        if key[0] in ((4, os.getpid()) if sys.platform == 'win32' else (1, os.getpid())):
            raise ValueError('不限制本应用自身或系统基础进程')
        if type(hard_memory) is not bool:
            raise ValueError('无效的硬内存上限选项')
        for name, value in (('CPU核心当量', cpu_cores), ('内存字节', memory_bytes),
                            ('读取速率', read_bps), ('写入速率', write_bps)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError(name + '必须是正数')
        if cpu_cores is not None and cpu_cores > (os.cpu_count() or 1):
            raise ValueError('CPU核心当量不能超过逻辑处理器数')
        for value in (memory_bytes, read_bps, write_bps):
            if value is not None and (int(value) != value or value > 2**63 - 1):
                raise ValueError('字节数和字节速率必须是有效整数')
        if all(value is None for value in (cpu_cores, memory_bytes, read_bps, write_bps)):
            raise ValueError('至少填写一个资源限制')
        if device is not None and not isinstance(device, str):
            raise ValueError('无效的块设备')
        limits = dict(cpu_cores=cpu_cores, memory_bytes=int(memory_bytes) if memory_bytes is not None else None,
                      hard_memory=hard_memory, device=device,
                      read_bps=int(read_bps) if read_bps is not None else None,
                      write_bps=int(write_bps) if write_bps is not None else None)
        with self._lock:
            if self._closed.is_set(): raise ValueError('管控服务已关闭')
            matching_process(key)
            native = self._backend.apply(key, **limits)
            stamp = time.time()
            previous = self._managed.get(key)
            self._managed[key] = {'key': key, 'active': True, 'limits': limits, 'native': native,
                                  'applied_at': stamp, 'cleared_at': None, 'error': '',
                                  'report': previous['report'] if previous else None,
                                  'report_time_offset': previous.get('report_time_offset') if previous else None,
                                  'controlled_memory': previous['controlled_memory'] if previous else []}
            self._managed[key]['controlled_io'] = previous.get('controlled_io', {}) if previous else {}
            self._histories.setdefault(key, AnalysisHistory())
            self._events.append({'key': key, 'action': 'modify' if previous and previous['active'] else 'apply',
                                 'timestamp': stamp, 'limits': limits})
        return self.state()

    def clear(self, key, confirmed=False):
        key = _key(key)
        if confirmed is not True: raise ValueError('解除资源限制需要明确确认')
        with self._lock:
            if self._closed.is_set(): raise ValueError('管控服务已关闭')
            if key not in self._managed: raise ValueError('该进程没有本应用管理的限制')
            self._backend.clear(key)
            stamp = time.time()
            self._managed[key].update(active=False, cleared_at=stamp, error='')
            self._events.append({'key': key, 'action': 'clear', 'timestamp': stamp})
        return self.state()

    def _run(self):
        while not self._closed.wait(1):
            with self._lock: keys = tuple(self._managed)
            for key in keys:
                try:
                    sample = sample_analysis(key)
                    with self._lock:
                        if self._closed.is_set(): return
                        entry = self._managed[key]
                        entry['report'] = _report_json(self._histories[key].add(sample))
                        entry['report_time_offset'] = time.time() - sample.captured_at
                        reading = self._backend.memory_usage(key)
                        entry['controlled_memory'] = (entry['controlled_memory'] + [[time.time(), reading]])[-120:]
                        if self._backend.capabilities().get('io'):
                            counters, stamp = self._backend.io_usage(key), time.time()
                            before = self._previous_io.get(key)
                            device = counters['device']
                            if entry['controlled_io'].get('device') != device:
                                entry['controlled_io'] = {'device': device, 'read_bps': [], 'write_bps': []}
                                before = None
                            for field, curve in (('read_bytes', 'read_bps'), ('write_bytes', 'write_bps')):
                                value = None
                                if before and before[0] < stamp and before[1]['device'] == device:
                                    old, new = before[1][field], counters[field]
                                    if old is not None and new is not None and new >= old: value = (new - old) / (stamp - before[0])
                                entry['controlled_io'][curve] = (entry['controlled_io'].get(curve, []) + [[stamp, value]])[-120:]
                            self._previous_io[key] = stamp, counters
                        entry['error'] = ''
                except (psutil.Error, OSError, ValueError) as error:
                    with self._lock: self._managed[key]['error'] = str(error)

    def shutdown(self):
        self._closed.set()
        with self._lock:
            self._backend.shutdown()
