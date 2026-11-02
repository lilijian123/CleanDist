"""目录监测阈值与仅管理本应用记录的同卷回收站。"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import threading
import time

from ..core.cleanup import classify_cleanup
from ..core.files import scan_directory, validate_plain_path
from ..platforms import system


def _inside(path, root):
    return path == root or root in path.parents


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class DirectoryService:
    def __init__(self, state_dir=None):
        data = (Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) if os.name == 'nt'
                else Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')))
        self._state_dir = Path(os.path.abspath(state_dir or data / 'CleanDist/governance'))
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir = validate_plain_path(self._state_dir).resolve()
        self._file = self._state_dir / 'directories.json'
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._roots, self._vault, self._snapshots, self._watchers = {}, {}, {}, {}
        self._pending, self._previews, self._errors = {}, {}, []
        self._generation = 0
        self._writable = True
        self._message = '监测阈值不会阻止写入；超限仅提示。'
        if self._file.exists():
            try:
                saved = json.loads(self._file.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or not isinstance(saved.get('roots', []), list) or not isinstance(saved.get('vault', []), list):
                    raise ValueError('配置结构无效')
                for item in saved.get('roots', []):
                    if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                            or not isinstance(item.get('path'), str) or type(item.get('limit_bytes')) is not int):
                        raise ValueError('监测目录配置无效')
                for item in saved.get('vault', []):
                    if (not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ('id', 'original', 'vault_path', 'status'))
                            or not isinstance(item.get('manifest'), dict) or '.' not in item['manifest']):
                        raise ValueError('回收记录配置无效')
            except (OSError, ValueError) as error:
                self._errors.append(f'目录配置无法读取：{error}；未恢复监测，其他功能仍可使用。')
                backup = self._file.with_name(f'directories.corrupt-{time.time_ns()}.json')
                try:
                    self._file.rename(backup)
                except OSError:
                    self._writable = False
                saved = {}
            for item in saved.get('roots', []):
                self._roots[item['id']] = self._root_state(item['id'], item['path'], item['limit_bytes'])
                self._pending[item['id']] = time.monotonic()
            self._vault = {item['id']: item for item in saved.get('vault', [])}
            self._recover()
        self._thread = threading.Thread(target=self._run, name='directory-governance', daemon=True)
        self._thread.start()

    @staticmethod
    def _root_state(id, path, limit):
        return dict(id=id, path=path, limit_bytes=limit, bytes=None, over_bytes=0, complete=False,
                    monitoring=False, busy=True, generation=0, source='initial', errors=[],
                    status='正在建立基线', checked_at=None)

    def _save(self):
        if not self._writable:
            raise ValueError('目录配置无法读取且不能备份，请修复配置文件权限后重试')
        saved = dict(roots=[{key: item[key] for key in ('id', 'path', 'limit_bytes')}
                            for item in self._roots.values()], vault=list(self._vault.values()))
        temporary = self._file.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, self._file)

    def _recover(self):
        # 只核对上次操作结果；启动时从不继续移动或删除。
        for item in self._vault.values():
            if item['status'] not in ('moving', 'restoring', 'purging'):
                continue
            try:
                vault = Path(item['vault_path'])
                if item['status'] == 'purging' and not os.path.lexists(vault):
                    item['status'] = 'purged'
                elif item['status'] == 'moving' and vault.exists() and not os.path.lexists(item['original']):
                    manifest, _, _ = self._capture(vault)
                    original = item['manifest']
                    if (manifest.keys() == original.keys() and all(
                            current[:4] == original[relative][:4] if relative in ('.', './')
                            else current == original[relative] for relative, current in manifest.items())):
                        item.update(status='vaulted', manifest=manifest)
                    else:
                        item['status'] = 'attention'
                elif item['status'] == 'restoring' and not os.path.lexists(vault):
                    target = Path(item.get('destination', item['original']))
                    if target.exists() and _identity(target.lstat())[:2] == item['manifest']['.'][:2]:
                        item['status'] = 'restored'
                    else:
                        item['status'] = 'attention'
                else:
                    item['status'] = 'attention'
            except (OSError, ValueError) as error:
                item.update(status='attention', error=str(error))

    def state(self):
        with self._lock:
            return dict(roots=[{**item, 'errors': list(item['errors'])} for item in self._roots.values()],
                        vault=[{key: value for key, value in item.items() if key != 'manifest'}
                               for item in reversed(list(self._vault.values()))],
                        busy=any(item['busy'] for item in self._roots.values()), generation=self._generation,
                        message=self._message, errors=list(self._errors),
                        metric='普通文件逻辑大小；符号链接、重解析点和无法访问项不计入。')

    def add(self, path, limit_bytes):
        if not isinstance(path, str) or not path.strip() or type(limit_bytes) is not int or limit_bytes < 1:
            raise ValueError('请选择目录，并设置大于零的字节阈值')
        root = validate_plain_path(path).resolve()
        if not root.is_dir() or root == Path(root.anchor):
            raise ValueError('请选择独立目录，不能监测整个文件系统根目录')
        if os.name != 'nt' and any(_inside(root, Path(value)) for value in ('/proc', '/sys', '/dev', '/run')):
            raise ValueError('不监测虚拟文件系统')
        with self._lock:
            if not self._writable:
                raise ValueError('目录配置不可用，请先修复配置文件权限')
            if any(_inside(root, Path(item['path'])) or _inside(Path(item['path']), root)
                   for item in self._roots.values()):
                raise ValueError('监测目录不能重复或相互包含')
            if _inside(self._state_dir, root) or any(_inside(Path(item['vault_path']), root) for item in self._vault.values()):
                raise ValueError('监测目录不能包含本应用的配置或回收目录')
            id = secrets.token_hex(12)
            self._roots[id] = self._root_state(id, str(root), limit_bytes)
            self._pending[id] = time.monotonic()
            self._previews.clear()
            self._save()
            return dict(self._roots[id])

    def remove(self, id):
        with self._lock:
            if id not in self._roots:
                raise ValueError('监测目录不存在')
            self._roots.pop(id)
            self._pending.pop(id, None)
            self._snapshots.pop(id, None)
            self._previews.clear()
            watcher = self._watchers.pop(id, None)
            self._save()
        if watcher:
            watcher.close()
        return self.state()

    def notify(self, path, source='trace'):
        target = Path(os.path.abspath(path)) if path else None
        with self._lock:
            for id, item in self._roots.items():
                if target is None or _inside(target, Path(item['path'])):
                    self._pending.setdefault(id, time.monotonic() + .25)
                    item['source'] = str(source)

    def _run(self):
        while not self._stop.wait(.1):
            with self._lock:
                roots = [(id, item['path']) for id, item in self._roots.items()]
            for id, path in roots:
                if self._stop.is_set():
                    return
                try:
                    if id not in self._watchers:
                        watcher = system.DirectoryWatch(path)
                        with self._lock:
                            if id not in self._roots:
                                watcher.close()
                                continue
                            self._watchers[id] = watcher
                    watcher = self._watchers.get(id)
                    if watcher is None:
                        continue
                    changed = watcher.changed()
                    now = time.monotonic()
                    with self._lock:
                        item = self._roots.get(id)
                        if item is None:
                            continue
                        item['monitoring'] = watcher.available
                        if changed:
                            self._pending.setdefault(id, now + .25)
                            item['source'] = 'directory-event'
                        if changed is None:
                            item['complete'] = False
                            item['status'] = '通知覆盖不完整；每 30 秒定时检查，期间变化可能延迟'
                            if now - item.get('_last_scan', 0) >= 30:
                                self._pending.setdefault(id, now)
                        if self._pending.get(id, now + 1) > now:
                            continue
                        self._pending.pop(id, None)
                        item['busy'] = True
                    snapshot = scan_directory(path, cancel=self._stop, backend='python')
                    analysis = classify_cleanup(snapshot, cancel=self._stop)
                    with self._lock:
                        if id not in self._roots or self._stop.is_set():
                            continue
                        self._snapshots[id] = (snapshot, analysis)
                        self._generation += 1
                        item.update(bytes=snapshot.total_bytes, over_bytes=max(0, snapshot.total_bytes - item['limit_bytes']),
                                    complete=snapshot.complete and watcher.available, monitoring=watcher.available,
                                    busy=False, generation=self._generation, checked_at=snapshot.timestamp,
                                    errors=list(snapshot.errors), _last_scan=now,
                                    status=('已检查；超限仅提示' if watcher.available else
                                            '通知覆盖不完整；每 30 秒定时检查，期间变化可能延迟'))
                except (OSError, ValueError) as error:
                    with self._lock:
                        if id in self._roots:
                            self._pending[id] = time.monotonic() + 30
                            self._roots[id].update(busy=False, complete=False, errors=[str(error)],
                                                   status='检查失败；30 秒后重试', _last_scan=time.monotonic())

    def rows(self, id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError('无效分页参数')
        with self._lock:
            if id not in self._roots:
                raise ValueError('监测目录不存在')
            pair = self._snapshots.get(id)
            if not pair:
                return dict(total=0, rows=[])
            snapshot, analysis = pair
            records = sorted(snapshot.files.values(), key=lambda record: (-record.size, record.relative))
            rows = []
            for record in records[offset:offset + min(limit, 200)]:
                classified = analysis.by_file.get(record.relative)
                risk = classified.risk if classified else 'medium'
                rows.append(dict(id=str(record.path), path=str(record.path), name=record.path.name, bytes=record.size,
                                 risk=risk, reason=classified.reason if classified else '用途未知，需逐项确认',
                                 readonly=risk == 'high', recommended=bool(classified and classified.recommended)))
            return dict(total=len(records), rows=rows)

    def _capture(self, path):
        path = validate_plain_path(path)
        info = path.lstat()
        if not info.st_ino or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('仅支持身份可确认的普通文件或独立目录')
        manifest = {'.': _identity(info)}
        if path.is_dir():
            snapshot = scan_directory(path, cancel=self._stop, backend='python')
            if not snapshot.complete:
                raise ValueError('目录包含链接、不可访问或不支持的项目，不能完整核验')
            for relative in snapshot.directories:
                directory = path / relative
                if os.path.ismount(directory):
                    raise ValueError('目录包含挂载点，不支持整目录回收')
                manifest[relative + '/'] = _identity(directory.lstat())
            manifest.update({relative: [record.device, record.inode, record.size, record.modified_ns, record.ctime_ns]
                             for relative, record in snapshot.files.items()})
            # 与目录项保持相同的 dev/inode/size/mtime/ctime 顺序。
            size = snapshot.total_bytes
        else:
            from ..core.files import DirectorySnapshot, FileRecord
            record = FileRecord.from_stat(path, path.parent, info)
            snapshot = DirectorySnapshot(path.parent, {path.name: record}, True, 0, (), False, False, time.time(), 0, None)
            size = info.st_size
        return manifest, size, classify_cleanup(snapshot)

    def _vault_directory(self, path):
        suffix = hashlib.sha256(str(self._state_dir).encode()).hexdigest()[:12]
        containing = next((Path(item['path']) for item in self._roots.values() if _inside(path, Path(item['path']))), path)
        candidates = [self._state_dir / 'vault', containing.parent / ('.cleandist-vault-' + suffix)]
        for candidate in candidates:
            if any(_inside(candidate, Path(item['path'])) for item in self._roots.values()):
                continue
            if _inside(candidate, path):
                continue
            try:
                candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
                validate_plain_path(candidate)
                if candidate.stat().st_dev == path.stat().st_dev:
                    return candidate
            except (OSError, ValueError):
                continue
        raise ValueError('没有可写且位于监测目录之外的同卷应用回收目录')

    def preview(self, paths, source='quota'):
        if source not in ('quota', 'portable') or not isinstance(paths, list) or not paths or any(not isinstance(path, str) for path in paths):
            raise ValueError('请选择需要回收的路径')
        with self._lock:
            chosen = [validate_plain_path(path).resolve() for path in paths]
            if len(set(chosen)) != len(chosen) or any(a != b and _inside(a, b) for a in chosen for b in chosen):
                raise ValueError('回收清单不能重复或相互包含')
            entries, notes = [], []
            for path in chosen:
                if (path == Path(path.anchor) or _inside(path, self._state_dir)
                        or _inside(self._state_dir, path)):
                    raise ValueError('不能回收文件系统根目录、本应用状态目录或包含状态目录的父目录')
                if any(_inside(path, Path(item['vault_path']).parent)
                       or _inside(Path(item['vault_path']).parent, path) for item in self._vault.values()):
                    raise ValueError('应用回收站中的项目请通过回收记录管理')
                if source == 'quota' and not any(path != Path(item['path']) and _inside(path, Path(item['path'])) for item in self._roots.values()):
                    raise ValueError('配额回收只能选择受监测目录内的项目')
                if source == 'portable' and not path.is_dir():
                    raise ValueError('便携应用需要用户明确选择独立目录，不能从可执行文件推断父目录')
                manifest, size, analysis = self._capture(path)
                if any(item.risk == 'high' for item in analysis.items):
                    raise ValueError('高风险内容仅供查看，不支持回收')
                vault = self._vault_directory(path)
                entries.append(dict(original=str(path), bytes=size, manifest=manifest, vault_root=str(vault), source=source))
                notes.extend(dict(name=item.name, risk=item.risk, reason=item.reason) for item in analysis.items)
            token = secrets.token_urlsafe(24)
            self._previews = {token: entries}
            return dict(token=token, count=len(entries), bytes=sum(item['bytes'] for item in entries), notes=notes,
                        files=[dict(path=item['original'], bytes=item['bytes']) for item in entries],
                        message='移入本应用回收站，同卷回收不会释放磁盘空间；永久删除需另行确认。')

    def recycle(self, token, confirmed=False):
        with self._lock:
            if confirmed is not True:
                raise ValueError('需要核对预览清单并明确确认回收')
            entries = self._previews.pop(token, None)
            if not entries:
                raise ValueError('回收预览已失效，请重新预览')
            for item in entries:
                if self._capture(Path(item['original']))[0] != item['manifest']:
                    raise ValueError('文件已变化，请重新预览')
            results = []
            for entry in entries:
                id = secrets.token_hex(16)
                target = Path(entry['vault_root']) / id
                validate_plain_path(target.parent)
                item = {key: value for key, value in entry.items() if key != 'vault_root'}
                item.update(id=id, vault_path=str(target), timestamp=time.time(), status='moving')
                self._vault[id] = item
                self._save()
                try:
                    os.rename(item['original'], target)
                    item['manifest'] = self._capture(target)[0]
                    item['status'] = 'vaulted'
                    results.append(dict(id=id, path=item['original'], bytes=item['bytes'], status='vaulted'))
                except (OSError, ValueError) as error:
                    item.update(status='attention' if target.exists() else 'failed', error=str(error))
                    results.append(dict(id=id, path=item['original'], status=item['status'], error=str(error)))
                self._save()
                self.notify(item['original'], source='vault')
            self._generation += 1
            failed = [item for item in results if item['status'] != 'vaulted']
            return dict(results=results, moved_bytes=sum(item['bytes'] for item in results if item['status'] == 'vaulted'),
                        freed_bytes=0, success=len(results) - len(failed), failed=len(failed),
                        error='；'.join(item['path'] + '：' + item['error'] for item in failed),
                        message=f'已回收 {len(results) - len(failed)} 项，失败 {len(failed)} 项；尚未释放磁盘空间。')

    def _item(self, id):
        item = self._vault.get(id)
        if item is None or item['status'] != 'vaulted':
            raise ValueError('请选择仍在应用回收站中的记录')
        path = Path(item['vault_path'])
        if path.name != id or self._capture(path)[0] != item['manifest']:
            raise ValueError('回收项目身份或内容已变化，拒绝处理')
        return item, path

    def restore(self, id, confirmed=False, destination=None):
        with self._lock:
            if confirmed is not True:
                raise ValueError('需要明确确认恢复位置')
            item, path = self._item(id)
            target = Path(os.path.abspath(destination or item['original']))
            validate_plain_path(target.parent)
            if os.path.lexists(target):
                raise ValueError('恢复目标已存在，请选择其他完整路径；不会覆盖')
            if _inside(target, path.parent) or target.parent.stat().st_dev != path.stat().st_dev:
                raise ValueError('恢复位置需要在回收目录之外且位于同一卷')
            item.update(status='restoring', destination=str(target))
            self._save()
            try:
                os.rename(path, target)
            except OSError:
                item['status'] = 'vaulted'
                self._save()
                raise
            item['status'] = 'restored'
            self._save()
            self.notify(str(target), source='restore')
            self._generation += 1
            return dict(id=id, status='restored', destination=str(target))

    def purge(self, ids, confirmed=False):
        if confirmed is not True:
            raise ValueError('永久删除不可恢复，需要单独明确确认具体清单')
        if not isinstance(ids, list) or not ids or any(not isinstance(id, str) for id in ids) or len(set(ids)) != len(ids):
            raise ValueError('请选择具体回收记录')
        with self._lock:
            chosen = [self._item(id) for id in ids]
            total = 0
            for item, path in chosen:
                item['status'] = 'purging'
                self._save()
                try:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                except OSError:
                    item['status'] = 'attention'
                    self._save()
                    raise
                item['status'] = 'purged'
                total += item['bytes']
                self._save()
            self._generation += 1
            return dict(count=len(chosen), deleted_bytes=total, message='已永久删除所选应用回收项目。')

    def shutdown(self):
        self._stop.set()
        self._thread.join(timeout=1)
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
            self._previews.clear()
        for watcher in watchers:
            watcher.close()
