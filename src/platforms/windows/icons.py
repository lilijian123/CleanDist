"""只读提取图标资源并转换为图片数据，不启动或加载目标程序代码。"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re


def icon_location(value):
    value = os.path.expandvars(str(value or '')).strip()
    match = re.search(r',\s*(-?\d+)\s*$', value)
    index = int(match.group(1)) if match else 0
    path = (value[:match.start()] if match else value).strip().strip('"')
    return (Path(path) if path and Path(path).is_absolute() else None), index


def load_app_icons(requests, cancel):
    result = {}
    for path, index in requests:
        if cancel.is_set():
            break
        result[(path, index)] = read_icon(path, index)
    return result


def read_icon(path, index=0):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    if path is None:
        return b''
    handle = wintypes.HICON()
    destroy = None
    try:
        if not path.is_file():
            return b''
        if path.suffix.casefold() in {'.png', '.ico', '.jpg', '.jpeg', '.bmp'}:
            image = QImage(str(path))
        else:
            extract = ctypes.WinDLL('shell32').ExtractIconExW
            extract.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
                                ctypes.POINTER(wintypes.HICON), wintypes.UINT]
            extract.restype = wintypes.UINT
            destroy = ctypes.WinDLL('user32').DestroyIcon
            destroy.argtypes = [wintypes.HICON]
            destroy.restype = wintypes.BOOL
            extract(str(path), index, ctypes.byref(handle), None, 1)
            image = QImage.fromHICON(handle.value) if handle.value else QImage()
        if image.isNull():
            return b''
        image = image.scaled(48, 48)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        return bytes(buffer.data()) if image.save(buffer, 'PNG') else b''
    except (OSError, ValueError):
        return b''
    finally:
        # 提取的独立图标句柄必须释放，避免每次扫描累积系统资源。
        if handle.value and destroy:
            destroy(handle)
