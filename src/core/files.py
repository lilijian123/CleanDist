"""Read-only directory snapshots and explicit, revalidated recycle-bin operations."""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import threading
import time


MAX_FILES = 50_000


@dataclass(frozen=True)
class FileRecord:
    path: Path
    relative: str
    size: int
    modified_ns: int
    device: int
    inode: int
    created_ns: int

    @classmethod
    def from_stat(cls, path, root, info):
        return cls(path, path.relative_to(root).as_posix(), info.st_size,
                   info.st_mtime_ns, info.st_dev, info.st_ino, info.st_ctime_ns)

    @property
    def fingerprint(self):
        return self.size, self.modified_ns, self.device, self.inode, self.created_ns


@dataclass(frozen=True)
class DirectorySnapshot:
    root: Path
    files: dict
    complete: bool
    skipped: int
    errors: tuple
    cancelled: bool
    truncated: bool
    timestamp: float
    elapsed: float
    free_bytes: int | None

    @property
    def total_bytes(self):
        return sum(record.size for record in self.files.values())


@dataclass(frozen=True)
class FileChange:
    record: FileRecord
    state: str
    delta: int


@dataclass(frozen=True)
class CleanupPlan:
    root: Path
    records: tuple


@dataclass(frozen=True)
class CleanupResult:
    record: FileRecord
    state: str
    message: str


@dataclass(frozen=True)
class CleanupReport:
    results: tuple
    before_free: int | None
    after_free: int | None

    @property
    def recycled_bytes(self):
        return sum(r.record.size for r in self.results if r.state == '成功')


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def validate_plain_path(path):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if is_link(info):
            raise ValueError(f'不处理符号链接或重解析点：{part}')
    return path


def disk_free(root):
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


def scan_directory(root, cancel=None, limit=MAX_FILES):
    root = validate_plain_path(root)
    if not root.is_dir():
        raise ValueError('请选择一个可访问的目录')
    if limit <= 0:
        raise ValueError('扫描数量限制必须大于0')
    cancel = cancel or threading.Event()
    start = time.monotonic()
    records, errors, stack = {}, [], [root]
    skipped = 0
    truncated = False
    while stack and not cancel.is_set() and not truncated:
        directory = stack.pop()
        try:
            # Recheck directories before descent, not only at discovery time.
            if is_link(directory.lstat()):
                skipped += 1
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel.is_set():
                        break
                    path = Path(entry.path)
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if is_link(info):
                            skipped += 1
                        elif stat.S_ISDIR(info.st_mode):
                            stack.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            if len(records) >= limit:
                                truncated = True
                                break
                            # Windows DirEntry.stat() can cache zero dev/ino values.
                            # Obtain a live identity so preview and execution compare the same facts.
                            if os.name == 'nt':
                                info = path.lstat()
                                if is_link(info) or not stat.S_ISREG(info.st_mode):
                                    skipped += 1
                                    continue
                            record = FileRecord.from_stat(path, root, info)
                            records[record.relative] = record
                        else:
                            skipped += 1
                    except OSError as error:
                        skipped += 1
                        if len(errors) < 5:
                            errors.append(f'{path}: {error}')
        except OSError as error:
            if directory == root:
                raise ValueError(f'无法读取所选目录：{error}') from error
            skipped += 1
            if len(errors) < 5:
                errors.append(f'{directory}: {error}')
    cancelled = cancel.is_set()
    return DirectorySnapshot(root, records, not (skipped or truncated or cancelled),
                             skipped, tuple(errors), cancelled, truncated, time.time(),
                             time.monotonic() - start, disk_free(root))


def diff_snapshots(previous, current):
    if previous is not None and previous.root != current.root:
        previous = None
    changes = []
    for name, record in current.files.items():
        before = previous.files.get(name) if previous else None
        delta = record.size - before.size if before else (record.size if previous else 0)
        if previous is None:
            state = '基线'
        elif before is None:
            state = '新增'
        elif delta:
            state = '增长' if delta > 0 else '缩小'
        else:
            state = '已修改' if before.fingerprint != record.fingerprint else '未变化'
        changes.append(FileChange(record, state, delta))
    if previous:
        for name, record in previous.files.items():
            if name not in current.files:
                changes.append(FileChange(record, '已删除' if current.complete else '未确认',
                                          -record.size if current.complete else 0))
    priorities = {'增长': 0, '新增': 1, '已修改': 2, '缩小': 3, '已删除': 4, '未确认': 5}
    return sorted(changes, key=lambda c: (priorities.get(c.state, 6), -abs(c.delta), -c.record.size))


def validate_record(root, record):
    root = validate_plain_path(root)
    path = Path(os.path.abspath(record.path))
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError('文件不在本次选择的目录中') from error
    if not relative.parts or relative.as_posix() != record.relative:
        raise ValueError('文件路径与扫描记录不一致')
    validate_plain_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or is_link(info):
        raise ValueError('仅支持普通文件')
    if FileRecord.from_stat(path, root, info).fingerprint != record.fingerprint:
        raise ValueError('文件已变化，请重新扫描和预览')
    if not record.inode:
        raise ValueError('无法确认文件身份，本版不执行清理')


def prepare_cleanup(root, records):
    root = validate_plain_path(root)
    records = tuple(records)
    if not records:
        raise ValueError('请先勾选需要处理的文件')
    if len({os.path.normcase(str(r.path)) for r in records}) != len(records):
        raise ValueError('清理清单包含重复文件')
    for record in records:
        validate_record(root, record)
    return CleanupPlan(root, records)


def execute_cleanup(plan, recycler=None):
    if recycler is None:
        from send2trash import send2trash
        recycler = lambda path: send2trash(str(path))
    before = disk_free(plan.root)
    results = []
    for record in plan.records:
        try:
            validate_record(plan.root, record)
        except (OSError, ValueError) as error:
            results.append(CleanupResult(record, '跳过', str(error)))
            continue
        try:
            recycler(record.path)
            if os.path.lexists(record.path):
                raise OSError('回收站调用后原路径仍存在，未计为成功')
            results.append(CleanupResult(record, '成功', '已移入回收站'))
        except Exception as error:
            results.append(CleanupResult(record, '失败', str(error)))
    return CleanupReport(tuple(results), before, disk_free(plan.root))
