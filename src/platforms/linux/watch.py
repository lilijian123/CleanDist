"""后台建立递归 inotify；覆盖不完整时返回 None，由调用方定时扫描兜底。"""
from collections import deque
import ctypes
import os
import select
import struct
import threading


_EVENT = struct.Struct('iIII')
_ISDIR = 0x40000000
_CREATE = 0x100
_MOVED_TO = 0x80
_MOVED_FROM = 0x40
_LOST = 0x4000 | 0x8000 | 0x2000 | 0x400 | 0x800  # overflow, ignored, unmount, self delete/move
_MASK = 0x2 | 0x4 | 0x8 | _CREATE | 0x200 | _MOVED_TO | _MOVED_FROM | 0x400 | 0x800


class DirectoryWatch:
    def __init__(self, root):
        self._root = os.path.abspath(root)
        self._fd = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._complete = False
        self._dirty = False
        self._thread = threading.Thread(target=self._run, name='linux-directory-watch', daemon=True)
        self._thread.start()

    @property
    def available(self):
        with self._lock:
            return self._complete and not self._stop.is_set()

    def changed(self):
        with self._lock:
            if not self._complete or self._stop.is_set():
                return None
            changed, self._dirty = self._dirty, False
            return changed

    def close(self):
        self._stop.set()
        # A filesystem syscall can block on a remote mount; never hold up the UI
        # indefinitely. The worker owns and closes its fd when that syscall returns.
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=.5)

    def _run(self):
        descriptor = None
        try:
            if os.name != 'posix':
                return
            # Lazy binding also makes imports safe in Windows platform-selection tests.
            api = ctypes.CDLL(None, use_errno=True)
            api.inotify_init1.argtypes = [ctypes.c_int]
            api.inotify_init1.restype = ctypes.c_int
            api.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            api.inotify_add_watch.restype = ctypes.c_int
            descriptor = api.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if descriptor < 0:
                raise OSError(ctypes.get_errno(), 'inotify_init1')
            self._fd = descriptor
            pending = deque([self._root])
            watched = {}
            while not self._stop.is_set():
                # Bound directory batches so live events are drained while building.
                for _ in range(min(16, len(pending))):
                    if self._stop.is_set():
                        return
                    path = pending.popleft()
                    wd = api.inotify_add_watch(descriptor, os.fsencode(path), _MASK | 0x01000000 | 0x02000000)
                    if wd < 0:
                        raise OSError(ctypes.get_errno(), 'inotify_add_watch')
                    if wd in watched:
                        continue
                    watched[wd] = path
                    with os.scandir(path) as entries:
                        for entry in entries:
                            if self._stop.is_set():
                                return
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                with self._lock:
                    if not pending and not self._complete:
                        # A scan after subscription catches changes during tree setup.
                        self._dirty = True
                    self._complete = not pending
                if not select.select([descriptor], [], [], 0 if pending else .1)[0]:
                    continue
                while not self._stop.is_set():
                    try:
                        data = os.read(descriptor, 65536)
                    except BlockingIOError:
                        break
                    if not data:
                        raise OSError('inotify closed')
                    offset = 0
                    while offset + _EVENT.size <= len(data):
                        wd, mask, _, size = _EVENT.unpack_from(data, offset)
                        start = offset + _EVENT.size
                        end = start + size
                        if end > len(data) or mask & _LOST:
                            raise OSError('inotify coverage lost')
                        if mask & _ISDIR and mask & _MOVED_FROM:
                            # Stored subtree paths are invalid after a move out/rename.
                            # Conservatively use periodic scans instead of stale watches.
                            raise OSError('watched directory moved')
                        with self._lock:
                            self._dirty = True
                        if mask & _ISDIR and mask & (_CREATE | _MOVED_TO):
                            name = os.fsdecode(data[start:end].split(b'\0', 1)[0])
                            pending.append(os.path.join(watched[wd], name))
                            with self._lock:
                                self._complete = False
                        offset = end
                    if offset != len(data):
                        raise OSError('incomplete inotify event')
        except (OSError, AttributeError, KeyError, ValueError):
            # Permissions, watch limits, disappearing directories and queue overflow
            # must all select the caller's 30-second scan fallback.
            pass
        finally:
            with self._lock:
                self._complete = False
            if descriptor is not None and descriptor >= 0:
                os.close(descriptor)
            self._fd = None
