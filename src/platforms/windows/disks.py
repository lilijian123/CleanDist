"""读取 Windows 可选盘符，不访问盘内文件。"""
import ctypes
from ctypes import wintypes
from pathlib import Path


def list_disks():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetLogicalDrives.argtypes = []
    api.GetLogicalDrives.restype = wintypes.DWORD
    mask = api.GetLogicalDrives()
    if not mask:
        raise ctypes.WinError(ctypes.get_last_error())
    return tuple(Path(f'{chr(65 + index)}:/') for index in range(26) if mask & (1 << index))
