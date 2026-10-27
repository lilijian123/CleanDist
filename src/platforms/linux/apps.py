"""只读 XDG 桌面登记；便携候选必须有 .desktop 启动项作为依据。"""
import configparser
from dataclasses import replace
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time

from ...core.apps import AppProgress, AppRecord, AppScanResult, select_apps


def data_dirs():
    home = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    shared = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')
    return tuple(dict.fromkeys(path for path in (home, *(Path(p) for p in shared.split(':') if p))
                               if path.is_absolute()))


def application_dirs():
    directories = [directory / 'applications' for directory in data_dirs()]
    directories.extend((Path.home() / '.local/share/flatpak/exports/share/applications',
                        Path('/var/lib/snapd/desktop/applications'),
                        Path('/var/lib/flatpak/exports/share/applications')))
    return tuple(dict.fromkeys(directories))


def desktop_entry(path):
    """读取有限大小的元数据，不展开变量或执行 Exec/TryExec。"""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('桌面登记不是普通文件')
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('桌面登记超过 64 KiB')
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string(data.decode('utf-8-sig'))
    return dict(config['Desktop Entry']) if config.has_section('Desktop Entry') else {}


def localized_name(entry):
    locale = next((os.environ.get(key) for key in ('LC_ALL', 'LC_MESSAGES', 'LANG')
                   if os.environ.get(key)), '')
    locales = os.environ.get('LANGUAGE', '').split(':') + [locale]
    for value in locales:
        value = value.split('.')[0]
        language, _, modifier = value.partition('@')
        base = language.split('_')[0]
        for suffix in dict.fromkeys((value, language, base + '@' + modifier if modifier else base, base)):
            if suffix and entry.get(f'Name[{suffix}]', '').strip():
                return entry[f'Name[{suffix}]'].strip()
    return entry.get('Name', '').strip()


def executable_path(command):
    """只解析直接启动及 env 的简单赋值形式；不解释 shell 命令。"""
    parts = shlex.split(command)
    if not parts:
        return None
    if Path(parts[0]).name == 'env':
        parts = parts[1:]
        while parts and '=' in parts[0] and not parts[0].startswith('/'):
            parts = parts[1:]
    if not parts or parts[0].startswith(('-', '%')):
        return None
    target = Path(parts[0])
    if target.name in {'sh', 'bash', 'dash', 'zsh', 'fish'}:
        return None
    if not target.is_absolute():
        found = shutil.which(parts[0])
        target = Path(found) if found else None
    return target if target and target.is_file() and os.access(target, os.X_OK) else None


def app_record(path, entry, *, portable=False):
    if (entry.get('Type') != 'Application'
            or entry.get('Hidden', '').lower() == 'true'
            or entry.get('NoDisplay', '').lower() == 'true'):
        return None
    name = localized_name(entry)
    if not name:
        return None
    if entry.get('TryExec') and executable_path(entry['TryExec']) is None:
        return None
    executable = executable_path(entry.get('Exec', ''))
    if entry.get('X-Flatpak') or '/flatpak/exports/share/applications/' in path.as_posix():
        source, location = 'Flatpak应用', path
    elif entry.get('X-SnapInstanceName') or '/snapd/desktop/applications/' in path.as_posix():
        source, location = 'Snap应用', executable
    elif portable or (executable and executable.suffix.lower() == '.appimage'):
        if executable is None:
            return None
        source, location = '便携候选', executable
    else:
        source, location = '桌面应用', executable
    # Desktop Entry 的 Version 是格式版本，不能用作软件版本。未知发布者同样留空。
    # 保存登记文件作为延迟加载图标的依据，扫描时不解码任何图片。
    return AppRecord(name, '', '', location, source, path)


def desktop_files(root, cancel, errors, *, missing_ok=False):
    """不跟随目录链接；仅打开 .desktop 文件，跳过 Linux 虚拟文件系统。"""
    def onerror(error):
        if missing_ok and isinstance(error, FileNotFoundError) and Path(error.filename) == root:
            return
        if len(errors) < 20:
            errors.append(f'{root}：{error}')
    for directory, folders, files in os.walk(root, onerror=onerror, followlinks=False):
        if cancel.is_set():
            return
        folders[:] = [name for name in folders
                      if name not in {'.git', 'node_modules', '__pycache__'}
                      and Path(directory) / name not in {Path('/proc'), Path('/sys'), Path('/dev'), Path('/run')}]
        for name in sorted(files):
            if cancel.is_set():
                return
            if name.endswith('.desktop'):
                yield Path(directory) / name


