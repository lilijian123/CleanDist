"""只读空间统计和分阶段内容比对，不提供自动删除。"""
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import os
import shutil
from pathlib import Path
import stat
import threading

from .files import FileRecord, validate_plain_path, validate_record
from ..platforms import system


@dataclass
class UsageResult:
    logical_bytes: int = 0
    allocated_bytes: int = 0
    slack_bytes: int = 0
    saved_bytes: int = 0
    unique_files: int = 0
    hardlinks: int = 0
    small_files: int = 0
    skipped: int = 0
    cancelled: bool = False
    snapshot_complete: bool = True
    directories: dict = field(default_factory=dict)
    buckets: list = field(default_factory=lambda: [0] * 5)
    errors: list = field(default_factory=list)
    volume: object = None

    @property
    def complete(self):
        return self.snapshot_complete and not self.cancelled and not self.skipped


@dataclass
class DuplicateResult:
    groups: tuple = ()
    hardlinks: int = 0
    skipped: int = 0
    checked: int = 0
    cancelled: bool = False
    snapshot_complete: bool = True
    errors: list = field(default_factory=list)

    @property
    def redundant_bytes(self):
        return sum(group[0].size * (len(group) - 1) for group in self.groups)

    @property
    def complete(self):
        return self.snapshot_complete and not self.cancelled and not self.skipped


class Cancelled(Exception):
    """停止当前读取，不将未确认内容计为重复。"""


def live_record(record, check_parents=True):
    root = Path(os.path.abspath(record.root))
    path = Path(os.path.abspath(record.path))
    if path.relative_to(root).as_posix() != record.relative:
        raise ValueError('文件路径与快照不一致')
    if check_parents:
        validate_plain_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or system.is_link(info):
        raise ValueError('不读取链接或特殊文件')
    live = FileRecord.from_stat(path, root, info)
    if live.observation != record.observation or (record.inode and live.fingerprint != record.fingerprint):
        raise ValueError('文件已变化，请重新扫描')
    if not live.inode:
        raise ValueError('无法确认文件身份')
    return live


@contextmanager
def checked_file(record, cancel, metadata=False):
    if cancel.is_set():
        raise Cancelled()
    live = live_record(record)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    opened = system.open_metadata(live.path) if metadata else os.fdopen(os.open(live.path, flags), 'rb')
    with opened as stream:
        current = FileRecord.from_stat(live.path, live.root, os.fstat(stream.fileno()))
        if current.fingerprint != live.fingerprint:
            raise ValueError('文件身份在打开时发生变化')
        yield stream, live
        current = FileRecord.from_stat(live.path, live.root, os.fstat(stream.fileno()))
        if current.fingerprint != live.fingerprint:
            raise ValueError('文件在读取过程中发生变化')
        # 结束时核对路径身份；已打开句柄的前后身份和时间必须同时稳定。
        live_record(live, check_parents=False)


def note_error(result, record, error):
    result.skipped += 1
    if len(result.errors) < 8:
        result.errors.append(f'{record.relative}：{error}')


def analyze_usage(snapshot, cancel=None, progress=None):
    cancel = cancel or threading.Event()
    result = UsageResult(snapshot_complete=snapshot.complete)
    try:
        result.volume = shutil.disk_usage(snapshot.root)
    except OSError:
        pass
    seen = set()
    total = len(snapshot.files)
    for index, record in enumerate(snapshot.files.values()):
        if cancel.is_set():
            break
        try:
            with checked_file(record, cancel, metadata=True) as (stream, live):
                allocated = system.allocated_bytes(stream)
            identity = live.device, live.inode
            if identity in seen:
                result.hardlinks += 1
                continue
            seen.add(identity)
            result.unique_files += 1
            result.logical_bytes += live.size
            result.allocated_bytes += allocated
            result.slack_bytes += max(0, allocated - live.size)
            result.saved_bytes += max(0, live.size - allocated)
            result.small_files += live.size < 1024 * 1024
            bucket = next((i for i, limit in enumerate((4096, 1024**2, 10 * 1024**2, 100 * 1024**2))
                           if live.size < limit), 4)
            result.buckets[bucket] += 1
            key = live.relative.split('/')[0] + '/' if '/' in live.relative else '根目录文件'
            entry = result.directories.setdefault(key, [0, 0, 0])
            entry[0] += live.size
            entry[1] += allocated
            entry[2] += 1
        except Cancelled:
            break
        except (OSError, ValueError) as error:
            note_error(result, record, error)
        if progress and index % 64 == 0:
            progress('读取分配空间', index + 1, total)
    result.cancelled = cancel.is_set()
    result.directories = dict(sorted(result.directories.items(), key=lambda item: -item[1][1]))
    return result


def file_digest(record, cancel, sample, seen=None):
    digest = hashlib.sha256()
    with checked_file(record, cancel) as (stream, live):
        if seen is not None and (live.device, live.inode) in seen:
            return None, live
        if sample:
            for offset in sorted({0, max(0, live.size // 2 - 32768), max(0, live.size - 65536)}):
                if cancel.is_set():
                    raise Cancelled()
                stream.seek(offset)
                digest.update(stream.read(65536))
        else:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    return digest.digest(), live


def find_duplicates(snapshot, cancel=None, progress=None):
    cancel = cancel or threading.Event()
    result = DuplicateResult(snapshot_complete=snapshot.complete)
    sizes = defaultdict(list)
    for record in snapshot.files.values():
        if cancel.is_set():
            result.cancelled = True
            return result
        if record.size:
            sizes[record.size].append(record)
    candidates = [record for group in sizes.values() if len(group) > 1 for record in group]
    samples = defaultdict(list)
    seen = set()
    for index, record in enumerate(sorted(candidates, key=lambda record: record.relative.casefold())):
        if cancel.is_set():
            break
        try:
            digest, live = file_digest(record, cancel, True, seen)
            identity = live.device, live.inode
            if digest is None:
                result.hardlinks += 1
                continue
            seen.add(identity)
            samples[(live.size, digest)].append(live)
            result.checked += 1
        except Cancelled:
            break
        except (OSError, ValueError) as error:
            note_error(result, record, error)
        if progress and index % 16 == 0:
            progress('筛选内容指纹', index + 1, len(candidates))
    hashes = defaultdict(list)
    full = [record for group in samples.values() if len(group) > 1 for record in group]
    for index, record in enumerate(full):
        if cancel.is_set():
            break
        try:
            if progress:
                progress(f'完整比对 · {record.relative}', index, len(full))
            digest, live = file_digest(record, cancel, False)
            hashes[(live.size, digest)].append(live)
        except Cancelled:
            break
        except (OSError, ValueError) as error:
            note_error(result, record, error)
    confirmed = []
    for group in hashes.values():
        if len(group) < 2 or cancel.is_set():
            continue
        valid = []
        for record in group:
            try:
                valid.append(validate_record(record.root, record))
            except (OSError, ValueError) as error:
                note_error(result, record, error)
        if len(valid) > 1:
            confirmed.append(tuple(valid))
    result.cancelled = cancel.is_set()
    result.groups = () if result.cancelled else tuple(sorted(
        confirmed, key=lambda group: (-group[0].size * (len(group) - 1), group[0].relative)))
    return result
