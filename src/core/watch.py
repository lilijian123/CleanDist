"""非阻塞接收 Windows 目录子树变化通知，调用方保留定时扫描作为兜底。"""
import ctypes
from ctypes import wintypes
import os


class DirectoryWatch:
    def __init__(self, root):
        self.handle = None
        if os.name != 'nt':
            return
        api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
        api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        api.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
        api.FindNextChangeNotification.restype = wintypes.BOOL
        api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        api.FindCloseChangeNotification.restype = wintypes.BOOL
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        path = os.path.abspath(root)
        if not path.startswith('\\\\?\\'):
            path = '\\\\?\\UNC\\' + path[2:] if path.startswith('\\\\') else '\\\\?\\' + path
        handle = api.FindFirstChangeNotificationW(path, True, 0x1 | 0x2 | 0x4 | 0x8 | 0x10)
        if handle not in (None, ctypes.c_void_p(-1).value):
            self.handle = handle

    def changed(self):
        if self.handle is None:
            return None
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result == 258:  # WAIT_TIMEOUT 表示未发生变化，无需重新启用尚未触发的通知句柄。
            return False
        if result != 0 or not self.api.FindNextChangeNotification(self.handle):
            self.close()
        return True

    def close(self):
        if self.handle is not None:
            self.api.FindCloseChangeNotification(self.handle)
            self.handle = None
