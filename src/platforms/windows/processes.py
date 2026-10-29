"""按用户选择普通或强制结束单个进程，持有句柄以阻止PID复用。"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess

from ...core.processes import matching_process, StopResult


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_open = _kernel.OpenProcess
_open.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_open.restype = wintypes.HANDLE
_close = _kernel.CloseHandle
_close.argtypes = [wintypes.HANDLE]
_close.restype = wintypes.BOOL
_wait = _kernel.WaitForSingleObject
_wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_wait.restype = wintypes.DWORD
_critical = _kernel.IsProcessCritical
_critical.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_critical.restype = wintypes.BOOL


@contextmanager
def guard_process(key):
    pid, _ = key
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('无效的PID')
    if pid in (4, os.getpid()):
        raise ValueError('不允许通过此操作结束系统进程或本应用自身')
    handle = _open(0x100000 | 0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # 先持有进程对象，再验证创建时间，避免验证与taskkill之间PID被再次分配。
        matching_process(key)
        if _wait(handle, 0) != 258:
            raise ValueError('进程已经退出，请刷新列表')
        critical = wintypes.BOOL()
        if not _critical(handle, ctypes.byref(critical)):
            raise ctypes.WinError(ctypes.get_last_error())
        if critical.value:
            raise ValueError('不允许结束Windows关键进程')
        yield handle
    finally:
        _close(handle)


def stop_process(key, *, force=False):
    with guard_process(key) as handle:
        command = [str(Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe'), '/PID', str(key[0])]
        if force:
            command.append('/F')
        mode = '强制结束' if force else '普通结束'
        try:
            result = subprocess.run(command, capture_output=True, timeout=12,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f'{mode}请求超时，进程状态尚未确认，请刷新') from error
        output = (result.stderr or result.stdout).decode(f'cp{_kernel.GetOEMCP()}', errors='replace').strip()
        if result.returncode:
            raise OSError(f'{mode}未成功：{output or result.returncode}')
        exited = _wait(handle, 1500) == 0
        message = '进程已结束' if exited else f'已发送{mode}请求，进程尚未退出，请刷新查看状态'
        return StopResult(key, exited, message)
