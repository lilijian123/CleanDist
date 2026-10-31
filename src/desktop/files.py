"""网页桌面的扫描快照、有限分页和核心清理安全边界。"""
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
import os
import math
from pathlib import Path
import secrets
import threading
import time

from ..core.ai import summarize_cleanup
from ..core.analysis import DirectoryEntry
from ..core.batch import ScanBatch, analyze_batch, build_matrix, record_key, scan_batch
from ..core.batch_cleanup import execute_batch_cleanup, prepare_batch_cleanup
from ..core.cleanup import CATEGORIES
from ..core.files import validate_plain_path
from ..core.storage import find_duplicates
from ..platforms import system


@dataclass(frozen=True)
class _Group:
    key: str
    name: str
    records: tuple
    risk: str = 'medium'
    reason: str = ''
    category: str = ''
    recommended: bool = False
    keep_one: bool = False


class FileService:
    def __init__(self):
        self._lock = threading.RLock()
        self._threads = []
        self._closed = False
        self._generation = 0
        self._cancel = threading.Event()
        self._duplicate_cancel = threading.Event()
        self._duplicate_job = 0
        self._busy = False
        self._status = '请选择扫描位置'
        self._progress = {'done': 0, 'total': 0, 'stage': ''}
        self._roots = []
        self._errors = []
        self._batch = None
        self._analysis = None
        self._metrics = dict(files=0, bytes=0, completed=False, targets=0, free=None)
        self._operation = dict(busy=False, message='')
        self._duplicate_state = self._empty_duplicates()
        self._duplicate_result = None
        self._group_index = {'cleanup': {}, 'large': {}, 'duplicates': {}}
        self._record_groups = {'cleanup': {}, 'large': {}, 'duplicates': {}}
        self._records = {}
        self._paths = {}
        self._readonly = set()
        self._row_cache = OrderedDict()
        self._matrices = {}
        self._matrix_parents = {}
        self._previews = {}
        self._reports = deque(maxlen=20)

    @staticmethod
    def _empty_duplicates():
        return dict(busy=False, stage='', done=0, total=0, groups=0, redundant=0, complete=False,
                    minimum=None, scope='')

    @staticmethod
    def _tool(tool):
        tool = 'duplicates' if tool == 'duplicate' else tool
        if tool not in ('cleanup', 'large', 'duplicates'):
            raise ValueError('未知的文件工具')
        return tool

    def _available(self):
        if self._closed:
            raise ValueError('文件服务已关闭')
        if self._operation['busy']:
            raise ValueError('正在核验或执行清理，请稍后重试')

    def _scope(self, scope):
        if not scope:
            return ''
        if not isinstance(scope, str):
            raise ValueError('无效的扫描范围')
        key = os.path.normcase(os.path.abspath(scope))
        if self._batch is None or key not in {os.path.normcase(str(root)) for root in self._batch.roots}:
            raise ValueError('范围必须是本次扫描根目录')
        return key

    @staticmethod
    def _in_scope(record, scope):
        return not scope or os.path.normcase(str(record.root)) == scope

    def _start(self, action):
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        thread = threading.Thread(target=action, daemon=True, name='desktop-files')
        self._threads.append(thread)
        thread.start()

    def state(self):
        with self._lock:
            return dict(generation=self._generation, busy=self._busy, status=self._status,
                        progress=dict(self._progress), metrics=dict(self._metrics), roots=list(self._roots),
                        errors=list(self._errors), categories=[dict(id=key, name=name) for key, name in CATEGORIES.items()],
                        duplicates={**self._duplicate_state, 'job': self._duplicate_job}, operation=dict(self._operation))

    def scan(self, roots, kind='folder'):
        if kind not in ('folder', 'disk'):
            raise ValueError('未知的扫描入口')
        if not isinstance(roots, (list, tuple)) or not roots:
            raise ValueError('请选择具体扫描目录')
        if kind == 'disk' and system.NAME != 'Windows':
            raise ValueError('当前平台不支持整盘扫描，请选择具体目录')
        paths = []
        for value in roots:
            if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
                raise ValueError('请选择具体扫描目录')
            path = Path(value)
            if not path.is_absolute():
                raise ValueError('扫描目录必须是绝对路径')
            path = Path(os.path.abspath(path))
            root = path == Path(path.anchor)
            if kind == 'folder' and root:
                raise ValueError('文件夹入口不接受根目录；Windows整盘扫描请使用磁盘入口')
            if kind == 'disk' and root and path not in system.list_disks():
                raise ValueError('请选择可用的磁盘根目录')
            validate_plain_path(path)
            if not path.is_dir():
                raise ValueError('请选择可访问的目录')
            paths.append(path)
        with self._lock:
            self._available()
            self._cancel.set()
            self._duplicate_cancel.set()
            self._generation += 1
            generation = self._generation
            cancel = self._cancel = threading.Event()
            self._duplicate_job += 1
            self._duplicate_state = self._empty_duplicates()
            self._duplicate_result = None
            self._previews.clear()
            self._batch = self._analysis = None
            self._records = {}
            self._paths = {}
            self._readonly = set()
            self._group_index = {'cleanup': {}, 'large': {}, 'duplicates': {}}
            self._record_groups = {'cleanup': {}, 'large': {}, 'duplicates': {}}
            self._row_cache.clear()
            self._matrices.clear()
            self._matrix_parents.clear()
            self._roots = [str(path) for path in paths]
            self._errors = []
            self._busy, self._status = True, '正在扫描…'
            self._progress = dict(done=0, total=0, stage='扫描')
            self._metrics = dict(files=0, bytes=0, completed=False, targets=len(paths), free=None)
            observed = {}

            def progress(root, update):
                with self._lock:
                    if generation == self._generation and not cancel.is_set():
                        observed[str(root)] = update.files
                        self._progress = dict(done=sum(observed.values()), total=0, stage=update.stage)

            def work():
                try:
                    batch = scan_batch(paths, cancel=cancel, progress=progress)
                    analysis = analyze_batch(batch, cancel=cancel)
                    with self._lock:
                        if generation != self._generation or cancel.is_set() or analysis is None:
                            return
                        self._install(batch, analysis)
                        self._status = '扫描完成' if batch.complete else '扫描完成，部分位置未确认'
                        self._progress = dict(done=len(batch.files), total=len(batch.files), stage='完成')
                except Exception as error:
                    with self._lock:
                        if generation == self._generation and not cancel.is_set():
                            self._errors = [str(error)]
                            self._status = '扫描未完成'
                finally:
                    with self._lock:
                        if generation == self._generation:
                            self._busy = False
            self._start(work)
            return self.state()

    def _install(self, batch, analysis):
        self._batch, self._analysis = batch, analysis
        self._roots = [str(root) for root in batch.roots]
        self._errors = list(batch.errors)
        self._records = dict(batch.files)
        self._paths = {key: str(record.path).casefold() for key, record in self._records.items()}
        self._readonly = {record_key(record) for record in analysis.cleanup.readonly_records()}
        cleanup = {_item.key: _Group(_item.key, _item.name, tuple(_item.records), _item.risk,
                    _item.reason, _item.category, _item.recommended) for _item in analysis.cleanup.items}
        large = _Group('large', '大文件', tuple(analysis.large_records), reason='大文件不一定无用；请确认用途后手动选择。')
        self._group_index = {'cleanup': cleanup, 'large': {'large': large} if large.records else {}, 'duplicates': {}}
        self._record_groups = {tool: {record_key(record): group for group in groups.values() for record in group.records}
                               for tool, groups in self._group_index.items()}
        self._row_cache.clear()
        self._matrices.clear()
        self._matrix_parents.clear()
        self._metrics = dict(files=len(batch.files), bytes=batch.total_bytes, completed=batch.complete,
                             targets=len(batch.roots), free=batch.free_bytes)

    def cancel(self):
        with self._lock:
            if self._operation['busy']:
                return self.state()
            self._cancel.set()
            self._cancel = threading.Event()
            self._duplicate_cancel.set()
            self._generation += 1
            self._duplicate_job += 1
            self._previews.clear()
            self._busy = False
            self._duplicate_state['busy'] = False
            if self._duplicate_result is None:
                self._duplicate_state['complete'] = False
            self._status = '已停止'
            return self.state()

    def _selection(self, tool, group, scope, query, minimum):
        tool = self._tool(tool)
        scope = self._scope(scope)
        if not isinstance(query, str) or not isinstance(group, str):
            raise ValueError('无效的文件筛选条件')
        if type(minimum) not in (int, float) or minimum < 0:
            raise ValueError('文件大小下限必须非负')
        query = query.strip().casefold()
        key = tool, group, scope, query, minimum
        if key not in self._row_cache:
            groups = self._group_index[tool]
            if group and group not in groups:
                raise ValueError('文件分组已失效，请刷新')
            source = groups[group].records if group else (record for item in groups.values() for record in item.records)
            records = tuple(sorted((record for record in source if self._in_scope(record, scope)
                                   and record.size >= minimum and (not query or query in self._paths[record_key(record)])),
                                   key=lambda record: (-record.size, record_key(record))))
            self._row_cache[key] = records
            if len(self._row_cache) > 32:
                self._row_cache.popitem(last=False)
        self._row_cache.move_to_end(key)
        return self._row_cache[key]

    def _metadata(self, tool, record):
        key = record_key(record)
        group = self._record_groups[tool][key]
        classified = self._analysis.cleanup.by_file.get(key) if self._analysis else None
        if key in self._readonly and classified is not None:
            return group, 'high', classified.reason, True
        return group, group.risk, group.reason, key in self._readonly

    def groups(self, tool='cleanup', scope='', risk='', query='', category=''):
        with self._lock:
            tool = self._tool(tool)
            records = self._selection(tool, '', scope, query, 0)
            by_group = {}
            for record in records:
                group, item_risk, _, _ = self._metadata(tool, record)
                if not risk or risk == item_risk:
                    by_group.setdefault(group.key, []).append(record)
            result = []
            for key, group in self._group_index[tool].items():
                if category and group.category != category:
                    continue
                selected = by_group.get(key)
                if selected:
                    result.append(dict(id=key, name=group.name, category=group.category, risk=group.risk,
                                       reason=group.reason, recommended=group.recommended, count=len(selected),
                                       bytes=sum(record.size for record in selected),
                                       readonly=all(record_key(record) in self._readonly for record in selected), keep_one=group.keep_one))
            return result

    def rows(self, tool='cleanup', group='', scope='', query='', minimum=0, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError('无效的分页参数')
        with self._lock:
            tool = self._tool(tool)
            records = self._selection(tool, group, scope, query, minimum)
            rows = []
            for record in records[offset:offset + min(limit, 200)]:
                item, risk, reason, readonly = self._metadata(tool, record)
                rows.append(dict(id=record_key(record), name=record.path.name, path=str(record.path), bytes=record.size,
                                 modified=record.modified_ns, risk=risk, reason=reason, readonly=readonly, group=item.key))
            return dict(total=len(records), rows=rows)

    def _scoped_batch(self, scope):
        scope = self._scope(scope)
        if self._batch is None:
            raise ValueError('请先完成扫描')
        if not scope:
            return self._batch
        return ScanBatch(tuple(snapshot for snapshot in self._batch.snapshots
                               if os.path.normcase(str(snapshot.root)) == scope), self._batch.errors,
                         self._batch.elapsed, self._batch.cancelled)

    def duplicates(self, minimum=1024**2, scope=''):
        if type(minimum) is not int or minimum < 1:
            raise ValueError('最小文件大小必须大于0')
        with self._lock:
            self._available()
            if self._busy:
                raise ValueError('请等待扫描完成')
            batch = self._scoped_batch(scope)
            self._duplicate_cancel.set()
            self._duplicate_job += 1
            job, generation = self._duplicate_job, self._generation
            cancel = self._duplicate_cancel = threading.Event()
            self._duplicate_result = None
            self._previews.clear()
            self._group_index['duplicates'] = {}
            self._record_groups['duplicates'] = {}
            self._row_cache.clear()
            self._duplicate_state = self._empty_duplicates()
            self._duplicate_state.update(busy=True, stage='比对文件内容', minimum=minimum, scope=scope)

            def current():
                return job == self._duplicate_job and generation == self._generation and not cancel.is_set()

            def progress(stage, done, total):
                with self._lock:
                    if current():
                        self._duplicate_state.update(stage=stage, done=done, total=total)

            def work():
                try:
                    result = find_duplicates(batch, cancel, progress, minimum_bytes=minimum)
                    with self._lock:
                        if not current():
                            return
                        self._duplicate_result = result
                        groups = {f'duplicate/{number}': _Group(f'duplicate/{number}', f'重复组 {number+1:03} · {records[0].path.name}',
                                  tuple(self._records[record_key(record)] for record in records),
                                  reason='已确认内容相同，但用途可能不同；每组至少保留一份。', keep_one=True)
                                  for number, records in enumerate(result.groups)}
                        self._group_index['duplicates'] = groups
                        self._record_groups['duplicates'] = {record_key(record): group for group in groups.values() for record in group.records}
                        self._row_cache.clear()
                        self._duplicate_state.update(stage='比对完成', groups=len(result.groups), redundant=result.redundant_bytes,
                                                     complete=result.complete, done=result.checked, total=result.checked)
                        self._errors.extend(result.errors)
                except Exception as error:
                    with self._lock:
                        if current():
                            self._errors.append(str(error))
                            self._duplicate_state['stage'] = '比对未完成'
                finally:
                    with self._lock:
                        if current():
                            self._duplicate_state['busy'] = False
            self._start(work)
            return self.state()

    def cancel_duplicates(self):
        with self._lock:
            self._available()
            self._duplicate_cancel.set()
            self._duplicate_job += 1
            self._duplicate_state = self._empty_duplicates()
            self._duplicate_state['stage'] = '已停止比对'
            self._duplicate_result = None
            self._group_index['duplicates'] = {}
            self._record_groups['duplicates'] = {}
            self._row_cache.clear()
            self._previews.clear()
            return self.state()

    def matrix(self, scope='', relative=''):
        with self._lock:
            if self._busy:
                raise ValueError('请等待扫描完成')
            batch = self._scoped_batch(scope)
            scope = self._scope(scope)
            generation = self._generation
            analysis = self._matrices.get(scope)
            parents = self._matrix_parents.get(scope, {})
            cancel = self._cancel
        if analysis is None:
            analysis = build_matrix(batch, cancel=cancel)
            with self._lock:
                if generation != self._generation or analysis is None:
                    raise ValueError('扫描结果已更新，请刷新矩阵')
                self._matrices[scope] = analysis
                self._matrix_parents[scope] = {entry.relative: parent for parent, entries in analysis.children.items()
                                              for entry in entries if isinstance(entry, DirectoryEntry)}
                parents = self._matrix_parents[scope]
        if not isinstance(relative, str) or relative not in analysis.children:
            raise ValueError('矩阵目录已失效，请返回根目录')
        nodes = []
        entries = analysis.children[relative]
        displayed = entries if len(entries) <= 200 else entries[:199]
        for entry in displayed:
            directory = isinstance(entry, DirectoryEntry)
            nodes.append(dict(id=entry.relative if directory else record_key(entry.record), name=entry.name,
                              bytes=entry.size, count=entry.count, directory=directory,
                              relative=entry.relative if directory else ''))
        if len(entries) > 200:
            remainder = entries[199:]
            nodes.append(dict(id='', name='其它', bytes=sum(entry.size for entry in remainder),
                              count=sum(entry.count for entry in remainder), directory=False, relative=''))
        parent = parents.get(relative, '')
        with self._lock:
            if generation != self._generation:
                raise ValueError('扫描结果已更新，请刷新矩阵')
        return dict(path=analysis.directories[relative].name if relative in analysis.directories else '文件明细',
                    relative=relative, parent=parent, nodes=nodes)

    def _resolve_ids(self, selection, tool):
        if not isinstance(selection, dict):
            return selection
        if set(selection) - {'groups', 'ids', 'excluded', 'recommended', 'scope', 'query', 'risk', 'minimum', 'category'}:
            raise ValueError('无效的选择描述')
        group_ids, explicit, excluded = (selection.get(field, []) for field in ('groups', 'ids', 'excluded'))
        if any(not isinstance(items, list) or any(not isinstance(key, str) for key in items)
               for items in (group_ids, explicit, excluded)) or type(selection.get('recommended', False)) is not bool:
            raise ValueError('无效的选择描述')
        scope = self._scope(selection.get('scope', ''))
        query, risk, minimum = selection.get('query', ''), selection.get('risk', ''), selection.get('minimum', 0)
        category = selection.get('category', '')
        if (not isinstance(query, str) or risk not in ('', 'low', 'medium', 'high')
                or not isinstance(category, str)
                or type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 0):
            raise ValueError('无效的选择筛选条件')
        query = query.strip().casefold()
        def matches(record):
            key = record_key(record)
            return (self._in_scope(record, scope) and record.size >= minimum
                    and (not query or query in self._paths[key])
                    and (not category or self._record_groups[tool][key].category == category)
                    and (not risk or self._metadata(tool, record)[1] == risk))
        groups, members = self._group_index[tool], self._record_groups[tool]
        if any(key not in groups for key in group_ids):
            raise ValueError('文件分组已失效，请刷新')
        if any(key not in members or not matches(self._records[key]) for key in explicit):
            raise ValueError('文件不属于当前工具或扫描范围')
        if any(key not in members or not self._in_scope(self._records[key], scope) for key in excluded):
            raise ValueError('排除项不属于当前工具或扫描范围')
        chosen = {}
        excluded = set(excluded)
        for group in groups.values():
            if group.key not in group_ids and not (selection.get('recommended') and group.recommended):
                continue
            records = tuple(record for record in group.records if matches(record))
            # 整组选择会避开只读文件；明确传入只读文件仍在preview拒绝。
            candidates = [record for record in records if record_key(record) not in self._readonly
                          and record_key(record) not in excluded]
            if group.keep_one and len(candidates) == len(group.records):
                candidates = candidates[1:]
            chosen.update((record_key(record), None) for record in candidates)
        chosen.update((key, None) for key in explicit if key not in excluded)
        return list(chosen)

    def preview(self, ids, tool='cleanup'):
        with self._lock:
            self._available()
            if self._busy:
                raise ValueError('请等待扫描完成')
            tool = self._tool(tool)
            ids = self._resolve_ids(ids, tool)
            if not isinstance(ids, (tuple, list)) or not ids or any(not isinstance(key, str) for key in ids):
                raise ValueError('请选择已扫描文件')
            if len(set(ids)) != len(ids):
                raise ValueError('清理清单包含重复文件')
            members = self._record_groups[tool]
            if any(key not in self._records or key not in members for key in ids):
                raise ValueError('文件不属于当前工具的已扫描记录')
            batch, generation = self._batch, self._generation
            records = tuple(self._records[key] for key in ids)
            notes = []
            for record in records:
                group, risk, reason, readonly = self._metadata(tool, record)
                if readonly:
                    raise ValueError('高风险文件仅供查看，不支持清理')
                notes.append((record_key(record), group.name, risk, reason))
            duplicate_groups = self._duplicate_result.groups if tool == 'duplicates' and self._duplicate_result else ()
            readonly = tuple(self._records[key] for key in self._readonly)
            self._operation = dict(busy=True, message='正在核验清理清单…')
        try:
            plan = prepare_batch_cleanup(batch, records, risk_notes=tuple(notes), duplicate_groups=duplicate_groups, readonly=readonly)
            with self._lock:
                if generation != self._generation or self._closed:
                    raise ValueError('扫描结果已更新，请重新预览')
                token = secrets.token_urlsafe(32)
                medium = any(note[2] == 'medium' for note in notes)
                # 仅保留最近一次确认清单，避免长期保留旧文件身份快照。
                self._previews = {token: (generation, plan, medium)}
                distinct = dict.fromkeys((name, risk, reason) for _, name, risk, reason in notes)
                return dict(token=token, count=len(plan.records), bytes=sum(record.size for record in plan.records),
                            medium=medium, notes=[dict(name=name, risk=risk, reason=reason) for name, risk, reason in distinct],
                            files=[dict(path=str(record.path), bytes=record.size) for record in plan.records[:100]])
        finally:
            with self._lock:
                self._operation = dict(busy=False, message='清理仅移入回收站，同盘回收通常不会立即释放空间。')

    def execute(self, token, confirm_medium=False):
        with self._lock:
            self._available()
            if not isinstance(token, str) or token not in self._previews:
                raise ValueError('清理预览已失效，请重新预览')
            generation, plan, medium = self._previews[token]
            if generation != self._generation:
                self._previews.pop(token, None)
                raise ValueError('扫描结果已更新，请重新预览')
            if medium and confirm_medium is not True:
                raise ValueError('中风险文件需要明确确认')
            self._previews.pop(token)
            self._duplicate_cancel.set()
            self._duplicate_job += 1
            self._duplicate_state = self._empty_duplicates()
            self._operation = dict(busy=True, message='正在移入回收站…')
            batch = self._batch
        try:
            result = execute_batch_cleanup(plan)
            counts = {state: sum(item.state == state for item in result.results) for state in ('成功', '跳过', '失败')}
            message = f"已移入回收站 {counts['成功']} 项 · 跳过 {counts['跳过']} · 失败 {counts['失败']}；同盘回收通常不会立即释放空间。"
            report = dict(timestamp=time.time(), count=len(result.results), success=counts['成功'], skipped=counts['跳过'],
                          failed=counts['失败'], bytes=result.recycled_bytes, before_free=result.before_free,
                          after_free=result.after_free, message=message,
                          results_truncated=len(result.results) > 200,
                          results=[dict(id=record_key(item.record), path=str(item.record.path), bytes=item.record.size,
                                        state=item.state, message=item.message) for item in result.results[:200]])
            with self._lock:
                self._reports.appendleft({**report, 'results': [dict(item) for item in report['results']]})
            excluded = {record_key(item.record) for item in result.results if item.state in ('成功', '跳过')}
            snapshots = tuple(replace(snapshot, files={key: record for key, record in snapshot.files.items()
                              if record_key(record) not in excluded}) for snapshot in batch.snapshots)
            updated = ScanBatch(snapshots, batch.errors, batch.elapsed, batch.cancelled)
            analysis = analyze_batch(updated)
            with self._lock:
                if analysis is not None:
                    self._install(updated, analysis)
                self._duplicate_result = None
                self._status = '清理结果已更新'
                self._operation['message'] = message
            return report
        finally:
            with self._lock:
                self._operation['busy'] = False

    def reports(self):
        with self._lock:
            # 返回独立容器，调用方无法改写服务端审计记录。
            return [{**report, 'results': [dict(item) for item in report['results']]} for report in self._reports]

    def summary(self, tool='cleanup', group='', id='', scope=''):
        with self._lock:
            tool = self._tool(tool)
            record = None
            if id:
                record = self._records.get(id)
                if record is None or id not in self._record_groups[tool] or not self._in_scope(record, self._scope(scope)):
                    raise ValueError('文件记录已失效，请刷新')
                original = self._record_groups[tool][id]
                if group and original.key != group:
                    raise ValueError('文件不属于指定分组')
                _, risk, reason, readonly = self._metadata(tool, record)
                classified = self._analysis.cleanup.by_file.get(id)
                target = replace(original, risk=risk, reason=reason,
                                 category=classified.category if readonly and classified else original.category)
            else:
                records = self._selection(tool, group, scope, '', 0)
                if not group or group not in self._group_index[tool]:
                    raise ValueError('请选择具体文件或分组')
                target = replace(self._group_index[tool][group], records=records)
                readonly = any(record_key(item) in self._readonly for item in records)
            return summarize_cleanup(target, record, platform=system.NAME,
                                     complete=bool(self._batch and self._batch.complete), read_only=readonly)

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            self._duplicate_cancel.set()
            self._generation += 1
            self._duplicate_job += 1
            self._previews.clear()
            self._busy = False
            self._duplicate_state['busy'] = False
