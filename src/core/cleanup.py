"""基于已扫描记录和明确路径的清理分类；不读取文件内容、不执行工具。"""
from dataclasses import dataclass, field
import os
import ntpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import posixpath
import time

import psutil

from ..platforms import system


CATEGORIES = {'system': '系统缓存与临时文件', 'apps': '应用缓存', 'browser': '浏览器缓存',
              'dev': '开发工具缓存', 'build': '项目构建产物', 'logs': '日志与崩溃转储',
              'managed': 'AI 与容器数据', 'protected': '系统维护数据'}
RISKS = {'low': '低风险', 'medium': '中风险', 'high': '高风险 · 仅查看'}
WEEK = 7 * 86400


@dataclass
class CleanupItem:
    key: str
    category: str
    name: str
    risk: str
    reason: str
    records: list = field(default_factory=list)
    recommended: bool = False

    @property
    def size(self):
        return sum(record.size for record in self.records)


@dataclass
class CleanupAnalysis:
    snapshot: object
    items: tuple
    by_file: dict

    @property
    def cleanable_bytes(self):
        return sum(item.size for item in self.items if item.risk != 'high')

    def recommended_records(self):
        return [record for item in self.items if item.recommended for record in item.records]

    def readonly_records(self):
        return [record for item in self.items if item.risk == 'high' for record in item.records]


def running_applications():
    """只查进程名；不能完整观测时，缓存不进入一键推荐。"""
    names = set()
    try:
        for process in psutil.process_iter(['name']):
            name = process.info['name']
            if not name:
                return None
            names.add(name.casefold().removesuffix('.exe'))
    except (psutil.Error, OSError):
        return None
    return names


_AUTO = object()


