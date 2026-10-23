"""从已打开的文件句柄读取分配量，不修改文件和磁盘。"""
import ctypes
from ctypes import wintypes
import msvcrt
import os


class StandardInfo(ctypes.Structure):
    _fields_ = [('allocated', ctypes.c_longlong), ('size', ctypes.c_longlong),
                ('links', wintypes.DWORD), ('deleting', ctypes.c_ubyte), ('directory', ctypes.c_ubyte)]


class CompressionInfo(ctypes.Structure):
    _fields_ = [('allocated', ctypes.c_longlong), ('format', wintypes.WORD),
                ('unit', ctypes.c_ubyte), ('chunk', ctypes.c_ubyte),
                ('cluster', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte * 3)]


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_query = _kernel.GetFileInformationByHandleEx
_query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_query.restype = wintypes.BOOL
_open = _kernel.CreateFileW
_open.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_open.restype = wintypes.HANDLE
_close = _kernel.CloseHandle
_close.argtypes = [wintypes.HANDLE]
_close.restype = wintypes.BOOL


def open_metadata(path):
    # 只申请读取属性权限；空间统计不打开文件内容，也不跟随末级重解析点。
    name = str(path)
    if not name.startswith('\\\\?\\'):
        name = '\\\\?\\UNC\\' + name[2:] if name.startswith('\\\\') else '\\\\?\\' + name
    handle = _open(name, 0x80, 7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        _close(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except Exception:
        os.close(descriptor)
        raise


def allocated_bytes(stream):
    handle = msvcrt.get_osfhandle(stream.fileno())
    compressed = bool(os.fstat(stream.fileno()).st_file_attributes & (0x800 | 0x200))
    info = CompressionInfo() if compressed else StandardInfo()
    if not _query(handle, 8 if compressed else 1, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return max(0, info.allocated)
