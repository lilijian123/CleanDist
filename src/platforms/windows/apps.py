"""只读汇总 Windows 桌面登记、当前用户商店应用和便携候选。"""
import base64
import configparser
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import winreg

from ...core.apps import AppProgress, AppRecord, AppScanResult, is_portable_name, select_apps
from .disks import list_disks
from .icons import icon_location
from .scan import scan_native


def registered_path(values):
    location = os.path.expandvars(str(values.get('InstallLocation') or '')).strip().strip('"')
    if location and Path(location).is_absolute():
        return Path(location)
    icon = os.path.expandvars(str(values.get('DisplayIcon') or '')).strip()
    icon = re.sub(r',\s*-?\d+\s*$', '', icon).strip().strip('"')
    if icon and Path(icon).is_absolute():
        path = Path(icon)
        windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
        # 系统 Installer 图标不代表软件实际位置；未知位置保留未知，不推测盘符。
        if not path.is_relative_to(windows) and path.is_file():
            return path.parent
    return None


def desktop_apps(cancel):
    records, errors = [], []
    key_path = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    fields = ('DisplayName', 'DisplayVersion', 'Publisher', 'InstallLocation',
              'DisplayIcon', 'SystemComponent', 'ParentKeyName')
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            if cancel.is_set():
                return records, errors
            try:
                with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view) as registry:
                    for index in range(winreg.QueryInfoKey(registry)[0]):
                        if cancel.is_set():
                            break
                        try:
                            with winreg.OpenKey(registry, winreg.EnumKey(registry, index)) as item:
                                values = {}
                                for field in fields:
                                    try:
                                        values[field] = winreg.QueryValueEx(item, field)[0]
                                    except FileNotFoundError:
                                        pass
                                name = str(values.get('DisplayName') or '').strip()
                                if not name or values.get('SystemComponent') == 1 or values.get('ParentKeyName'):
                                    continue
                                icon_path, icon_index = icon_location(values.get('DisplayIcon'))
                                records.append(AppRecord(name, str(values.get('DisplayVersion') or ''),
                                                         str(values.get('Publisher') or ''),
                                                         registered_path(values), '桌面应用', icon_path, icon_index))
                        except OSError as error:
                            if len(errors) < 10:
                                errors.append(f'桌面登记项：{error}')
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f'桌面登记：{error}')
    return records, errors


def store_records(data):
    records = []
    for item in data:
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        path = str(item.get('path') or '').strip()
        publisher = str(item.get('publisher') or '')
        company = re.search(r'(?:^|,\s*)CN=([^,]+)', publisher)
        records.append(AppRecord(name, str(item.get('version') or ''),
                                 company.group(1) if company else publisher,
                                 Path(path) if path and Path(path).is_absolute() else None, '商店应用',
                                 icon_location(item.get('icon'))[0]))
    return records


