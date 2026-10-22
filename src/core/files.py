"""只读目录快照，以及经用户确认并重新校验的回收站操作。"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import threading
import time

from ..platforms import system


@dataclass(frozen=True, slots=True)
class FileRecord:
    root: Path
    relative: str
    size: int
    modified_ns: int
    device: int
    inode: int
    ctime_ns: int  # 与 stat.st_ctime_ns 一致，其含义由平台决定。

    @classmethod
    def from_stat(cls, path, root, info):
        return cls(root, path.relative_to(root).as_posix(), info.st_size,
                   info.st_mtime_ns, info.st_dev, info.st_ino, info.st_ctime_ns)

    @property
    def path(self):
        return self.root / self.relative

    @property
    def fingerprint(self):
        return self.size, self.modified_ns, self.device, self.inode, self.ctime_ns

    @property
    def observation(self):
        return self.size, self.modified_ns, self.ctime_ns


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
    directories: tuple = ()
    backend: str = 'python'

    @property
    def total_bytes(self):
        return sum(record.size for record in self.files.values())


@dataclass(frozen=True, slots=True)
class FileChange:
    record: FileRecord
    state: str
    delta: int

    @property
    def relative(self):
        return self.record.relative

    @property
    def name(self):
        return self.record.relative.rsplit('/', 1)[-1]

    @property
    def size(self):
        return self.record.size if self.count else 0

    @property
    def count(self):
        return int(self.state not in ('已删除', '未确认'))

    @property
    def modified_ns(self):
        return self.record.modified_ns


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
        return sum(result.record.size for result in self.results if result.state == '成功')


def validate_plain_path(path):
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        info = part.lstat()
        if system.is_link(info):
            raise ValueError(f'不处理符号链接或重解析点：{part}')
    return path


def free_disk_bytes(root):
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


@dataclass(frozen=True, slots=True)
class ScanProgress:
    stage: str
    files: int
    directories: int
    elapsed: float
    backend: str


def scan_directory(root, cancel=None, limit=None, progress=None, *, backend='auto'):
    root = validate_plain_path(root)
    if not root.is_dir():
        raise ValueError('请选择一个可访问的目录')
    if limit is not None and limit <= 0:
        raise ValueError('扫描数量限制必须大于0')
    cancel = cancel or threading.Event()
    if backend != 'python':
        native_snapshot = system.scan_native(root, cancel, limit, progress)
        if native_snapshot is not None:
            return native_snapshot
    start = time.monotonic()
    records, errors, stack = {}, [], [root]
    directories = []
    skipped = 0
    truncated = False
    last_report = 0
    prefix_length = len(str(root).rstrip('\\/')) + 1
    while stack and not cancel.is_set() and not truncated:
        directory = stack.pop()
        try:
            # 进入目录前再次校验，不能仅依赖发现目录时的检查。
            if system.is_link(directory.lstat()):
                skipped += 1
                continue
            if directory != root:
                directories.append(directory.relative_to(root).as_posix())
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel.is_set():
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if system.is_link(info):
                            skipped += 1
                        elif stat.S_ISDIR(info.st_mode):
                            stack.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            if limit is not None and len(records) >= limit:
                                truncated = True
                                break
                            # 枚举可低成本获取文件大小和时间；仅对用户选中的
                            # 待清理文件读取并绑定当前文件身份。
                            relative = entry.path[prefix_length:].replace(os.sep, '/')
                            record = FileRecord(root, relative, info.st_size, info.st_mtime_ns,
                                                info.st_dev, info.st_ino, info.st_ctime_ns)
                            records[record.relative] = record
                        else:
                            skipped += 1
                    except OSError as error:
                        skipped += 1
                        if len(errors) < 5:
                            errors.append(f'{entry.path}: {error}')
            now = time.monotonic()
            if progress and now - last_report >= .25:
                progress(ScanProgress('兼容扫描', len(records), len(directories), now - start, 'python'))
                last_report = now
        except OSError as error:
            if directory == root:
                raise ValueError(f'无法读取所选目录：{error}') from error
            skipped += 1
            if len(errors) < 5:
                errors.append(f'{directory}: {error}')
    cancelled = cancel.is_set()
    if progress:
        progress(ScanProgress('扫描完成', len(records), len(directories), time.monotonic() - start, 'python'))
    return DirectorySnapshot(root, records, not (skipped or truncated or cancelled),
                             skipped, tuple(errors), cancelled, truncated, time.time(),
                             time.monotonic() - start, free_disk_bytes(root), tuple(directories))


def diff_snapshots(previous, current, sort=True):
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
    if not sort:
        return changes
    priorities = {'增长': 0, '新增': 1, '已修改': 2, '缩小': 3, '已删除': 4, '未确认': 5}
    return sorted(changes, key=lambda change: (priorities.get(change.state, 6), -abs(change.delta), -change.record.size))


def validate_record(root, record, *, bind=False):
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
    if not stat.S_ISREG(info.st_mode) or system.is_link(info):
        raise ValueError('仅支持普通文件')
    live = FileRecord.from_stat(path, root, info)
    if live.observation != record.observation or (record.inode and live.fingerprint != record.fingerprint):
        raise ValueError('文件已变化，请重新扫描和预览')
    if not live.inode or (not bind and not record.inode):
        raise ValueError('无法确认文件身份，本版不执行清理')
    return live


def prepare_cleanup(root, records):
    root = validate_plain_path(root)
    records = tuple(records)
    if not records:
        raise ValueError('请先勾选需要处理的文件')
    if len({os.path.normcase(str(record.path)) for record in records}) != len(records):
        raise ValueError('清理清单包含重复文件')
    return CleanupPlan(root, tuple(validate_record(root, record, bind=True) for record in records))


def execute_cleanup(plan, recycler=None):
    if recycler is None:
        recycler = system.recycle
    before = free_disk_bytes(plan.root)
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
    return CleanupReport(tuple(results), before, free_disk_bytes(plan.root))
