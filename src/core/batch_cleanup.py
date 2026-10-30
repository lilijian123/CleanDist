"""按真实扫描根准备清单；重复清理必须保留经完整内容核验的副本。"""
from dataclasses import dataclass
import os
from pathlib import Path
import threading

from .batch import record_key
from .files import (CleanupPlan, CleanupReport, CleanupResult, FileRecord, execute_cleanup,
                    free_disk_bytes, prepare_cleanup, validate_record)
from .storage import Cancelled, file_digest
from ..platforms import system


@dataclass(frozen=True)
class DuplicateKeeper:
    source_group: tuple
    selected: tuple
    keeper: FileRecord
    digest: bytes


@dataclass(frozen=True)
class BatchCleanupPlan:
    root: Path
    records: tuple
    risk_notes: tuple
    plans: tuple
    keepers: tuple


def _root_key(root):
    return os.path.normcase(os.path.abspath(root))


def _digest(record):
    digest, _ = file_digest(record, threading.Event(), False)
    if digest is None:
        raise ValueError('未完成完整内容核验，请重新扫描')
    return digest


def prepare_batch_cleanup(batch, records, *, risk_notes=(), duplicate_groups=(), readonly=()):
    risk_notes = tuple(risk_notes)
    roots = {_root_key(root): Path(os.path.abspath(root)) for root in batch.roots}
    blocked = {record_key(record) for record in readonly}

    def source(record):
        original = batch.files.get(record_key(record))
        if (original is None or _root_key(original.root) not in roots
                or _root_key(record.root) != _root_key(original.root)
                or record.relative != original.relative
                or record.observation != original.observation
                or (original.inode and record.fingerprint != original.fingerprint)):
            raise ValueError('文件不属于本次扫描记录，或扫描后已变化')
        return record

    selected = tuple(source(record) for record in records)
    keys = {record_key(record) for record in selected}
    if not selected:
        raise ValueError('请先勾选需要处理的文件')
    if len(keys) != len(selected):
        raise ValueError('清理清单包含重复文件')
    if keys & blocked:
        raise ValueError('高风险文件仅供查看，不支持清理')
    by_root = {}
    for record in selected:
        by_root.setdefault(_root_key(record.root), []).append(record)
    plans = tuple(prepare_cleanup(roots[root], items, risk_notes=risk_notes)
                  for root, items in by_root.items())
    bound = {record_key(record): record for plan in plans for record in plan.records}
    selected = tuple(bound[record_key(record)] for record in selected)
    groups = tuple(tuple(group) for group in duplicate_groups)
    keepers, grouped = [], set()
    for group in groups:
        group = tuple(source(record) for record in group)
        group_keys = {record_key(record) for record in group}
        if len(group_keys) < 2 or len(group_keys) != len(group) or grouped & group_keys:
            raise ValueError('重复文件分组包含重复路径或交叉分组')
        grouped.update(group_keys)
        touched = tuple(bound[record_key(record)] for record in group if record_key(record) in keys)
        if not touched:
            continue
        retained = tuple(record for record in group if record_key(record) not in keys)
        if not retained:
            raise ValueError('每组重复文件至少保留一份，不能全部勾选')
        # 保留原始分组的不可变快照，绑定真实文件身份；硬链接不是独立副本。
        group = tuple(validate_record(roots[_root_key(record.root)], record, bind=True) for record in group)
        if len({(record.device, record.inode) for record in group}) != len(group):
            raise ValueError('硬链接不能作为重复文件的独立保留副本')
        keeper = next(record for record in group if record_key(record) == record_key(retained[0]))
        digest = _digest(keeper)
        if any(record.size != keeper.size or _digest(record) != digest for record in touched):
            raise ValueError('重复文件内容已不一致，请重新扫描')
        if _digest(keeper) != digest:
            raise ValueError('保留副本在预览核验过程中变化，请重新扫描')
        keepers.append(DuplicateKeeper(group, touched, keeper, digest))
    if groups and not keys <= grouped:
        raise ValueError('勾选文件不在已确认的重复文件分组中')
    return BatchCleanupPlan(plans[0].root, selected, tuple(risk_notes), plans, tuple(keepers))


def _check_keeper(guard):
    if _digest(guard.keeper) != guard.digest:
        raise ValueError('保留副本内容已变化，请重新扫描')


def execute_batch_cleanup(plan, recycler=None):
    recycler = system.recycle if recycler is None else recycler
    roots = {_root_key(item.root) for item in plan.plans}
    before = free_disk_bytes(plan.root) if len(roots) == 1 else None
    guards, invalid = {}, {}
    for guard in plan.keepers:
        group_keys = tuple(record_key(record) for record in guard.selected)
        for key in group_keys:
            guards[key] = guard
        try:
            _check_keeper(guard)
            for record in guard.selected:
                if record.size != guard.keeper.size or _digest(record) != guard.digest:
                    raise ValueError('待清理文件内容已变化，请重新扫描')
            _check_keeper(guard)
        except (OSError, ValueError, Cancelled) as error:
            for key in group_keys:
                invalid[key] = str(error) or '完整内容核验已取消'
    results = []
    for record in plan.records:
        key = record_key(record)
        if key in invalid:
            results.append(CleanupResult(record, '跳过', invalid[key]))
            continue
        guard = guards.get(key)
        validation_error = []

        def guarded_recycle(path):
            # 在原清理器完成目标身份复核后、调用回收器前再次核对保留副本。
            if guard is not None:
                try:
                    _check_keeper(guard)
                    # 完整读取保留副本可能耗时，不能沿用读取前的目标身份检查。
                    validate_record(record.root, record)
                except (OSError, ValueError, Cancelled) as error:
                    validation_error.append(str(error) or '完整内容核验已取消')
                    raise ValueError(validation_error[0]) from error
            recycler(path)

        report = execute_cleanup(CleanupPlan(record.root, (record,), plan.risk_notes), guarded_recycle)
        result = report.results[0]
        if validation_error:
            result = CleanupResult(record, '跳过', validation_error[0])
        if guard is not None and result.state == '跳过':
            for member in guard.selected:
                invalid[record_key(member)] = result.message
        results.append(result)
    after = free_disk_bytes(plan.root) if len(roots) == 1 else None
    return CleanupReport(tuple(results), before, after)