def package_query(command, cancel, deadline):
    """只运行 dpkg-query；每次最多 1.5 秒，取消时终止自己的查询进程组。"""
    if cancel.is_set():
        return ''
    env = dict(os.environ, LC_ALL='C')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, start_new_session=True)
    end = min(deadline, time.monotonic() + 1.5)
    try:
        while not cancel.is_set():
            if time.monotonic() >= end:
                raise TimeoutError('dpkg 应用元数据查询超时')
            try:
                output, _ = process.communicate(timeout=.05)
                return output.decode('utf-8', errors='replace') if process.returncode in {0, 1} else ''
            except subprocess.TimeoutExpired:
                pass
        return ''
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def package_metadata(records, cancel, errors):
    """按可执行文件查已安装 dpkg 包，版本和 Maintainer 均来自包数据库。"""
    query = shutil.which('dpkg-query')
    if not query or cancel.is_set():
        return records
    targets = {}
    for app in records:
        if app.path and app.source not in {'Snap应用', 'Flatpak应用'}:
            for path in (str(app.path), os.path.realpath(app.path)):
                # dpkg --search 将参数视为通配模式；不查询含模式字符的文件名。
                if any(char in path for char in '*?[]\\'):
                    continue
                targets.setdefault(path, set()).add(app.path)
                # Ubuntu usrmerge 后 /bin/foo 与 /usr/bin/foo 可能只登记其一。
                if path.startswith(('/usr/bin/', '/usr/sbin/', '/usr/lib/')):
                    targets.setdefault(path[4:], set()).add(app.path)
    paths, owners, metadata = list(targets), {}, {}
    deadline = time.monotonic() + 4
    try:
        for start in range(0, len(paths), 64):
            if cancel.is_set():
                return records
            output = package_query([query, '--search', *paths[start:start + 64]], cancel, deadline)
            for line in output.splitlines():
                package, separator, path = line.rpartition(': ')
                if (separator and path in targets
                        and re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9]+)?', package)):
                    for original in targets[path]:
                        owners.setdefault(original, set()).add(package)
        packages = sorted({next(iter(values)) for values in owners.values() if len(values) == 1})
        for start in range(0, len(packages), 64):
            if cancel.is_set():
                return records
            output = package_query([query, '--show',
                '--showformat=${binary:Package}\t${Version}\t${Maintainer}\t${db:Status-Status}\n',
                *packages[start:start + 64]], cancel, deadline)
            for line in output.splitlines():
                values = line.split('\t')
                if len(values) == 4 and values[3] == 'installed':
                    metadata[values[0]] = values[1:3]
    except (OSError, TimeoutError) as error:
        if len(errors) < 20:
            errors.append(f'已安装包元数据：{error}')
    enriched = []
    for app in records:
        packages = owners.get(app.path, set())
        values = metadata.get(next(iter(packages))) if len(packages) == 1 else None
        enriched.append(replace(app, version=values[0], publisher=values[1]) if values else app)
    return tuple(enriched)


def scan_apps(roots=None, cancel=None, progress=None):
    started = time.monotonic()
    cancel = cancel or threading.Event()
    roots = None if roots is None else tuple(dict.fromkeys(Path(root) for root in roots))
    records, errors, seen_ids, seen_paths = [], [], set(), set()
    skipped = 0

    def read(path, portable=False):
        nonlocal skipped
        try:
            record = app_record(path, desktop_entry(path), portable=portable)
            if record:
                records.append(record)
        except (OSError, UnicodeError, ValueError, configparser.Error) as error:
            skipped += 1
            if len(errors) < 20:
                errors.append(f'{path}：{error}')

    for directory in application_dirs():
        if cancel.is_set():
            break
        if progress:
            progress(AppProgress(f'读取桌面应用 {directory}', len(records)))
        for path in desktop_files(directory, cancel, errors, missing_ok=True):
            # 高优先级 Hidden 项也覆盖同名系统登记。
            desktop_id = str(path.relative_to(directory)).replace('/', '-')
            if desktop_id in seen_ids:
                continue
            seen_ids.add(desktop_id)
            seen_paths.add(path)
            read(path)
    if roots is not None:
        for root in roots:
            if cancel.is_set():
                break
            if progress:
                progress(AppProgress(f'读取便携候选 {root}', len(records)))
            for path in desktop_files(root, cancel, errors):
                if path not in seen_paths:
                    seen_paths.add(path)
                    read(path, portable=True)
    records = select_apps(records, roots, include_unknown=roots is None)
    if progress and not cancel.is_set():
        progress(AppProgress('读取已安装包元数据', len(records)))
    records = package_metadata(records, cancel, errors)
    return AppScanResult(records,
                         tuple(errors), skipped, cancel.is_set(), time.monotonic() - started)
