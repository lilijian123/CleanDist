"""Vue生产资源与pywebview宿主；Windows固定WebView2，Linux使用WebKit。"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def ensure_frontend():
    frontend = ROOT / 'frontend'
    index = frontend / 'dist' / 'index.html'
    manifests = [frontend/'package.json', frontend/'package-lock.json']
    sources = [*manifests, frontend/'index.html', frontend/'vite.config.js']
    sources.extend((frontend/'src').rglob('*'))
    stale = not index.exists() or any(path.is_file() and path.stat().st_mtime > index.stat().st_mtime
                                     for path in sources if path.exists())
    if not stale: return index
    npm = shutil.which('npm.cmd' if sys.platform == 'win32' else 'npm')
    if not npm:
        raise RuntimeError('网页界面尚未构建。请安装Node.js 22.12以上，在frontend目录运行 npm ci 和 npm run build。')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    installed = frontend/'node_modules'/'.package-lock.json'
    if not installed.exists() or any(path.exists() and path.stat().st_mtime > installed.stat().st_mtime for path in manifests):
        subprocess.run([npm, 'ci'], cwd=frontend, check=True, **options)
    subprocess.run([npm, 'run', 'build'], cwd=frontend, check=True, **options)
    if not index.exists(): raise RuntimeError('网页资源构建未生成index.html')
    return index


def prepare_linux():
    if sys.platform != 'linux': return
    # WebKit 的本机静态资源必须直连，避免环境代理把请求改成绝对URI。
    for name in ('no_proxy', 'NO_PROXY'):
        values = os.environ.get(name, '').split(',')
        os.environ[name] = ','.join(dict.fromkeys(values + ['127.0.0.1', 'localhost', '::1']))
    # Ubuntu的PyGObject来自apt；venv默认不包含发行版dist-packages。
    if importlib.util.find_spec('gi') is None:
        import site
        system_packages = Path('/usr/lib/python3/dist-packages')
        if system_packages.is_dir(): site.addsitedir(str(system_packages))


def main(demo=False):
    index = ensure_frontend()
    prepare_linux()
    import webview
    from .api import DesktopApi
    api = DesktopApi()
    window = webview.create_window('CleanDist', str(index), js_api=api, width=1260, height=870,
                                  min_size=(1000, 700), text_select=True, background_color='#151515')
    api._window = window
    window.events.closed += api._shutdown
    if demo: window.events.loaded += lambda: api.call('demo', {'enabled': True})
    try:
        webview.start(gui='edgechromium' if sys.platform == 'win32' else 'gtk',
                      private_mode=True, http_server=True)
    finally:
        api._shutdown()
    return 0
