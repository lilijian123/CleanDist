"""Optional strace attachment; never changes ptrace settings or signals the target.

Only paths and successful read/write syscall return counts are retained. -s 0
prevents strace from emitting buffer contents, including readv/writev buffers.
"""
from collections import deque
import codecs
import os
from pathlib import Path
import re
import select
import selectors
import shutil
import signal
import subprocess
import threading

import psutil

from ...core.processes import matching_process
from ...core.process_trace import TraceEvent, TraceState


_OPERATIONS = 'read,write,pread64,pwrite64,readv,writev,preadv,pwritev,preadv2,pwritev2'
_COVERAGE = '仅读写系统调用返回字节；不覆盖 mmap 修改、缓存落盘或开始前的行为；耗时含等待/调度，不等于设备硬件延迟；受 ptrace 权限限制'
_MAX_LINE = 16384
_HEADER = re.compile(r'^(?:(?:\[pid\s+(\d+)\]|(\d+))\s+)?(\d+\.\d+)\s+(.*)$')
_CALL = re.compile(r'^(' + _OPERATIONS.replace(',', '|') + r')\(\d+<([^>]+)>,')
_RESUMED = re.compile(r'^<\.\.\. (\w+) resumed>')
_RESULT = re.compile(r'\)\s+=\s+(-?\d+)(?:\s+[A-Z][A-Z0-9_]*(?:\s+\([^\r\n]*\))?)?(?:\s+<(\d+\.\d+)>)?\s*$')
_ESCAPE = re.compile(r'\\(?:[\\\"\'abfnrtv]|[0-7]{1,3}|x[0-9a-fA-F]{2})')


class _IdentityError(ValueError):
    """Only identity validation failures use this exception."""


class _TraceParseError(ValueError):
    """Invalid syscall data, distinct from process identity changes."""


def _start_ticks(pid):
    """/proc field 22 is monotonic; epoch create_time can drift with btime."""
    try:
        stat = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError as error:
        raise ProcessLookupError('目标进程已退出') from error
    try:
        return int(stat.rsplit(b')', 1)[1].split()[19])
    except (IndexError, ValueError) as error:
        raise _IdentityError('无法读取进程启动时钟，无法继续校验身份') from error


class _TraceParser:
    """Strict bounded parser. Pending syscalls keep metadata, never arguments."""
    def __init__(self, pid):
        self.pid = pid
        self.pending = {}
        self.unparsed = 0

    def feed(self, line):
        header = _HEADER.fullmatch(line)
        if not header:
            self.unparsed += 1
            return None
        tid = int(header[1] or header[2] or self.pid)
        timestamp = float(header[3])
        body = header[4]
        if body.startswith(('+++ ', '--- ')):
            if tid in self.pending:
                self.pending.pop(tid)
                self.unparsed += 1
            return None
        resumed = _RESUMED.match(body)
        if resumed:
            saved = self.pending.pop(tid, None)
            if saved is None or saved[0] != resumed[1]:
                self.unparsed += 1
                return None
            operation, path = saved
        else:
            call = _CALL.match(body)
            if not call:
                # Known non-path descriptors (sockets/pipes/anon_inode) are not files.
                if re.match(r'^(?:' + _OPERATIONS.replace(',', '|') + r')\(\d+<(?:pipe:|socket:|TCP|UDP|UNIX|anon_inode:)', body):
                    return None
                self.unparsed += 1
                return None
            operation, path = call.groups()
            if not path.startswith('/'):
                return None
            # Decode only strace's documented C/octal escapes; unfamiliar forms are unknown.
            if '\\' in _ESCAPE.sub('', path):
                self.unparsed += 1
                return None
            path = os.fsdecode(codecs.escape_decode(os.fsencode(path))[0])
            if body.endswith('<unfinished ...>'):
                if tid in self.pending or len(self.pending) >= 256:
                    self.unparsed += 1
                    self.pending.pop(tid, None)
                else:
                    self.pending[tid] = (operation, path)
                return None
        result = _RESULT.search(body)
        if result is None:
            self.unparsed += 1
            return None
        count = int(result[1])
        if count <= 0:
            return None
        return TraceEvent(timestamp, tid, operation, path, count,
                          float(result[2]) if result[2] is not None else None)


