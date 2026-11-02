"""Installed package metadata, explicit uninstall previews and portable-directory handoff."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform as host_platform
import re
import shutil
import subprocess
import threading
import uuid


# Run the distribution's Python, not a venv: Ubuntu ships python3-apt there.
# target_versions is APT's version/alternative/virtual-package resolution.
APT_QUERY = '''import apt, apt_pkg, json
cache = apt.Cache()
native = apt_pkg.config.find('APT::Architecture')
rows = []
for package in cache:
    version = package.installed
    if version is None:
        continue
    groups = []
    for group in version.get_dependencies('Depends', 'PreDepends'):
        targets = sorted({'apt:' + target.package.fullname
            for dependency in group.or_dependencies
            for target in dependency.target_versions if target.is_installed})
        groups.append({'expression': group.rawstr, 'targets': targets})
    rows.append({'id': 'apt:' + package.fullname, 'name': package.fullname,
        'native': package.architecture() == native,
        'version': version.version, 'source': 'APT', 'auto': package.is_auto_installed,
        'essential': package.essential, 'installed_size': version.installed_size,
        'dependencies': sorted({target for group in groups for target in group['targets']}),
        'dependency_groups': groups})
print(json.dumps(rows))
'''

WINDOWS_QUERY = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$rows = @()
foreach ($p in @(Get-AppxPackage)) {
    $rows += @{id=('appx:'+$p.PackageFullName); name=$p.Name; version=$p.Version.ToString();
        source='AppX'; dependencies=@($p.Dependencies | ForEach-Object {'appx:'+$_.PackageFullName});
        package_full_name=$p.PackageFullName; framework=[bool]$p.IsFramework;
        system=[bool]$p.NonRemovable; path=$p.InstallLocation}
}
$seen = @{}
foreach ($hive in @([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryHive]::CurrentUser)) {
    foreach ($view in @([Microsoft.Win32.RegistryView]::Registry64,[Microsoft.Win32.RegistryView]::Registry32)) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
        $key = $base.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
        if ($null -ne $key) {
            foreach ($name in $key.GetSubKeyNames()) {
                $item = $key.OpenSubKey($name)
                if ($null -eq $item) {continue}
                $display = [string]$item.GetValue('DisplayName')
                $identity = $hive.ToString()+':'+$name
                if ($display -and !$item.GetValue('SystemComponent') -and !$item.GetValue('ParentKeyName') -and !$seen.ContainsKey($identity)) {
                    $seen[$identity]=$true
                    $rows += @{id=('desktop:'+$identity); name=$display;
                        version=[string]$item.GetValue('DisplayVersion'); source='Desktop'; dependencies=$null;
                        uninstall_command=[string]$item.GetValue('UninstallString');
                        path=[string]$item.GetValue('InstallLocation')}
                }
                $item.Dispose()
            }
            $key.Dispose()
        }
        $base.Dispose()
    }
}
ConvertTo-Json -InputObject @($rows) -Depth 6 -Compress
'''


class DependencyService:
    def __init__(self, recycle_preview=None, *, state_dir=None, runner=None, platform=None):
        self._platform = platform or host_platform.system()
        root = Path(state_dir) if state_dir is not None else (
            Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'CleanDist')
        self._path = root / 'portable-apps.json'
        self._runner = runner
        self._recycle_preview = recycle_preview
        self._lock = threading.RLock()
        self._rows, self._portable, self._previews = {}, {}, {}
        self._busy, self._closed, self._thread = False, False, None
        self._error, self._errors, self._generation, self._result = '', [], 0, None
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding='utf-8'))
                if not isinstance(data, list):
                    raise ValueError('便携应用登记格式错误')
                for row in data:
                    if (not isinstance(row, dict) or row.get('source') != 'Portable'
                            or not isinstance(row.get('id'), str) or not isinstance(row.get('path'), str)
                            or not isinstance(row.get('name'), str)
                            or (row.get('dependencies') is not None and
                                (not isinstance(row['dependencies'], list) or not all(isinstance(item, str) for item in row['dependencies'])))):
                        raise ValueError('便携应用登记格式错误')
                self._portable = {row['id']: row for row in data}
        except (ValueError, OSError) as error:
            self._error = str(error)
        self._rebuild()

    def _run(self, command, timeout=180):
        if self._runner is not None:
            result = self._runner(command)
        else:
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                    encoding='utf-8-sig', errors='replace', timeout=timeout,
                    env=dict(os.environ, LC_ALL='C'), shell=False,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
            except subprocess.TimeoutExpired as error:
                raise OSError('包数据库查询超时，请稍后重试') from error
            except OSError as error:
                if self._platform == 'Windows' and isinstance(command, str) and getattr(error, 'winerror', None) == 740:
                    return self._elevated_uninstall(command)
                raise
        if result.returncode:
            raise OSError((result.stderr or result.stdout or '包管理操作失败').strip()[:1500])
        return result.stdout

    @staticmethod
    def _elevated_uninstall(command):
        """Only retry an installed desktop uninstaller rejected with ERROR_ELEVATION_REQUIRED."""
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel.LocalFree.restype = wintypes.HLOCAL
        count = ctypes.c_int()
        argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
        if not argv:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            arguments = [argv[index] for index in range(count.value)]
        finally:
            kernel.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))
        if not arguments or not arguments[0]:
            raise ValueError('登记的卸载命令缺少可执行文件')

        class ShellExecuteInfo(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG),
                ('hwnd', wintypes.HWND), ('lpVerb', wintypes.LPCWSTR),
                ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int),
                ('hInstApp', wintypes.HINSTANCE), ('lpIDList', ctypes.c_void_p),
                ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE),
                ('hProcess', wintypes.HANDLE)]

        shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
        shell.ShellExecuteExW.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        info = ShellExecuteInfo(cbSize=ctypes.sizeof(ShellExecuteInfo), fMask=0x140,
            lpVerb='runas', lpFile=arguments[0],
            lpParameters=subprocess.list2cmdline(arguments[1:]), nShow=1)
        if not shell.ShellExecuteExW(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.hProcess:
            raise OSError('系统没有返回卸载进程句柄，无法确认卸载结果')
        try:
            if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if code.value:
                raise OSError(f'卸载程序返回退出码 {code.value}，请核对系统安装状态')
        finally:
            kernel.CloseHandle(info.hProcess)
        return ''

    def _powershell(self, script):
        executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        return [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]

    @staticmethod
    def _apt_names(rows):
        lookup, grouped = {}, {}
        for row in rows:
            if row['source'] == 'APT':
                lookup[row['name']] = row
                grouped.setdefault(row['name'].split(':')[0], []).append(row)
        for name, group in grouped.items():
            native = [row for row in group if row.get('native')]
            if len(native) == 1:
                lookup[name] = native[0]
            elif len(group) == 1:
                lookup[name] = group[0]
        return lookup

    def _collect(self):
        errors = []
        if self._platform == 'Linux':
            try:
                rows = json.loads(self._run(['/usr/bin/python3', '-c', APT_QUERY]))
            except OSError as error:
                raise OSError(f'无法读取 APT 依赖；需要系统 python3-apt：{error}') from error
            orphans = set()
            try:
                simulation = self._run(['/usr/bin/apt-get', '-s', 'autoremove'])
                orphans = {line.split()[1] for line in simulation.splitlines() if line.startswith('Remv ')}
            except OSError as error:
                errors.append(f'APT 孤儿模拟不可用，未推荐任何包：{error}')
            lookup = self._apt_names(rows)
            orphan_ids = {lookup[name]['id'] for name in orphans if name in lookup}
            for row in rows:
                row['orphan'] = bool(row.get('auto') and not row.get('essential') and row['id'] in orphan_ids)
                row['orphan_reason'] = 'APT 模拟 autoremove 列出的自动安装包' if row['orphan'] else ''
        elif self._platform == 'Windows':
            rows = json.loads(self._run(self._powershell(WINDOWS_QUERY)))
        else:
            raise ValueError('依赖查询仅支持 Windows 与 Ubuntu/APT')
        if not isinstance(rows, list):
            raise ValueError('包管理器返回无效元数据')
        return {row['id']: row for row in rows}, errors

    def _rebuild(self):
        self._rows = {**{key: value for key, value in self._rows.items() if value['source'] != 'Portable'}, **self._portable}
        for row in self._rows.values():
            row['reverse_ids'] = []
            row['dependency_status'] = 'known' if row.get('dependencies') is not None else 'unknown'
            if row['source'] != 'APT':
                row['orphan'] = False
                row['orphan_reason'] = ''
            if row['source'] == 'Portable':
                row['present'] = Path(row['path']).is_dir()
        for row in self._rows.values():
            if row['source'] == 'Portable' and not row['present']:
                continue
            for target in row.get('dependencies') or []:
                if target in self._rows:
                    self._rows[target]['reverse_ids'].append(row['id'])
        # Unknown portable relationships can reference any portable dependency.
        unknown_portable = any(row.get('dependencies') is None and row['present'] for row in self._portable.values())
        for row in self._portable.values():
            row['orphan'] = bool(row['present'] and row.get('is_dependency') and not row.get('keep')
                and row['dependency_status'] == 'known' and not row['reverse_ids'] and not unknown_portable)
            row['orphan_reason'] = '明确登记为依赖，已知无反向引用且未手动保留' if row['orphan'] else ''

    def _refresh_portable(self):
        if any(Path(row['path']).is_dir() != row.get('present') for row in self._portable.values()):
            self._rebuild()
            self._generation += 1

    def state(self):
        with self._lock:
            self._refresh_portable()
            return {'busy': self._busy, 'count': len(self._rows),
                    'orphans': sum(bool(row.get('orphan')) for row in self._rows.values()),
                    'generation': self._generation, 'stage': '读取包数据库与模拟孤儿' if self._busy else '依赖信息',
                    'error': self._error, 'errors': list(self._errors), 'result': self._result}

    def scan(self):
        with self._lock:
            if self._closed:
                raise ValueError('依赖服务已关闭')
            if self._busy:
                return self.state()
            self._busy, self._error = True, ''
            self._thread = threading.Thread(target=self._scan, daemon=True, name='dependencies')
            self._thread.start()
            return self.state()

    def _scan(self):
        try:
            rows, errors = self._collect()
            with self._lock:
                self._rows, self._errors = rows, errors
                self._rebuild()
                self._generation += 1
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            with self._lock:
                self._error = str(error)
        finally:
            with self._lock:
                self._busy = False

    @staticmethod
    def _public(row):
        return {key: value for key, value in row.items()
                if key not in ('uninstall_command', 'reverse_ids')}

    def rows(self, offset=0, limit=100, query='', orphans=False):
        if (type(offset) is not int or type(limit) is not int or offset < 0 or limit < 1
                or not isinstance(query, str) or type(orphans) is not bool):
            raise ValueError('无效的依赖列表参数')
        with self._lock:
            self._refresh_portable()
            rows = [self._public(row) for row in self._rows.values()
                if (not orphans or row.get('orphan')) and query.casefold() in (row['name'] + ' ' + row.get('path', '')).casefold()]
            rows.sort(key=lambda row: (row['source'], row['name'].casefold()))
            return {'total': len(rows), 'rows': rows[offset:offset + min(limit, 200)]}

    def details(self, id):
        with self._lock:
            self._refresh_portable()
            row = self._rows.get(id)
            if row is None:
                raise ValueError('应用不在当前依赖列表中，请重新扫描')
            return {**self._public(row),
                'dependencies': [self._public(self._rows[target]) if target in self._rows else
                    {'id': target, 'name': target, 'dependency_status': 'unknown'} for target in row.get('dependencies') or []],
                'reverse_dependencies': [self._public(self._rows[target]) for target in row['reverse_ids']],
                'uninstall_available': row['source'] == 'APT' or (row['source'] == 'AppX' and not row.get('framework') and not row.get('system'))
                    or (row['source'] == 'Desktop' and bool(row.get('uninstall_command')))}

    def _selection(self, ids):
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise ValueError('请选择具体应用')
        rows = [self._rows.get(id) for id in dict.fromkeys(ids)]
        if any(row is None for row in rows):
            raise ValueError('应用状态已经变化，请重新扫描')
        if any(row['source'] == 'Portable' for row in rows):
            raise ValueError('便携目录请使用应用回收站预览')
        if len({row['source'] for row in rows}) != 1:
            raise ValueError('请分别预览不同来源的应用')
        if any(row.get('framework') or row.get('system') or row.get('essential') for row in rows):
            raise ValueError('不在此入口卸载系统组件、必需包或 AppX 框架')
        if rows[0]['source'] == 'Desktop' and (len(rows) != 1 or not rows[0].get('uninstall_command')):
            raise ValueError('每次请选择一个具有正式卸载入口的桌面应用')
        return rows

    def _apt_plan(self, rows):
        command = ['/usr/bin/apt-get', '-s', 'remove', '--', *[row['name'] for row in rows]]
        output = self._run(command)
        changes = [line.strip() for line in output.splitlines() if re.match(r'^(Remv|Inst|Conf) ', line)]
        if any(not line.startswith('Remv ') for line in changes):
            raise ValueError('此次卸载模拟包含安装或配置操作，请在系统包管理器中处理')
        affected = []
        lookup = self._apt_names(self._rows.values())
        for line in changes:
            name = line.split()[1]
            row = lookup.get(name)
            if row is None:
                raise ValueError('模拟卸载包的架构信息不明确，请在系统包管理器中处理')
            if row.get('essential'):
                raise ValueError('模拟结果涉及系统必需包，已拒绝卸载')
            affected.append(self._public(row))
        if not affected:
            raise ValueError('模拟没有待卸载包，请重新扫描')
        return changes, affected

    def preview(self, ids):
        with self._lock:
            if self._busy:
                raise ValueError('请等待依赖扫描完成')
            rows = self._selection(ids)
            source = rows[0]['source']
            if source == 'APT':
                changes, affected = self._apt_plan(rows)
                permission = '需要 root 权限；非 root 通过系统 pkexec 授权；无授权入口则拒绝执行'
            else:
                changes, affected = [], [self._public(row) for row in rows]
                selected = {row['id'] for row in rows}
                if source == 'AppX' and any(set(row['reverse_ids']) - selected for row in rows):
                    raise ValueError('仍有已知 AppX 反向依赖，请先在系统设置中处理')
                permission = '当前用户 AppX 正规卸载' if source == 'AppX' else '打开登记的卸载程序；可能弹出系统权限或应用确认窗口'
            token = uuid.uuid4().hex
            result = {'token': token, 'source': source, 'affected': affected, 'changes': changes,
                'count': len(affected), 'space_bytes_estimate': sum(row.get('installed_size', 0) for row in affected) if source == 'APT' else None,
                'permission': permission, 'message': '正式卸载软件包/应用，不能从应用回收站恢复；用户数据不在此处推测清理。'}
            self._previews = {token: {'ids': [row['id'] for row in rows], 'source': source,
                'signatures': [(row['id'], row.get('version'), row.get('uninstall_command')) for row in rows],
                'changes': changes, 'result': result}}
            return result

    def uninstall(self, token, confirmed=False):
        if confirmed is not True:
            raise ValueError('需要对正式卸载清单单独确认')
        with self._lock:
            preview = self._previews.get(token)
            if preview is None:
                raise ValueError('卸载预览已失效，请重新预览')
            if self._busy:
                raise ValueError('请等待依赖扫描完成')
            rows = self._selection(preview['ids'])
            if preview['source'] == 'APT':
                changes, _ = self._apt_plan(rows)
                if changes != preview['changes']:
                    raise ValueError('包管理器模拟结果已经变化，请重新预览并确认')
                command = ['/usr/bin/apt-get', '--assume-yes', '--no-download', 'remove', '--', *[row['name'] for row in rows]]
                if not hasattr(os, 'geteuid') or os.geteuid() != 0:
                    authorization = shutil.which('pkexec')
                    if not authorization:
                        raise PermissionError('未获 root 权限且没有 pkexec 系统授权入口；未执行卸载，请在系统包管理器中操作')
                    command.insert(0, authorization)
            else:
                fresh, _ = self._collect()
                signatures = [(id, fresh.get(id, {}).get('version'), fresh.get(id, {}).get('uninstall_command')) for id in preview['ids']]
                if signatures != preview['signatures'] or any(id not in fresh for id in preview['ids']):
                    raise ValueError('应用登记已经变化，请重新预览并确认')
                if preview['source'] == 'AppX':
                    if any(fresh[id].get('system') or fresh[id].get('framework') or
                           any(id in (row.get('dependencies') or []) and row['id'] not in preview['ids'] for row in fresh.values()) for id in preview['ids']):
                        raise ValueError('应用依赖已经变化，请重新预览并确认')
                    payload = base64.b64encode(json.dumps([row['package_full_name'] for row in rows]).encode()).decode('ascii')
                    script = "$ErrorActionPreference='Stop'; $names=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "'))); foreach($name in $names){ Remove-AppxPackage -Package $name -ErrorAction Stop }"
                    command = self._powershell(script)
                else:
                    # Windows CreateProcess receives the registered command line verbatim.
                    # shell=False: no cmd.exe wrapper and no interpolated shell command.
                    command = rows[0]['uninstall_command']
            self._previews.pop(token)
        error, output = '', ''
        try:
            # Official uninstallers may wait for an interactive OS/application dialog.
            # Do not terminate an installer because a metadata-query timeout elapsed.
            output = self._run(command, timeout=None)
        except (OSError, subprocess.SubprocessError) as failure:
            error = str(failure)
        finally:
            try:
                fresh, errors = self._collect()
                with self._lock:
                    self._rows, self._errors = fresh, errors
                    self._rebuild()
                    self._generation += 1
            except (OSError, ValueError, subprocess.SubprocessError) as failure:
                error = (error + '\n刷新包数据库：' + str(failure)).strip()
        with self._lock:
            remaining = [id for id in preview['ids'] if id in self._rows]
            self._result = {'completed': not error and not remaining, 'remaining': remaining,
                'output': output[-2000:], 'error': error,
                'message': '卸载入口已返回，已重新查询登记；仍登记的应用未标记为卸载成功。' if remaining else '卸载入口已返回并重新查询登记。'}
            return dict(self._result)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix('.tmp')
        fields = ('id', 'name', 'source', 'path', 'dependencies', 'is_dependency', 'keep')
        temporary.write_text(json.dumps([{key: row.get(key) for key in fields} for row in self._portable.values()], ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, self._path)

    def portable_register(self, path, name, dependencies=None, is_dependency=False):
        if (not isinstance(path, str) or not isinstance(name, str) or not name.strip()
                or type(is_dependency) is not bool or
                (dependencies is not None and (not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies)))):
            raise ValueError('请填写便携应用目录、名称和已登记依赖关系')
        directory = Path(path)
        if not directory.is_absolute():
            raise ValueError('便携应用必须登记完整的独立目录')
        directory = directory.resolve()
        home = Path.home().resolve()
        forbidden = [Path(directory.anchor), home, self._path.parent.resolve()]
        if self._platform == 'Linux':
            forbidden += [Path(value) for value in ('/usr', '/bin', '/sbin', '/etc', '/var', '/opt', '/home', '/proc', '/sys', '/dev', '/run')]
        else:
            forbidden += [Path(value).resolve() for key in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData', 'APPDATA', 'LOCALAPPDATA') if (value := os.environ.get(key))]
        if any(directory == item or item.is_relative_to(directory) for item in forbidden):
            raise ValueError('请选择单个便携应用的独立目录，不能登记系统、用户或共享根目录')
        with self._lock:
            existing = next((row for row in self._portable.values() if directory == Path(row['path'])), None)
            if any(directory != Path(row['path']) and (directory.is_relative_to(Path(row['path'])) or Path(row['path']).is_relative_to(directory)) for row in self._portable.values()):
                raise ValueError('便携目录不能互相包含')
            if not directory.is_dir() or Path(path).is_symlink() or (getattr(Path(path).lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('请选择真实存在的独立目录，不能选择文件、符号链接或重解析目录')
            if dependencies is not None and any(id not in self._rows for id in dependencies):
                raise ValueError('依赖关系只能选择已经登记或扫描的应用')
            id = existing['id'] if existing else 'portable:' + uuid.uuid4().hex
            row = {'id': id, 'name': name.strip(), 'source': 'Portable', 'path': str(directory),
                'dependencies': list(dict.fromkeys(dependencies)) if dependencies is not None else None,
                'is_dependency': is_dependency,
                'keep': existing['keep'] if existing and existing.get('is_dependency') == is_dependency else not is_dependency}
            self._portable[id] = row
            self._save()
            self._rebuild()
            self._generation += 1
            return self._public(row)

    def portable_keep(self, id, keep=True):
        if type(keep) is not bool:
            raise ValueError('无效的保留状态')
        with self._lock:
            if id not in self._portable:
                raise ValueError('便携应用没有登记')
            self._portable[id]['keep'] = keep
            self._save()
            self._rebuild()
            self._generation += 1
            return self._public(self._portable[id])

    def portable_preview(self, id):
        with self._lock:
            if id not in self._portable:
                raise ValueError('便携应用没有登记')
            if not Path(self._portable[id]['path']).is_dir():
                raise ValueError('便携目录已经移走或回收；恢复后才可再次预览')
            if self._recycle_preview is None:
                raise ValueError('应用回收站尚未就绪')
            path = self._portable[id]['path']
        return self._recycle_preview([path], source='portable')

    def shutdown(self):
        self._closed = True
        self._previews.clear()
        if self._thread is not None:
            self._thread.join(timeout=.2)
