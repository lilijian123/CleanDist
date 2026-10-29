"""Windows 私有内存及线程创建身份；原生绑定在调用时初始化。"""
from functools import lru_cache

import psutil


@lru_cache(maxsize=1)
def _thread_api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.GetProcessIdOfThread.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessIdOfThread.restype = wintypes.DWORD
    kernel.GetThreadTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.GetThreadTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def thread_identity(process, tid):
    import ctypes
    from ctypes import wintypes
    kernel = _thread_api()
    handle = kernel.OpenThread(0x0800, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # 已退出的 TID。
            return None
        raise ctypes.WinError(error)
    try:
        if kernel.GetProcessIdOfThread(handle) != process.pid:
            return None
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetThreadTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                     ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def sample_memory_breakdown(process):
    result = dict.fromkeys(('rss', 'uss', 'pss', 'anonymous', 'file_pss',
                            'shared_pss', 'swap', 'private_commit'))
    errors = []
    try:
        info = process.memory_info()
        result.update(rss=info.rss, private_commit=getattr(info, 'private', None))
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as error:
        errors.append(f'驻留/私有提交：{type(error).__name__}')
    try:
        result['uss'] = process.memory_full_info().uss
    except (psutil.AccessDenied, psutil.ZombieProcess, OSError) as error:
        errors.append(f'私有驻留（USS）：{type(error).__name__}')
    result.update(mappings=(), errors=tuple(errors))
    return result
