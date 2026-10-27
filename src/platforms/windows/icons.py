"""只读提取图标资源并转换为图片数据，不启动或加载目标程序代码。"""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import re
import psutil


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


@lru_cache(maxsize=128)
def process_icon(path, modified, size):
    # 多个进程常共用一个程序；文件版本变化后自然使用新的缓存键。
    return read_icon(path)


def load_process_icons(keys, cancel):
    result = {}
    for key in keys:
        if cancel.is_set():
            break
        data = b''
        try:
            process = psutil.Process(key[0])
            if process.create_time() == key[1] and process.is_running():
                path = Path(process.exe())
                if path.is_absolute():
                    info = path.stat()
                    data = process_icon(path, info.st_mtime_ns, info.st_size)
                    if not process.is_running():
                        data = b''
        except (psutil.Error, OSError, ValueError):
            pass
        result[key] = data
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
