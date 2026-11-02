"""Optional real ETW session, decoded by Windows tracerpt (metadata only).

The provider receives a PID filter before collection starts. Only our randomly
named session is stopped. No administrator prompt or global kernel logger use.
"""
from collections import deque
import ctypes
from ctypes import wintypes as w
from datetime import datetime
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from ...core.process_trace import TraceEvent, TraceState
from .processes import guard_process, _wait


_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_PROVIDER = 'edd08927-9cc4-4e65-b970-c2560fb5c289'
_NOTE = '仅目标 PID 的 ETW 文件元数据，不读取内容；路径、完成状态或字节无法关联时显示未知；不代表物理落盘'
_NS = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}


class _Guid(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        result = cls()
        result.bytes[:] = uuid.UUID(value).bytes_le
        return result


class _Wnode(ctypes.Structure):
    _fields_ = [('BufferSize', w.ULONG), ('ProviderId', w.ULONG), ('HistoricalContext', ctypes.c_ulonglong),
                ('TimeStamp', ctypes.c_longlong), ('Guid', _Guid), ('ClientContext', w.ULONG), ('Flags', w.ULONG)]


class _Properties(ctypes.Structure):
    _fields_ = [('Wnode', _Wnode), ('BufferSize', w.ULONG), ('MinimumBuffers', w.ULONG),
                ('MaximumBuffers', w.ULONG), ('MaximumFileSize', w.ULONG), ('LogFileMode', w.ULONG),
                ('FlushTimer', w.ULONG), ('EnableFlags', w.ULONG), ('AgeLimit', w.LONG),
                ('NumberOfBuffers', w.ULONG), ('FreeBuffers', w.ULONG), ('EventsLost', w.ULONG),
                ('BuffersWritten', w.ULONG), ('LogBuffersLost', w.ULONG), ('RealTimeBuffersLost', w.ULONG),
                ('LoggerThreadId', w.HANDLE), ('LogFileNameOffset', w.ULONG), ('LoggerNameOffset', w.ULONG)]


class _Filter(ctypes.Structure):
    _fields_ = [('Ptr', ctypes.c_ulonglong), ('Size', w.ULONG), ('Type', w.ULONG)]


class _Enable(ctypes.Structure):
    _fields_ = [('Version', w.ULONG), ('EnableProperty', w.ULONG), ('ControlFlags', w.ULONG),
                ('SourceId', _Guid), ('EnableFilterDesc', ctypes.POINTER(_Filter)), ('FilterDescCount', w.ULONG)]


_start = _advapi.StartTraceW
_start.argtypes, _start.restype = [ctypes.POINTER(ctypes.c_ulonglong), w.LPCWSTR, ctypes.POINTER(_Properties)], w.ULONG
_enable = _advapi.EnableTraceEx2
_enable.argtypes = [ctypes.c_ulonglong, ctypes.POINTER(_Guid), w.ULONG, ctypes.c_ubyte,
                    ctypes.c_ulonglong, ctypes.c_ulonglong, w.ULONG, ctypes.POINTER(_Enable)]
_enable.restype = w.ULONG
_control = _advapi.ControlTraceW
_control.argtypes, _control.restype = [ctypes.c_ulonglong, w.LPCWSTR, ctypes.POINTER(_Properties), w.ULONG], w.ULONG
_dos_device = ctypes.WinDLL('kernel32', use_last_error=True).QueryDosDeviceW
_dos_device.argtypes, _dos_device.restype = [w.LPCWSTR, w.LPWSTR, w.DWORD], w.DWORD


def _integer(value):
    if value is None: return None
    return int(value, 16 if value.lower().startswith('0x') else 10)


class FileTrace:
    def __init__(self, key):
        self.key = key
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._events = deque(maxlen=1000)
        self._dropped = self._unparsed = 0
        self._active = False
        self._status = '尚未开启可选 Windows ETW；' + _NOTE
        self._paths, self._pending = {}, {}
        self._devices = []

    def start(self):
        with self._lock:
            if self._thread is not None: return
            self._active = True
            self._status = '正在启动独立 ETW 会话；' + _NOTE
            self._thread = threading.Thread(target=self._run, daemon=False, name='windows-file-etw')
            self._thread.start()

    def close(self):
        self._cancel.set()

    def snapshot(self):
        with self._lock:
            return TraceState(self.key, self._active and not self._cancel.is_set(), self._status,
                              tuple(self._events), self._dropped, self._unparsed,
                              self._thread is None or not self._thread.is_alive())

    def _append(self, event):
        with self._lock:
            if len(self._events) == self._events.maxlen: self._dropped += 1
            self._events.append(event)

    def _event(self, element):
        # tracerpt emits the normal Windows event XML schema. Never retain raw XML.
        system = element.find('e:System', _NS)
        if system is None: return
        provider = system.find('e:Provider', _NS)
        if provider is None or provider.get('Guid', '').strip('{}').lower() != _PROVIDER: return
        execution = system.find('e:Execution', _NS)
        if execution is None or _integer(execution.get('ProcessID')) != self.key[0]: return
        values = {item.get('Name'): item.text for item in element.findall('e:EventData/e:Data', _NS)}
        event_id = _integer(system.findtext('e:EventID', namespaces=_NS))
        timestamp = datetime.fromisoformat(system.find('e:TimeCreated', _NS).get('SystemTime').replace('Z', '+00:00')).timestamp()
        tid = _integer(values.get('IssuingThreadId') or values.get('ThreadId') or execution.get('ThreadID')) or 0
        obj = values.get('FileObject') or values.get('FileKey')
        path = values.get('FilePath') or values.get('FileName') or ''
        if path.startswith('\\??\\'): path = path[4:]
        for prefix, drive in self._devices:
            if path.casefold().startswith(prefix.casefold() + '\\'):
                path = drive + path[len(prefix):]
                break
        if obj and path:
            if len(self._paths) >= 4096: self._paths.clear()
            self._paths[obj] = path
        path = path or self._paths.get(obj, '')
        irp = values.get('Irp')
        if event_id == 24 and irp in self._pending:
            saved = self._pending.pop(irp)
            status = _integer(values.get('Status'))
            success = status < 0x80000000 if status is not None else None
            # ExtraInfo is the completed operation's information field; request IoSize
            # alone cannot prove how many bytes completed.
            completed = _integer(values.get('ExtraInformation')) if saved[2] in ('read', 'write') else None
            self._append(TraceEvent(saved[0], saved[1], saved[2], saved[3],
                                    completed if success is True else None, timestamp - saved[0], status, success))
        elif event_id in (15, 16, 26):
            operation = {15: 'read', 16: 'write', 26: 'unlink'}[event_id]
            size = _integer(values.get('IOSize')) if event_id in (15, 16) else None
            if irp:
                if len(self._pending) >= 1024:
                    old = self._pending.pop(next(iter(self._pending)))
                    self._append(TraceEvent(old[0], old[1], old[2], old[3], None))
                self._pending[irp] = timestamp, tid, operation, path, size
            else:
                status = _integer(values.get('Status'))
                success = status < 0x80000000 if status is not None else None
                self._append(TraceEvent(timestamp, tid, operation, path, None,
                                        return_value=status, success=success))
        if event_id in (11, 14) and obj: self._paths.pop(obj, None)

    def _run(self):
        name = 'CleanDist-' + uuid.uuid4().hex
        buffer = ctypes.create_string_buffer(ctypes.sizeof(_Properties) + (len(name) + 1) * 2)
        properties = ctypes.cast(buffer, ctypes.POINTER(_Properties))
        properties.contents.Wnode.BufferSize = len(buffer)
        properties.contents.Wnode.Flags = 0x20000  # WNODE_FLAG_TRACED_GUID
        properties.contents.Wnode.ClientContext = 2  # system clock
        properties.contents.LogFileMode = 0x100  # REAL_TIME, no ETL file
        properties.contents.BufferSize = 64
        properties.contents.MinimumBuffers = 2
        properties.contents.MaximumBuffers = 8
        properties.contents.FlushTimer = 1
        properties.contents.LoggerNameOffset = ctypes.sizeof(_Properties)
        session, started, consumer = ctypes.c_ulonglong(), False, None
        temporary = None
        final = 'ETW 已停止'
        try:
            with guard_process(self.key) as target:
                temporary = tempfile.TemporaryDirectory(prefix='cleandist-etw-')
                folder = temporary.name
                for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                    drive, destination = letter + ':', ctypes.create_unicode_buffer(32768)
                    if _dos_device(drive, destination, len(destination)):
                        self._devices.append((destination.value, drive))
                status = _start(ctypes.byref(session), name, properties)
                if status: raise ctypes.WinError(status)
                started = True
                # EVENT_FILTER_TYPE_PID. Kernel provider filtering prevents collecting foreign process paths.
                pids = (w.ULONG * 1)(self.key[0])
                descriptor = _Filter(ctypes.addressof(pids), ctypes.sizeof(pids), 0x80000004)
                options = _Enable(2, 0, 0, _Guid(), ctypes.pointer(descriptor), 1)
                provider = _Guid.parse(_PROVIDER)
                status = _enable(session, ctypes.byref(provider), 1, 5, 0x7f0, 0, 0, ctypes.byref(options))
                if status: raise ctypes.WinError(status)
                path = Path(folder) / 'events.xml'
                consumer = subprocess.Popen([str(Path(os.environ['SystemRoot']) / 'System32' / 'tracerpt.exe'),
                    '-rt', name, '-o', str(path), '-of', 'XML', '-y'], cwd=folder,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                parser, position = ET.XMLPullParser(events=('end',)), 0
                with self._lock: self._status = 'ETW 已启动，等待目标文件事件；' + _NOTE
                while not self._cancel.wait(.1):
                    if _wait(target, 0) != 258:
                        final = '目标已退出，ETW 已停止'
                        break
                    if consumer.poll() is not None:
                        final = 'ETW 解码器结束（返回码 ' + str(consumer.returncode) + '），未确认持续采集'
                        break
                    if not path.exists(): continue
                    if path.stat().st_size > 16 * 1024**2:
                        final = 'ETW 临时元数据达到 16 MB，本次跟踪已停止；可重新开启'
                        break
                    try:
                        with path.open('rb') as stream:
                            stream.seek(position)
                            chunk = stream.read(65536)
                            position = stream.tell()
                    except PermissionError: continue
                    if chunk:
                        parser.feed(chunk)
                        for _, element in parser.read_events():
                            if element.tag.rsplit('}', 1)[-1] == 'Event':
                                try: self._event(element)
                                except (ValueError, AttributeError, OverflowError):
                                    with self._lock: self._unparsed += 1
                                element.clear()
                status = _control(session, name, properties, 1)
                started = status != 0
                if status: final = '本应用 ETW 会话停止未确认（错误 ' + str(status) + '）'
                with self._lock: self._dropped += properties.contents.EventsLost + properties.contents.RealTimeBuffersLost
                try: consumer.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    consumer.terminate()
                    consumer.wait(timeout=3)
                if path.exists():
                    with path.open('rb') as stream:
                        stream.seek(position)
                        parser.feed(stream.read(16 * 1024**2))
                    for _, element in parser.read_events():
                        if element.tag.rsplit('}', 1)[-1] == 'Event':
                            try: self._event(element)
                            except (ValueError, AttributeError, OverflowError):
                                with self._lock: self._unparsed += 1
                            element.clear()
                # Pending operations without an OperationEnd remain explicitly unknown.
                for saved in self._pending.values(): self._append(TraceEvent(saved[0], saved[1], saved[2], saved[3], None))
        except OSError as error:
            final = ('ETW 不可用：权限不足，未提权；需有该提供程序跟踪权限后手动重试' if getattr(error, 'winerror', None) == 5
                     else 'ETW 不可用：' + str(error))
        except (ValueError, ET.ParseError) as error:
            final = 'ETW 身份或元数据解析不可用：' + str(error)
        finally:
            if started: _control(session, name, properties, 1)
            if consumer is not None and consumer.poll() is None:
                consumer.terminate()
                consumer.wait(timeout=3)
            if temporary is not None: temporary.cleanup()
            with self._lock:
                self._active = False
                self._status = final + '；' + _NOTE
