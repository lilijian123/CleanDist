"""通用兼容行为，不包含平台原生扫描或变化通知。"""
import stat
import sys


NAME = '兼容模式'
FONT_FAMILY = 'sans-serif'
SCAN_NAME = '兼容扫描'


def list_disks():
    return ()


def scan_apps(roots=None, cancel=None, progress=None):
    raise NotImplementedError('当前平台尚未适配应用扫描')


def load_app_icons(requests, cancel):
    return {key: b'' for key in requests if not cancel.is_set()}


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
