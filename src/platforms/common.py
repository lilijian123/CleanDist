"""通用兼容行为，不包含平台原生扫描或变化通知。"""
from contextlib import contextmanager
import os
import stat
import sys
from types import SimpleNamespace


NAME = '兼容模式'
FONT_FAMILY = 'sans-serif'
SCAN_NAME = '兼容扫描'


def list_disks():
    return ()


def scan_apps(roots=None, cancel=None, progress=None):
    raise NotImplementedError('当前平台尚未适配应用扫描')


def load_app_icons(requests, cancel):
    return {key: b'' for key in requests if not cancel.is_set()}


def allocated_bytes(stream):
    info = os.fstat(stream.fileno())
    if not hasattr(info, 'st_blocks'):
        raise OSError('当前平台无法读取文件分配量')
    return info.st_blocks * 512


@contextmanager
def open_metadata(path):
    descriptor = os.open(path, getattr(os, 'O_PATH', os.O_RDONLY) | getattr(os, 'O_NOFOLLOW', 0))
    # 元数据路径只需要文件描述符，避免申请文件内容读取权限。
    try:
        yield SimpleNamespace(fileno=lambda: descriptor)
    finally:
        os.close(descriptor)


def scan_native(root, cancel, limit, progress):
    return None


def is_link(info):
    return stat.S_ISLNK(info.st_mode)


def recycle(path):
    from send2trash import send2trash
    send2trash(str(path))


def python_executable():
    return sys.executable


def process_options():
    return {}


class DirectoryWatch:
    """无原生通知时，由调用方使用定时扫描。"""
    available = False

    def __init__(self, root):
        pass

    def changed(self):
        return None

    def close(self):
        pass
