"""合并多个实际扫描根；文件身份始终保留扫描器给出的根与相对路径。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from functools import cached_property
import os
from pathlib import Path
import threading
import time

from .analysis import DirectoryEntry, FileAnalysis, analyze_files
from .cleanup import CATEGORIES, CleanupAnalysis, CleanupItem, classify_cleanup, running_applications
from .files import scan_directory


def record_key(record):
    return os.path.normcase(os.path.abspath(record.path))


@dataclass(frozen=True)
class ScanBatch:
    snapshots: tuple
    errors: tuple = ()
    elapsed: float = 0
    _cancelled: bool = False

    @property
    def roots(self):
        return tuple(snapshot.root for snapshot in self.snapshots)

    @cached_property
    def files(self):
        records = {}
        for snapshot in self.snapshots:
            for record in snapshot.files.values():
                records.setdefault(record_key(record), record)
        return records

    @property
    def complete(self):
        return bool(self.snapshots) and not self.errors and not self.cancelled and all(snapshot.complete for snapshot in self.snapshots)

    @property
    def skipped(self):
        return sum(snapshot.skipped for snapshot in self.snapshots)

    @property
    def total_bytes(self):
        return sum(record.size for record in self.files.values())

    @property
    def free_bytes(self):
        if len(self.snapshots) == 1:
            return self.snapshots[0].free_bytes
        # anchor 无法区分 Linux 挂载点；只合计能确认卷身份的根。
        volumes = {}
        for snapshot in self.snapshots:
            if snapshot.free_bytes is None:
                return None
            try:
                device = snapshot.root.stat().st_dev
            except OSError:
                return None
            if not device:
                return None
            volumes.setdefault(device, snapshot.free_bytes)
        return sum(volumes.values()) if volumes else None

    @property
    def timestamp(self):
        return max((snapshot.timestamp for snapshot in self.snapshots), default=0)

    @property
    def root(self):
        """仅兼容界面展示，不能作为跨根清理的授权边界。"""
        return self.snapshots[0].root if self.snapshots else None

    @property
    def cancelled(self):
        return self._cancelled or any(snapshot.cancelled for snapshot in self.snapshots)


@dataclass
class BatchAnalysis:
    snapshot: ScanBatch
    cleanup: CleanupAnalysis
    large_records: tuple

    @property
    def all_records(self):
        return self.snapshot.files.values()


def scan_batch(roots, cancel=None, progress=None):
    start = time.monotonic()
    cancel = cancel if cancel is not None else threading.Event()
    normalized = {}
    for root in roots:
        path = Path(os.path.abspath(root))
        normalized.setdefault(os.path.normcase(str(path)), path)
    selected = []
    for key, path in sorted(normalized.items(), key=lambda item: len(item[0])):
        if not any(key == parent or key.startswith(parent.rstrip('\\/') + os.sep) for parent, _ in selected):
            selected.append((key, path))
    # 恢复用户所选顺序，结果不受线程完成先后影响。
    selected_keys = {key for key, _ in selected}
    paths = [path for key, path in normalized.items() if key in selected_keys]
    snapshots, errors = {}, {}

    def scan(root, callback):
        if cancel.is_set():
            return None
        return scan_directory(root, cancel=cancel, progress=callback)

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = {}
        for index, root in enumerate(paths):
            if cancel.is_set():
                break
            callback = (lambda update, root=root: progress(root, update)) if progress else None
            jobs[executor.submit(scan, root, callback)] = (index, root)
        for job in as_completed(jobs):
            index, root = jobs[job]
            try:
                snapshot = job.result()
                if snapshot is None:
                    continue
                snapshots[index] = snapshot
                if snapshot.errors:
                    errors[index] = tuple(f'{root}: {error}' for error in snapshot.errors)
            except Exception as error:
                errors[index] = (f'{root}: {error}',)
    results = tuple(snapshots[index] for index in sorted(snapshots))
    if cancel.is_set():
        results = tuple(replace(snapshot, complete=False, cancelled=True) for snapshot in results)
    return ScanBatch(results, tuple(error for index in sorted(errors) for error in errors[index]),
                     time.monotonic() - start, _cancelled=cancel.is_set())


def analyze_batch(batch, cancel=None):
    if cancel is not None and cancel.is_set():
        return None
    running = running_applications()
    items, by_file = {}, {}
    for snapshot in batch.snapshots:
        cleanup = classify_cleanup(snapshot, running=running, cancel=cancel)
        if cleanup is None:
            return None
        for item in cleanup.items:
            merged = items.setdefault(item.key, CleanupItem(item.key, item.category, item.name, item.risk,
                                                           item.reason, recommended=item.recommended))
            merged.recommended = merged.recommended and item.recommended
            for index, record in enumerate(item.records):
                if index % 4096 == 0 and cancel is not None and cancel.is_set():
                    return None
                key = record_key(record)
                if key not in by_file:
                    merged.records.append(batch.files[key])
                    by_file[key] = merged
    order = {key: index for index, key in enumerate(CATEGORIES)}
    for item in items.values():
        item.records.sort(key=lambda record: (-record.size, record_key(record)))
    ordered = tuple(sorted((item for item in items.values() if item.records),
                           key=lambda item: (order[item.category], ('low', 'medium', 'high').index(item.risk), -item.size)))
    large_records = []
    for index, record in enumerate(batch.files.values()):
        if index % 4096 == 0 and cancel is not None and cancel.is_set():
            return None
        if record.size >= 1024 * 1024:
            large_records.append(record)
    large_records.sort(key=lambda record: (-record.size, record_key(record)))
    if cancel is not None and cancel.is_set():
        return None
    return BatchAnalysis(batch, CleanupAnalysis(batch, ordered, by_file), tuple(large_records))


def build_matrix(batch, cancel=None):
    """仅给多根目录条目增加展示标识，FileChange 仍引用实际扫描记录。"""
    if cancel is not None and cancel.is_set():
        return None
    directories = {'': DirectoryEntry('', '所有位置')}
    children, changes, seen = {'': []}, [], set()
    for index, snapshot in enumerate(batch.snapshots):
        files = {}
        for relative, record in snapshot.files.items():
            key = record_key(record)
            if key not in seen:
                files[relative] = record
                seen.add(key)
        analysis = analyze_files(None, replace(snapshot, files=files), cancel=cancel,
                                 include_growth=False, include_cleanup=False)
        if analysis is None:
            return None
        prefix = f'\0root/{index}'
        mapped = {relative: replace(entry, relative=prefix + ('/' + relative if relative else ''),
                                   name=str(snapshot.root) if not relative else entry.name)
                  for relative, entry in analysis.directories.items()}
        directories.update((entry.relative, entry) for entry in mapped.values())
        # 小文件分组只在 children 中；它们同样需要独立的展示标识。
        for relative, entries in analysis.children.items():
            key = prefix + ('/' + relative if relative else '')
            children[key] = [mapped.get(entry.relative) or replace(entry, relative=prefix + '/' + entry.relative)
                             if isinstance(entry, DirectoryEntry) else entry for entry in entries]
        root_entry = mapped['']
        children[''].append(root_entry)
        summary = directories['']
        summary.size += root_entry.size
        summary.count += root_entry.count
        summary.modified_ns = max(summary.modified_ns, root_entry.modified_ns)
        changes.extend(analysis.changes)
    children[''].sort(key=lambda entry: (-entry.size, entry.name.casefold()))
    if cancel is not None and cancel.is_set():
        return None
    return FileAnalysis(batch, batch, changes, directories, children, batch.complete, {})