def classify_cleanup(snapshot, *, platform=None, environ=None, home=None, now=None, running=_AUTO, cancel=None):
    """精确目录前缀匹配；未知文件不成为清理候选。同一文件只有一个归属。"""
    windows = (platform or system.NAME) == 'Windows'
    environ = os.environ if environ is None else environ
    home = home if home is not None else Path.home()
    home = home.as_posix() if isinstance(home, Path) else str(home)
    now = time.time() if now is None else now
    if cancel is not None and cancel.is_set():
        return None
    running = running_applications() if running is _AUTO else running
    running = None if running is None else {name.casefold().removesuffix('.exe') for name in running}

    def parts(path):
        path = path.as_posix() if isinstance(path, Path) else str(path)
        values = (PureWindowsPath(ntpath.normpath(path)).parts if windows else PurePosixPath(posixpath.normpath(path)).parts)
        return tuple(value.casefold() if windows else value for value in values)

    rules = {}

    def add(path, key, category, name, risk, reason, mode='all', owners=()):
        if path:
            path = str(path)
            if path == '~' or path.startswith(('~/', '~\\')):
                path = home + '/' + path[2:] if len(path) > 1 else home
            pathmod = ntpath if windows else posixpath
            if not pathmod.isabs(path) and not os.path.isabs(path) and not PureWindowsPath(path).is_absolute():
                path = pathmod.join(Path.cwd().as_posix(), path)
            rules.setdefault(parts(path), []).append((key, category, name, risk, reason, mode, owners))

    def join(root, *tail):
        return str((PureWindowsPath(root) if windows else PurePosixPath(root)).joinpath(*tail))

    local = environ.get('LOCALAPPDATA', join(home, 'AppData', 'Local'))
    roaming = environ.get('APPDATA', join(home, 'AppData', 'Roaming'))
    cache = environ.get('XDG_CACHE_HOME', join(home, '.cache'))
    windir = environ.get('WINDIR', 'C:/Windows')
    temp_reason = '至少 7 天未修改的已知临时目录文件；先关闭相关程序，文件夹保留。'
    for path in ({environ.get('TEMP'), environ.get('TMP'), join(local, 'Temp'), join(windir, 'Temp')} if windows
                 else {environ.get('TMPDIR'), '/tmp', '/var/tmp'}):
        add(path, 'temp', 'system', '过期临时文件', 'low', temp_reason, 'old')
    if windows:
        add(join(local, 'D3DSCache'), 'directx', 'system', 'DirectX 着色器缓存', 'low', '可重新生成；下次启动游戏可能重新编译着色器。')
        add(join(local, 'Microsoft/Windows/Explorer'), 'thumbnails', 'system', '文件缩略图缓存', 'low', '可重新生成；下次浏览文件可能较慢。', 'thumb')
        add(join(local, 'CrashDumps'), 'dumps', 'logs', '旧崩溃转储', 'medium', '至少 7 天未修改；清理后无法用于排查对应崩溃。', 'dump')
        add(join(windir, 'Logs'), 'winlogs', 'logs', '旧 Windows 日志', 'medium', '至少 7 天未修改；清理会丢失历史诊断信息，可能需要管理员权限。', 'log')
        add(join(windir, 'SoftwareDistribution'), 'updates', 'protected', 'Windows 更新组件', 'high', '由 Windows 管理；请通过系统存储设置清理。')
        add(join(windir, 'WinSxS'), 'components', 'protected', 'Windows 组件存储', 'high', '由 Windows 维护，不支持逐文件清理。')
        add(join(local, 'Microsoft/Windows/WER'), 'wer', 'logs', '旧 Windows 错误报告', 'medium', '至少 7 天未修改；清理会丢失故障报告。', 'old')
    else:
        add(join(cache, 'thumbnails'), 'thumbnails', 'system', '文件缩略图缓存', 'low', '可重新生成；下次浏览文件可能较慢。')
        add(join(environ.get('XDG_DATA_HOME', join(home, '.local/share')), 'Trash'), 'trash', 'protected', '回收站数据', 'high', '请在桌面回收站管理，不将内部数据再次移入回收站。')

    for path in ((join(local, 'pip/Cache'),) if windows else (join(cache, 'pip'),)):
        add(path, 'pip', 'dev', 'pip 包下载缓存', 'medium', '清理后需要重新下载或构建 Python 包，离线安装可能受影响。')
    for path in ({join(local, 'npm-cache'), join(roaming, 'npm-cache')} if windows else {join(home, '.npm')}):
        add(join(path, '_cacache'), 'npm', 'dev', 'npm 包下载缓存', 'medium', '清理后需要重新下载依赖，离线安装可能受影响。')
    add(join(environ.get('GRADLE_USER_HOME', join(home, '.gradle')), 'caches'), 'gradle', 'dev', 'Gradle 依赖与构建缓存', 'medium', '需重新下载依赖或重新构建，离线构建可能受影响。')
    add(join(home, '.m2/repository'), 'maven', 'dev', 'Maven 本地依赖', 'medium', '可能包含本地发布且无法重新下载的依赖，请确认后选择。')
    for path in (join(cache, 'mesa_shader_cache'), join(cache, 'mesa_shader_cache_db')) if not windows else ():
        add(path, 'mesa', 'apps', 'Mesa 着色器缓存', 'low', '可重新生成；后续启动图形程序可能重新编译着色器。')
    vscode_roots = (join(roaming, 'Code'),) if windows else (join(home, '.config/Code'),)
    for root in vscode_roots:
        for folder in ('Cache', 'CachedData', 'GPUCache'):
            add(join(root, folder), 'vscode', 'apps', 'Visual Studio Code 缓存', 'low', '先关闭 VS Code；缓存可重新生成，工作区和设置不在清理范围。', owners=('code', 'code-insiders'))

    browser_roots = ((join(local, 'Google/Chrome/User Data'), join(local, 'Microsoft/Edge/User Data')) if windows else
                     (join(cache, 'google-chrome'), join(cache, 'chromium'), join(cache, 'microsoft-edge'),
                      join(home, '.config/google-chrome'), join(home, '.config/chromium'), join(home, '.config/microsoft-edge')))
    for root in browser_roots:
        edge = (parts(root) == parts(join(local, 'Microsoft/Edge/User Data')) if windows else parts(root)[-1] == 'microsoft-edge')
        add(root, 'edge' if edge else 'chrome', 'browser', 'Edge 缓存' if edge else 'Chrome / Chromium 缓存', 'low',
            '先关闭浏览器；仅磁盘/代码/GPU 缓存，首次打开页面可能变慢，不包含登录信息。', 'chromium',
            ('msedge', 'microsoft-edge') if edge else ('chrome', 'google-chrome', 'chromium', 'chromium-browser'))
    firefox_roots = ((join(local, 'Mozilla/Firefox/Profiles'),) if windows else
        (join(cache, 'mozilla/firefox'), join(home, 'snap/firefox/common/.cache/mozilla/firefox'),
         join(home, '.var/app/org.mozilla.firefox/cache/mozilla/firefox')))
    for firefox in firefox_roots:
        add(firefox, 'firefox', 'browser', 'Firefox 磁盘缓存', 'low', '先关闭 Firefox；仅 cache2/startupCache，网页可能需要重新加载。', 'firefox', ('firefox',))

    for path in {join(cache, 'huggingface'), join(home, '.cache/huggingface'), environ.get('HF_HOME'), environ.get('HF_HUB_CACHE'),
                 environ.get('HUGGINGFACE_HUB_CACHE'), join(home, '.ollama/models'), environ.get('OLLAMA_MODELS')}:
        add(path, 'models', 'managed', 'AI 模型与下载数据', 'high', '模型可能被引用或无法重新下载；请用 Hugging Face / Ollama 对应工具管理。')
    for path in ((join(local, 'Docker'), join(local, 'containers')) if windows else ('/var/lib/docker', '/var/lib/containers', join(home, '.local/share/containers'))):
        add(path, 'containers', 'managed', '容器镜像与数据', 'high', '可能包含镜像、卷和服务数据；请使用容器工具管理，禁止逐文件回收。')

    root_parts = parts(snapshot.root)
    relative_parents = {}

    def relative_parts(relative):
        # 扫描器产生规范相对路径；共享父目录只解析一次，异常路径仍走规范化规则。
        value = relative.replace('\\', '/') if windows else relative
        parent, _, name = value.rpartition('/')
        if not name or name in ('.', '..') or value.startswith('/') or (windows and ntpath.splitdrive(value)[0]):
            return parts(relative)
        if parent not in relative_parents:
            relative_parents[parent] = parts(parent) if parent else ()
        return relative_parents[parent] + (name.casefold() if windows else name,)

    manifest_dirs = {}
    marker_names = {'cargo.toml' if windows else 'Cargo.toml', 'pom.xml', 'package.json'}
    for index, relative in enumerate(snapshot.files):
        if index % 4096 == 0 and cancel is not None and cancel.is_set():
            return None
        name = relative.rsplit('/', 1)[-1]
        name = name.casefold() if windows else name
        if name in marker_names:
            path = root_parts + relative_parts(relative)
            manifest_dirs.setdefault(path[:-1], set()).add(name)
    protected_names = {'windows.old', '$recycle.bin'} if windows else set()
    sensitive = {'cookies', 'login data', 'history', 'preferences', 'credentials', 'token', 'tokens',
                 'id_rsa', 'id_ed25519', '.npmrc', '.pypirc', 'settings.xml'}
    environment_dirs = {'.git', '.venv', 'venv', 'node_modules'}
    protected_rules = {prefix: next(rule for rule in entries if rule[3] == 'high')
                       for prefix, entries in rules.items() if any(rule[3] == 'high' for rule in entries)}
    items, by_file = {}, {}
    for index, (relative, record) in enumerate(snapshot.files.items()):
        if index % 4096 == 0 and cancel is not None and cancel.is_set():
            return None
        path = root_parts + relative_parts(relative)
        matched = None
        if any(value in protected_names for value in path):
            matched = ('maintenance', 'protected', '系统维护 / 回收站数据', 'high', '请使用系统存储或回收站管理；不逐文件处理维护数据。', 'all', ())
        if not matched:
            # 保护规则优先于任何更深的缓存/临时目录，包括环境变量指定的重叠位置。
            matched = next((protected_rules[path[:length]] for length in range(len(path)-1, 0, -1)
                            if path[:length] in protected_rules), None)
        for length in range(len(path) - 1, 0, -1):
            if matched:
                break
            for rule in rules.get(path[:length], ()):
                tail, mode = path[length:], rule[5]
                age = now - record.modified_ns / 1e9
                if mode in ('old', 'log', 'dump') and age < WEEK:
                    continue
                if mode == 'log' and not path[-1].casefold().endswith(('.log', '.etl', '.cab')):
                    continue
                if mode == 'dump' and not path[-1].casefold().endswith('.dmp'):
                    continue
                if mode == 'thumb' and not (len(tail) == 1 and tail[0].startswith('thumbcache_') and tail[0].endswith('.db')):
                    continue
                if mode == 'chromium' and not (len(tail) >= 3 and (tail[0] in (('default', 'guest profile') if windows else ('Default', 'Guest Profile')) or tail[0].startswith('profile ' if windows else 'Profile '))
                                                       and tail[1] in (('cache', 'code cache', 'gpucache') if windows else ('Cache', 'Code Cache', 'GPUCache'))):
                    continue
                if mode == 'firefox' and not (len(tail) >= 3 and tail[1] in (('cache2', 'startupcache') if windows else ('cache2', 'startupCache'))):
                    continue
                matched = rule
                break
        if not matched and not any(value in environment_dirs for value in path):
            if '__pycache__' in path[:-1] and path[-1].endswith('.pyc'):
                matched = ('pycache', 'build', 'Python 字节码缓存', 'medium', '可重新生成；首次启动 Python 程序可能重新编译。', 'all', ())
            else:
                for length in range(len(path) - 1, 0, -1):
                    markers = manifest_dirs.get(path[:length], ())
                    folder = path[length] if length < len(path) else ''
                    if ((folder == 'target' and any(name in markers for name in ('cargo.toml' if windows else 'Cargo.toml', 'pom.xml')))
                            or (folder == '.next' and 'package.json' in markers)):
                        matched = ('build', 'build', '已识别项目的构建产物', 'medium', '通过 Cargo.toml / pom.xml / package.json 确认项目；清理后需重新构建。', 'all', ())
                        break
        if not matched:
            continue
        key, category, name, risk, reason, mode, owners = matched
        basename = path[-1].casefold()
        document_types = ('.txt', '.md', '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.csv', '.jpg', '.png', '.mp4', '.mp3')
        source_types = ('.py', '.pyi', '.c', '.h', '.cpp', '.hpp', '.rs', '.go', '.java', '.js', '.ts', '.tsx', '.jsx', '.json', '.yaml', '.yml', '.toml', '.xml', '.ini', '.cfg')
        if risk != 'high' and (any(value in environment_dirs for value in path)
                               or any(value.casefold() in ('documents', 'downloads', 'desktop', 'pictures', 'videos', 'music') for value in path)
                               or (category == 'system' and key == 'temp' and basename.endswith(document_types + source_types))
                               or any(value in ('.ssh', '.gnupg', '.aws', '.azure', '.kube') for value in path)
                               or basename in sensitive or basename.startswith('.env') or basename.endswith(('.key', '.pem', '.pfx', '.p12', '.sqlite', '.sqlite3'))
                               or (basename.endswith('.db') and mode != 'thumb')):
            continue
        recommend = risk == 'low' and (not owners or (running is not None and not any(owner in running for owner in owners)))
        if owners and not recommend:
            reason += ' 所属应用正在运行或状态不可确认，未加入一键推荐。'
        if key not in items:
            items[key] = CleanupItem(key, category, name, risk, reason, recommended=recommend)
        items[key].records.append(record)
        by_file[relative] = items[key]
    order = {key: number for number, key in enumerate(CATEGORIES)}
    for item in items.values():
        item.records.sort(key=lambda record: (-record.size, record.relative))
    result = sorted(items.values(), key=lambda item: (order[item.category], ('low', 'medium', 'high').index(item.risk), -item.size))
    return CleanupAnalysis(snapshot, tuple(result), by_file)
