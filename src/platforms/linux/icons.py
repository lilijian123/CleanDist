"""只读解析 Linux 桌面图标；不启动程序或从可执行文件加载代码。"""
import configparser
import os
from pathlib import Path

import psutil

from .apps import data_dirs, desktop_entry
from .identity import process_created


def icon_dirs():
    return tuple(dict.fromkeys((Path.home() / '.icons',
                               *(root / 'icons' for root in data_dirs()),
                               *(root / 'pixmaps' for root in data_dirs()))))


def resolve_icon(value, cancel=None):
    path = Path(value)
    if path.is_absolute():
        return path if path.is_file() else None
    if not value or '/' in value or '\\' in value or value in {'.', '..'}:
        return None
    names = (value,) if path.suffix.lower() in {'.png', '.svg', '.xpm'} else tuple(
        value + suffix for suffix in ('.png', '.svg', '.xpm'))
    for root in icon_dirs():
        if cancel and cancel.is_set():
            return None
        for name in names:
            # hicolor 是通用后备；其他安装主题在其后查找，不依赖 GUI 会话。
            for pattern in (name, f'hicolor/48x48/apps/{name}', f'hicolor/scalable/apps/{name}',
                            f'hicolor/*/*/{name}', f'*/*/*/{name}'):
                for match in root.glob(pattern):
                    if cancel and cancel.is_set():
                        return None
                    if match.is_file():
                        return match
    return None


def read_icon(path, index=0, cancel=None):
    # 导入平台入口不加载 Qt，图片仅在后台请求时解码。
    from PySide6.QtCore import QBuffer, QIODevice, QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter

    if path is None or (cancel and cancel.is_set()):
        return b''
    try:
        path = Path(path)
        if path.suffix.lower() == '.desktop':
            path = resolve_icon(desktop_entry(path).get('Icon', ''), cancel)
        if path is None or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return b''
        if path.suffix.lower() == '.svg':
            # SVG 中的文字会访问 Qt 字体库；无 GUI 实例时 Qt 会直接中止进程。
            if not isinstance(QGuiApplication.instance(), QGuiApplication):
                return b''
            from PySide6.QtSvg import QSvgRenderer
            renderer = QSvgRenderer(str(path))
            if not renderer.isValid():
                return b''
            image = QImage(48, 48, QImage.Format.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            renderer.render(painter, QRectF(0, 0, 48, 48))
            painter.end()
        elif path.suffix.lower() in {'.png', '.xpm', '.jpg', '.jpeg', '.ico', '.bmp', '.webp'}:
            image = QImage(str(path))
        else:
            return b''
        if image.isNull() or (cancel and cancel.is_set()):
            return b''
        image = image.scaled(48, 48)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        return bytes(buffer.data()) if image.save(buffer, 'PNG') else b''
    except (OSError, UnicodeError, ValueError, configparser.Error):
        return b''


def load_app_icons(requests, cancel):
    result = {}
    for path, index in requests:
        if cancel.is_set():
            break
        result[(path, index)] = read_icon(path, index, cancel)
    return result


def load_process_icons(keys, cancel):
    from .apps import scan_apps

    result, by_executable, images = {}, None, {}
    for key in keys:
        if cancel.is_set():
            break
        data = b''
        try:
            process = psutil.Process(key[0])
            if process_created(process) == key[1] and process.is_running():
                executable = Path(process.exe())
                if executable.is_absolute():
                    if by_executable is None:
                        by_executable = {}
                        for app in scan_apps(cancel=cancel).apps:
                            if app.path and app.icon_path and app.source != 'Flatpak应用':
                                by_executable.setdefault(os.path.realpath(app.path), app.icon_path)
                    icon = by_executable.get(os.path.realpath(executable))
                    if icon:
                        if icon not in images:
                            images[icon] = read_icon(icon, cancel=cancel)
                        data = images[icon]
                    if process_created(process) != key[1] or not process.is_running():
                        data = b''
        except (psutil.Error, OSError, ValueError):
            pass
        result[key] = data
    return result
