"""Resolve uninstall leftovers from package metadata and registered application paths."""
import os
from pathlib import Path
import re
import shlex
import stat

from ..core.files import stat_identity, validate_plain_path

_CONFDIR = Path('/etc')
_VENDOR_USER_UNITS = {Path('/usr/lib/systemd/user'), Path('/usr/share/systemd/user')}

def file_identity(path):
    info = validate_plain_path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino or os.path.ismount(path):
        raise ValueError('仅支持归属可核验、没有其他硬链接的普通文件')
    return stat_identity(info)


def apt_owner(run, path, owner, allow_missing=False):
    # dpkg-query -S interprets glob patterns. Compare the complete answer to also
    # exclude diversions and multiple registered owners.
    if any(c in str(path) for c in '*?[]\\\n'):
        return False
    try:
        output = run(['/usr/bin/dpkg-query', '-S', '--', str(path)])
    except OSError as error:
        if str(error) == f'dpkg-query: no path found matching pattern {path}':
            return allow_missing
        raise
    return output.splitlines() == [f'{owner}: {path}']


def apt_manifest(run, rows, notes):
    entries = []
    for row in rows:
        try:
            output = run(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\\n${Conffiles}', '--', row['name']])
            owner, *lines = output.splitlines()
            for line in lines:
                match = re.fullmatch(r' (/.+) [0-9a-f]{32}', line)
                if not match:
                    continue
                path = Path(match[1])
                if _CONFDIR not in path.parents or '..' in path.parts:
                    continue
                try:
                    identity = file_identity(path)
                except (ValueError, FileNotFoundError):
                    continue
                if apt_owner(run, path, owner):
                    entries.append(dict(path=str(path), package=row['id'], owner=row['name'], dpkg_owner=owner,
                                        identity=identity, evidence='dpkg 配置文件清单及独占归属'))
        except (ValueError, OSError) as error:
            notes.append(f"{row['name']} 配置文件清单不可用：{error}")
    return entries


def cache_files(root, fields, notes):
    validate_plain_path(root)
    errors = []
    for directory, dirs, files in os.walk(root, onerror=errors.append):
        for name in dirs[:]:
            try:
                child = validate_plain_path(Path(directory) / name)
                if os.path.ismount(child):
                    dirs.remove(name)
            except (ValueError, FileNotFoundError):
                dirs.remove(name)
            except OSError as error:
                errors.append(error)
                dirs.remove(name)
        for name in files:
            path = Path(directory) / name
            try:
                identity = file_identity(path)
            except (ValueError, FileNotFoundError):
                continue
            except OSError as error:
                errors.append(error)
                continue
            yield dict(fields, path=str(path), identity=identity)
    if errors:
        notes.append(f'{root} 有 {len(errors)} 项无法读取。{errors[0]}')


def user_cache_claims(run):
    """Index even shadowed unit files/drop-ins so ambiguous sharing is excluded."""
    roots = run(['/usr/bin/systemd-analyze', '--user', 'unit-paths']).splitlines()
    if not roots or not all(Path(value).is_absolute() for value in roots):
        raise ValueError('无法确认 systemd 用户单元搜索目录')
    claims = {}
    visited = set()
    for value in roots:
        directory = Path(value)
        try:
            children = list(directory.iterdir())
        except FileNotFoundError:
            continue
        for child in children:
            if child.name.endswith('.d') and child.is_dir():
                files = [p for p in child.iterdir() if p.suffix == '.conf']
            elif child.suffix in ('.service', '.mount', '.socket', '.swap') and child.is_file():
                files = [child]
            else:
                continue
            for file in files:
                if file in visited:
                    continue
                visited.add(file)
                content = file.read_text(encoding='utf-8').replace('\\\n', ' ')
                for declaration in re.findall(r'^\s*CacheDirectory\s*=([^\n]*)', content, re.M):
                    for field in shlex.split(declaration):
                        # Extended directory syntax can add aliases. Keep only unambiguous literal namespaces.
                        for name in field.split(':')[:2]:
                            if not name:
                                continue
                            if not re.fullmatch(r'[\w./-]+', name) or '..' in Path(name).parts or Path(name).is_absolute():
                                raise ValueError('存在动态或不明确的 CacheDirectory，共享情况无法确认')
                            claims.setdefault(Path(name), set()).add(file)
    return claims


def overlapping_claims(claims, name):
    return set().union(*(units for path, units in claims.items()
                         if path == name or path in name.parents or name in path.parents))


def user_cache_root(run):
    env = dict(line.split('=', 1) for line in run(['/usr/bin/systemctl', '--user', 'show-environment']).splitlines()
               if '=' in line)
    root = Path(env.get('XDG_CACHE_HOME') or Path(env.get('HOME') or Path.home()) / '.cache')
    if (not root.is_absolute() or len(root.parts) < 3 or root in (Path.home(), *Path.home().parents)
            or '..' in root.parts or validate_plain_path(root).stat().st_uid != os.getuid()):
        raise ValueError('无法确认用户缓存根目录')
    return root


def user_cache_manifest(run, rows, notes):
    entries = []
    try:
        units = {}
        for row in rows:
            for raw in run(['/usr/bin/dpkg-query', '-L', '--', row['name']]).splitlines():
                file = Path(raw)
                if file.suffix == '.service' and file.parent in _VENDOR_USER_UNITS:
                    units[file] = row
        if not units:
            return entries
        claims = user_cache_claims(run)
        cache = user_cache_root(run)
        for name, claimants in claims.items():
            if len(claimants) != 1 or len(name.parts) != 1:
                continue
            file, = claimants
            if file not in units or '@' in file.name or overlapping_claims(claims, name) != {file}:
                continue
            row = units[file]
            owner = run(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}', '--', row['name']]).strip()
            if not apt_owner(run, file, owner):
                continue
            properties = dict(line.split('=', 1) for line in run(['/usr/bin/systemctl', '--user', 'show',
                '-p', 'FragmentPath,DropInPaths,CacheDirectory', '--', file.name]).splitlines() if '=' in line)
            if (properties.get('FragmentPath') != str(file) or properties.get('DropInPaths') != '' or
                    str(name) not in properties.get('CacheDirectory', '').split()):
                continue
            path = cache / name
            try:
                info = validate_plain_path(path).stat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not os.path.ismount(path):
                entries.append(dict(unit=file.name, cache_name=str(name), cache_root=str(path), cache_inode=(info.st_dev, info.st_ino),
                                    package=row['id'], owner=row['name'], evidence=f'{file.name} 明确声明 CacheDirectory={name}'))
    except (ValueError, OSError) as error:
        notes.append(f'systemd 用户缓存清单不可用：{error}')
        return []
    return entries


def check_user_cache(item, cache, claims, loaded):
    name = Path(item['cache_name'])
    path = cache / name
    info = validate_plain_path(path).stat()
    if (str(path) != item['cache_root'] or (info.st_dev, info.st_ino) != tuple(item['cache_inode'])
            or info.st_uid != os.getuid() or os.path.ismount(path) or overlapping_claims(claims, name)
            or item['unit'] in loaded):
        raise ValueError('用户缓存目录已变化或仍有服务声明使用')


def cache_status(run):
    output = run(['/usr/bin/systemctl', '--user', 'list-units', '--type=service', '--plain', '--no-legend', '--no-pager'])
    return user_cache_root(run), user_cache_claims(run), {line.split()[0] for line in output.splitlines() if line.strip()}


def desktop_paths(row):
    icon = os.path.expandvars(row.get('display_icon') or '').strip()
    icon = re.sub(r',\s*-?\d+\s*$', '', icon).strip().strip('"')
    command = os.path.expandvars(row.get('uninstall_command') or '').strip()
    exe = re.match(r'^(?:"([^"]+)"|([^"\s]+))(?:\s|$)', command)
    paths = [Path(icon)] if icon else []
    if exe:
        paths.append(Path(exe[1] or exe[2]))
    return {path for path in paths if path.is_absolute() and path.suffix.lower() in ('.exe', '.ico')}


def desktop_shared(path, installed, owner):
    for row in installed.values():
        if row['id'] == owner:
            continue
        root = Path(os.path.expandvars(row.get('path') or '').strip().strip('"'))
        if (root.is_absolute() and path.is_relative_to(root)) or path in desktop_paths(row):
            return True
    return False


def desktop_manifest(row, installed, notes):
    root = Path(os.path.expandvars(row.get('path') or '').strip().strip('"'))
    program_dirs = [Path(os.environ[key]) for key in ('ProgramFiles', 'ProgramFiles(x86)') if os.environ.get(key)]
    if os.environ.get('LOCALAPPDATA'):
        program_dirs.append(Path(os.environ['LOCALAPPDATA']) / 'Programs')
    if not any(base in root.parents for base in program_dirs) or '..' in root.parts:
        notes.append('缺少独立安装目录，无法核对桌面应用残留。')
        return []
    shared = [base / name for base in program_dirs for name in ('Common Files', 'WindowsApps')]
    entries = []
    for path in desktop_paths(row):
        if (root not in path.parents or '..' in path.parts or any(path.is_relative_to(base) for base in shared)
                or desktop_shared(path, installed, row['id'])):
            continue
        try:
            identity = file_identity(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            notes.append(str(error))
            continue
        entries.append(dict(path=str(path), package=row['id'], owner=row['name'], identity=identity,
                            desktop=True, evidence='卸载登记直接引用的私有安装文件'))
    return entries


def appx_cache(rows, installed, notes):
    entries = []
    seen = set()
    local = os.environ.get('LOCALAPPDATA')
    for row in rows:
        family = row.get('package_family_name', '')
        if family.casefold() in seen:
            continue
        seen.add(family.casefold())
        if not local or not re.fullmatch(r'[A-Za-z0-9.-]+_[A-Za-z0-9]+', family):
            notes.append(f"{row['name']} 缺少可核验的包系列标识或用户目录")
            continue
        if any(r.get('package_family_name', '').casefold() == family.casefold() for r in installed.values()):
            notes.append(f'{family} 仍有已登记的版本，保留共享缓存')
            continue
        for folder in ('LocalCache', 'TempState'):
            root = Path(local) / 'Packages' / family / folder
            try:
                entries.extend(cache_files(root, dict(package=row['id'], owner=row['name'], family=family,
                                           evidence=f'AppX 包系列 {family} 的 {folder} 文件'), notes))
            except FileNotFoundError:
                continue
            except (ValueError, OSError) as error:
                notes.append(str(error))
    return entries