class FileTrace:
    def __init__(self, key):
        self.key = key
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._events = deque(maxlen=1000)
        self._dropped = 0
        self._unparsed = 0
        self._active = False
        self._status = '尚未开启文件读写跟踪；' + _COVERAGE
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None or self._cancel.is_set():
                return
            self._active = True
            self._status = '正在启动跟踪；' + _COVERAGE
            self._thread = threading.Thread(target=self._run, daemon=False, name='file-syscall-trace')
            self._thread.start()

    def snapshot(self):
        with self._lock:
            return TraceState(self.key, self._active, self._status, tuple(self._events),
                              self._dropped, self._unparsed,
                              self._thread is None or not self._thread.is_alive())

    def close(self):
        # GUI cancellation never waits for strace to detach. The worker owns cleanup.
        self._cancel.set()
        with self._lock:
            if self._active:
                self._active = False
                self._status = '已请求停止跟踪器；' + _COVERAGE

    def _set_status(self, status, active=False):
        with self._lock:
            self._active = active and not self._cancel.is_set()
            self._status = status + '；' + _COVERAGE

    def _append(self, event):
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._dropped += 1
            self._events.append(event)

    def _belongs(self, tid):
        try:
            # -f is needed for threads, but also follows child processes: exclude those.
            for line in Path(f'/proc/{tid}/status').read_text().splitlines():
                if line.startswith('Tgid:'):
                    return int(line.split()[1]) == self.key[0]
        except (OSError, ValueError):
            pass
        return False

    def _run(self):
        descriptor = None
        tracer = None
        reader = None
        parser = _TraceParser(self.key[0])
        final = '跟踪已停止'
        try:
            if self.key[0] == os.getpid():
                self._set_status('不跟踪本工具自身，避免自跟踪反馈')
                return
            executable = shutil.which('strace')
            if executable is None:
                self._set_status('未安装可选工具 strace，无法跟踪')
                return
            if not hasattr(os, 'pidfd_open') or not hasattr(select, 'poll'):
                self._set_status('当前系统不支持安全的 pidfd 身份校验，未附加')
                return
            descriptor = os.pidfd_open(self.key[0], 0)
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            try:
                matching_process(self.key)
            except ValueError as error:
                raise _IdentityError('进程身份不匹配或 PID 已复用') from error
            start_ticks = _start_ticks(self.key[0])
            if poller.poll(0):
                raise ProcessLookupError('进程已退出')
            if self._cancel.is_set():
                return
            tracer = subprocess.Popen(
                [executable, '-f', '-qq', '-ttt', '-T', '-yy', '-s', '0',
                 '-e', 'signal=none', '-e', 'trace=' + _OPERATIONS, '-p', str(self.key[0])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                start_new_session=True, env={**os.environ, 'LC_ALL': 'C'})
            # Keep the pidfd until detachment. Validate again across the spawn boundary.
            if _start_ticks(self.key[0]) != start_ticks:
                raise _IdentityError('进程启动时钟已变化，身份不匹配或 PID 已复用')
            if poller.poll(0):
                raise ProcessLookupError('进程已退出')
            reader = selectors.DefaultSelector()
            os.set_blocking(tracer.stderr.fileno(), False)
            reader.register(tracer.stderr, selectors.EVENT_READ)
            self._set_status('strace 已启动，等待文件读写系统调用', True)
            buffer = b''
            oversized = False
            diagnostic = ''
            while not self._cancel.is_set():
                if poller.poll(0):
                    final = '目标进程已退出，跟踪已停止'
                    break
                if _start_ticks(self.key[0]) != start_ticks:
                    raise _IdentityError('进程启动时钟已变化，身份不匹配或 PID 已复用')
                ready = reader.select(.1)
                ended = False
                if ready:
                    chunk = os.read(tracer.stderr.fileno(), 8192)
                    ended = not chunk
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        if oversized or len(line) > _MAX_LINE:
                            oversized = False
                            with self._lock:
                                self._unparsed += 1
                            continue
                        line = os.fsdecode(line)
                        if line.startswith('strace:'):
                            # Diagnostics are bounded; syscall arguments are never retained.
                            diagnostic = line[:512]
                            continue
                        previous_unparsed = parser.unparsed
                        try:
                            event = parser.feed(line)
                        except (ValueError, OverflowError) as error:
                            with self._lock:
                                self._unparsed += 1
                            raise _TraceParseError(type(error).__name__) from error
                        with self._lock:
                            self._unparsed += parser.unparsed - previous_unparsed
                        if event is not None:
                            # A pidfd binds the target; /proc Tgid binds each followed TID.
                            if not poller.poll(0) and self._belongs(event.tid):
                                self._append(event)
                                if not self._cancel.is_set():
                                    self._set_status('跟踪中', True)
                            else:
                                with self._lock:
                                    self._dropped += 1
                    if len(buffer) > _MAX_LINE:
                        buffer = b''
                        oversized = True
                if ended or (tracer.poll() is not None and not ready):
                    if 'Operation not permitted' in diagnostic or 'Permission denied' in diagnostic:
                        final = '权限不足，strace 无法附加；未提权或修改 ptrace 设置'
                    elif 'No such process' in diagnostic or poller.poll(0):
                        final = '目标进程已退出，跟踪已停止'
                    else:
                        final = 'strace 输出已结束，跟踪已停止'
                        if tracer.poll() is not None:
                            final += '（返回码 ' + str(tracer.returncode) + '）'
                        if diagnostic:
                            final += '：' + diagnostic
                    break
            if buffer or oversized:
                with self._lock:
                    self._unparsed += 1
        except (PermissionError, psutil.AccessDenied):
            final = '权限不足，无法核对或附加目标进程；未提权或修改 ptrace 设置'
        except (ProcessLookupError, psutil.NoSuchProcess):
            final = '目标进程已退出，跟踪已停止'
        except _IdentityError as error:
            final = str(error) + '，跟踪已停止'
        except _TraceParseError as error:
            final = 'strace 记录解析失败（' + str(error) + '），跟踪已停止'
        except ValueError:
            final = '跟踪数据处理失败（ValueError），跟踪已停止'
        except OSError as error:
            final = '跟踪不可用：' + str(error)
        finally:
            cleanup_failed = False
            if tracer is not None:
                # Only the Popen child tracer receives signals. A non-daemon worker
                # lets interpreter shutdown wait for this bounded detach/reap path.
                try:
                    if tracer.poll() is None:
                        tracer.send_signal(signal.SIGINT)
                    try:
                        tracer.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        tracer.kill()
                        tracer.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    cleanup_failed = True
            # A signal/wait failure must not skip any owned descriptor's close.
            for resource in (reader, tracer.stderr if tracer is not None else None):
                if resource is not None:
                    try:
                        resource.close()
                    except OSError:
                        cleanup_failed = True
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    cleanup_failed = True
            if parser.pending:
                with self._lock:
                    self._unparsed += len(parser.pending)
            if cleanup_failed:
                self._set_status('跟踪器或资源清理未确认，请检查 strace')
            # Preserve a concrete startup failure assigned before an early return.
            elif self._active or tracer is not None or descriptor is not None or self._cancel.is_set():
                self._set_status('跟踪器已停止' if self._cancel.is_set() else final)