def query_apps(script_name, cancel):
    script = Path(__file__).with_name(script_name).read_text(encoding='utf-8')
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = subprocess.Popen([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 45
    try:
        while True:
            if cancel.is_set():
                return []
            if time.monotonic() >= deadline:
                raise TimeoutError('应用信息查询超时')
            try:
                output, error = process.communicate(timeout=.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise OSError(error.decode('utf-8-sig', errors='replace').strip()[:400] or '应用信息查询失败')
        data = json.loads(output.decode('utf-8-sig') or '[]')
        return data if isinstance(data, list) else [data] if data else []
    finally:
        # 只终止本功能自己启动的查询进程，不操作任何真实软件进程。
        if process.poll() is None:
            process.terminate()
        process.communicate()


def store_apps(cancel):
    return store_records(query_apps('store.ps1', cancel))


def shortcut_apps(cancel):
    return query_apps('shortcuts.ps1', cancel)


def app_locations(known):
    shared = {Path(value) for name in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramData',
                                     'LOCALAPPDATA', 'APPDATA', 'USERPROFILE')
              if (value := os.environ.get(name))}
    return {app.path for app in known if app.path and app.path.parent != app.path and app.path not in shared}


def shortcut_records(data, known, cancel):
    installed = app_locations(known)
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    known_names = {app.name.casefold() for app in known}
    seen = {app.icon_path for app in known if app.icon_path}
    records = []
    for item in data:
        if cancel.is_set():
            break
        path = Path(str(item.get('path') or ''))
        name = str(item.get('name') or '').strip()
        name = re.sub(r'(?:\.exe)?\s*-\s*快捷方式$|\.exe$', '', name, flags=re.IGNORECASE).strip()
        if (not name or not path.is_absolute() or not is_portable_name(path.name)
                or path.is_relative_to(windows) or not is_portable_name(name + '.exe') or not path.is_file()):
            continue
        if path in seen or path in installed or any(parent in installed for parent in path.parents) or name.casefold() in known_names:
            continue
        icon_path, index = icon_location(item.get('icon'))
        records.append(AppRecord(name, str(item.get('version') or ''), str(item.get('publisher') or ''),
                                 path, '便携候选', icon_path or path, index))
        seen.add(path)
    return records


def portable_record(path):
    # 只接受有格式标记、应用名称且启动项对应当前文件的便携配置；不执行配置中的命令。
    info = path.parent / 'App/AppInfo/appinfo.ini'
    try:
        with info.open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return None
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(data.decode('utf-8-sig'))
        name = config.get('Details', 'Name', fallback='').strip()
        start = Path(config.get('Control', 'Start', fallback='').strip().strip('"'))
        if (config.get('Format', 'Type', fallback='') != 'PortableApps.comFormat' or not name
                or start.is_absolute() or '..' in start.parts or path.parent / start != path):
            return None
        icon = info.parent / 'appicon.ico'
        return AppRecord(name, config.get('Version', 'DisplayVersion', fallback=''),
                         config.get('Details', 'Publisher', fallback=''), path, '便携候选',
                         icon if icon.is_file() else path)
    except (OSError, ValueError, configparser.Error):
        return None


def portable_apps(snapshot, known, cancel):
    installed = app_locations(known)
    records = []
    for record in snapshot.files.values():
        if cancel.is_set():
            break
        path = record.path
        if not is_portable_name(path.name):
            continue
        if path in installed or any(parent in installed for parent in path.parents):
            continue
        if app := portable_record(path):
            records.append(app)
    return records


def scan_apps(roots=None, cancel=None, progress=None):
    started = time.monotonic()
    cancel = cancel or threading.Event()
    all_drives = roots is None
    roots = tuple(dict.fromkeys(Path(root) for root in (list_disks() if all_drives else roots)))
    records, errors = [], []
    skipped = 0

    def report(stage):
        if progress:
            progress(AppProgress(stage, len(records)))

    if not cancel.is_set():
        report('读取桌面应用')
        try:
            desktop, issues = desktop_apps(cancel)
            records.extend(desktop)
            errors.extend(issues)
        except (OSError, ValueError) as error:
            errors.append(f'桌面登记：{error}')
    if not cancel.is_set():
        report('读取商店应用')
        try:
            records.extend(store_apps(cancel))
        except (OSError, ValueError, TimeoutError) as error:
            errors.append(f'商店应用：{error}')
    if not cancel.is_set():
        report('读取应用快捷方式')
        try:
            records.extend(shortcut_records(shortcut_apps(cancel), records, cancel))
        except (OSError, ValueError, TimeoutError) as error:
            errors.append(f'应用快捷方式：{error}')
    known = tuple(records)
    records = list(select_apps(records, roots, include_unknown=all_drives))
    for root in roots:
        if cancel.is_set():
            break
        report(f'扫描便携候选 {root}')
        try:
            def on_progress(info):
                report(f'扫描便携候选 {root} · {info.files:,} 个可执行文件')
            snapshot = scan_native(root, cancel, None, on_progress, apps_only=True)
            if snapshot is None:
                raise OSError('原生扫描环境不可用')
            skipped += snapshot.skipped
            errors.extend(snapshot.errors)
            records.extend(portable_apps(snapshot, known, cancel))
        except (OSError, ValueError, EOFError, subprocess.TimeoutExpired) as error:
            errors.append(f'{root}：{error}')
            skipped += 1
    return AppScanResult(select_apps(records), tuple(errors), skipped,
                         cancel.is_set(), time.monotonic() - started)
