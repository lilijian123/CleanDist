"""在扫描工作线程中构建目录统计与界面展示数据。"""
from dataclasses import dataclass, replace
from heapq import nlargest
import time

from .files import ScanProgress, diff_snapshots
from .cleanup import classify_cleanup


@dataclass(slots=True)
class DirectoryEntry:
    relative: str
    name: str
    size: int = 0
    count: int = 0
    delta: int = 0
    modified_ns: int = 0
    state: str = '目录汇总'


@dataclass
class FileAnalysis:
    snapshot: object
    comparison: object
    changes: list
    directories: dict
    children: dict
    comparable: bool
    growth_children: dict
    cleanup: object = None


def analyze_files(previous, snapshot, cancel=None, progress=None, *, include_growth=True, include_cleanup=True):
    start = time.monotonic()

    def report(stage):
        if progress:
            progress(ScanProgress(stage, len(snapshot.files), len(snapshot.directories),
                                  snapshot.elapsed + time.monotonic() - start, snapshot.backend))
        return cancel is not None and cancel.is_set()

    if report('分析文件变化'):
        return None
    if previous is not None and previous.root != snapshot.root:
        previous = None
    changes = diff_snapshots(previous, snapshot, sort=False)
    directories = {'': DirectoryEntry('', snapshot.root.name)}
    children = {'': []}

    def ensure_directory(relative):
        while relative not in directories:
            parent, _, name = relative.rpartition('/')
            entry = DirectoryEntry(relative, name)
            directories[relative] = entry
            children.setdefault(relative, [])
            children.setdefault(parent, []).append(entry)
            relative = parent

    for index, relative in enumerate(snapshot.directories):
        if index % 4096 == 0 and report('汇总目录'):
            return None
        ensure_directory(relative)
    for index, change in enumerate(changes):
        if index % 4096 == 0 and report('汇总目录'):
            return None
        parent = change.relative.rpartition('/')[0]
        ensure_directory(parent)
        children[parent].append(change)
        entry = directories[parent]
        entry.size += change.size
        entry.count += change.count
        entry.delta += change.delta
        entry.modified_ns = max(entry.modified_ns, change.modified_ns)
    for index, relative in enumerate(sorted(directories, key=lambda key: key.count('/') + bool(key), reverse=True)):
        if index % 4096 == 0 and report('汇总目录'):
            return None
        if relative:
            entry = directories[relative]
            parent = directories[relative.rpartition('/')[0]]
            parent.size += entry.size
            parent.count += entry.count
            parent.delta += entry.delta
            parent.modified_ns = max(parent.modified_ns, entry.modified_ns)
    for index, (relative, entries) in enumerate(list(children.items())):
        if index % 4096 == 0 and report('整理明细'):
            return None
        small_files = [entry for entry in entries if not isinstance(entry, DirectoryEntry) and entry.record.size < 1024 * 1024]
        if len(small_files) > 100:
            key = '\0small/' + relative  # 仅用于展示的分组，其标识不可能对应真实文件路径。
            group = DirectoryEntry(key, '小文件（小于1 MB）', sum(entry.size for entry in small_files),
                                   sum(entry.count for entry in small_files), sum(entry.delta for entry in small_files),
                                   max(entry.modified_ns for entry in small_files), '明细分组')
            children[relative] = [entry for entry in entries if isinstance(entry, DirectoryEntry) or entry.record.size >= 1024 * 1024] + [group]
            children[key] = small_files
        # 在工作线程中对数据引用排序，避免重建和排序界面条目。
    for index, entries in enumerate(children.values()):
        if index % 4096 == 0 and report('排序明细'):
            return None
        entries.sort(key=lambda entry: (-entry.size, entry.relative.casefold()))
    comparison = snapshot
    if previous is not None and not snapshot.complete:
        comparison = replace(snapshot, files={**previous.files, **snapshot.files})
    comparable = snapshot.complete and (previous is None or previous.complete)
    growth_children = {}
    for index, (key, entries) in enumerate(children.items() if include_growth else ()):
        if index % 4096 == 0 and report('排序增长明细'):
            return None
        growth_children[key] = sorted(entries, key=lambda entry: (-entry.delta, -entry.size, entry.relative.casefold()))
    cleanup = None
    if include_cleanup:
        if report('识别清理类别'):
            return None
        cleanup = classify_cleanup(snapshot, cancel=cancel)
        if cleanup is None:
            return None
    return FileAnalysis(snapshot, comparison, changes, directories, children, comparable, growth_children, cleanup)


def rank_files(analysis, mode, min_size=0, query=''):
    query = query.strip().casefold()
    def matches(change):
        return ((not query or query in change.relative.casefold())
                and (mode != 'large' or (change.count and change.size >= min_size))
                and (mode != 'growth' or change.delta > 0))
    key = (lambda entry: (entry.delta, entry.size, entry.relative)) if mode == 'growth' else (lambda entry: (entry.size, entry.relative))
    # 全局排行有意限制条目数量；完整内容仍可通过目录树浏览。
    return nlargest(500, (entry for entry in analysis.changes if matches(entry)), key=key)
