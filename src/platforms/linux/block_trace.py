"""Process-tree, ext4 file and whole-device block requests for MQSim replay."""
from bisect import bisect_left
from collections import Counter
from glob import glob
import os
from pathlib import Path
import re
import signal
from stat import S_ISREG
import subprocess
import tempfile
import threading

import psutil

from ...core.processes import matching_process


_EVENT = re.compile(
    r'^\s*(\d+)/(\d+)\s+(\d+)\.(\d{1,9}):\s+block:block_rq_issue:\s+'
    r'(\d+),(\d+)\s+(\S+)\s+\d+\s+\([^)]*\)\s+(\d+)\s+\+\s+(\d+)\b',
    re.MULTILINE,
)
_DURATION = 30
_MAX_BYTES = 8 * 1024 * 1024
_EXTENT = re.compile(r'^\s*\d+:\s*\d+\s*\.\.\s*\d+:\s*(\d+)\s*\.\.\s*(\d+):', re.MULTILINE)


def parse_trace(source, pid=None):
    rows = []
    matches = list(_EVENT.finditer(source))
    if len(matches) != source.count(': block:block_rq_issue:'):
        raise ValueError('无法识别此内核的块请求格式')
    for match in matches:
        tgid, tid, seconds, fraction, major, minor, flags, sector, count = match.groups()
        kind = flags.lstrip('F')[:1]
        if (pid is None or int(tgid) == pid) and kind in ('R', 'W') and int(count):
            rows.append((int(seconds) * 10**9 + int(fraction.ljust(9, '0')),
                         int(tid), f'{major}:{minor}', int(sector), int(count), kind))
    return sorted(rows, key=lambda row: row[0])


def file_layout(path):
    """Return the disk ID and 512-byte sector ranges of an ext4 file."""
    source = Path(path)
    info = source.stat()
    if not S_ISREG(info.st_mode):
        raise ValueError('请选择普通文件')
    device = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
    fstype = subprocess.check_output(['/usr/bin/findmnt', '-n', '-o', 'FSTYPE', '-T', str(source)],
                                     text=True, timeout=5).strip()
    disk = Path('/sys/dev/block', device).resolve()
    if fstype != 'ext4' or not disk.is_dir():
        raise ValueError('按文件采集需要 Linux ext4 文件')
    start = 0
    if (disk / 'partition').is_file():
        start = int((disk / 'start').read_text())
        disk = disk.parent
    if str(disk).startswith('/sys/devices/virtual/'):
        raise ValueError('按文件采集需要直连块设备上的 ext4 文件')
    major, minor = map(int, (disk / 'dev').read_text().split(':'))
    mapping = subprocess.check_output(['/usr/sbin/filefrag', '-v', '-e', '-b512', str(source)],
                                      text=True, env={**os.environ, 'LC_ALL': 'C'}, timeout=10)
    extents = tuple(sorted((start + int(first), start + int(last) + 1)
                           for first, last in _EXTENT.findall(mapping)))
    extent_lines = (line for line in mapping.splitlines() if _EXTENT.match(line))
    if not extents or any(re.search(r'\b(delalloc|unknown_loc|unwritten|inline|shared|encoded)\b', line)
                          for line in extent_lines):
        raise ValueError('该文件没有可确认的磁盘扇区')
    return (info.st_dev, info.st_ino, info.st_size), (major, minor), extents


def file_requests(rows, device, extents):
    starts = [first for first, _ in extents]
    selected = []
    for row in rows:
        if row[2] != device:
            continue
        sector, count = row[3:5]
        index = bisect_left(starts, sector + count) - 1
        if index >= 0 and extents[index][1] > sector:
            selected.append(row)
    return selected


def mqsim_text(rows, device):
    selected = [row for row in rows if row[2] == device]
    if not selected:
        raise ValueError('该设备没有可导出的读写请求')
    origin = selected[0][0]
    return ''.join(f'{stamp-origin} 0 {sector} {count} {int(kind == "R")}\n'
                   for stamp, _, _, sector, count, kind in selected)


def _perf():
    if 'microsoft' in os.uname().release.lower():
        # Ubuntu's /usr/bin/perf wrapper looks for the running Microsoft kernel.
        # The installed Linux perf binary works with the WSL tracepoint directly.
        installed = glob('/usr/lib/linux-tools-*/perf') + glob('/usr/lib/linux-tools/*/perf')
        if installed:
            return max(installed, key=lambda path: tuple(map(int, re.findall(r'\d+', path))))
    if Path('/usr/bin/perf').is_file():
        return '/usr/bin/perf'
    raise ValueError('未找到 perf。Ubuntu 可安装 linux-tools-generic。')


class BlockTrace:
    def __init__(self, key, path=None, whole_device=False):
        self.key = key
        self.path = path
        self.whole_device = whole_device
        self.dropped = None
        self.status = '正在启动采集…'
        self.rows = []
        self.reads = 0
        self.devices = {}
        self.partial = False
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name='block-io-trace')

    def start(self):
        self._worker.start()

    def close(self):
        self._stop.set()

    @property
    def active(self):
        return self._worker.is_alive()

    def snapshot(self):
        active = self.active
        rows = self.rows
        origin = rows[0][0] if rows else 0
        return {'active': active, 'stopping': active and self._stop.is_set(),
                'status': self.status, 'count': len(rows), 'partial': self.partial, 'path': self.path,
                'whole_device': self.whole_device, 'dropped': self.dropped, 'reads': self.reads, 'writes': len(rows) - self.reads,
                'devices': self.devices,
                'events': [(stamp - origin, tid, device, str(sector), count, kind)
                           for stamp, tid, device, sector, count, kind in rows[-100:]]}

    def _run(self):
        try:
            if os.geteuid() != 0:
                self.status = '等待 sudo 授权，请查看启动 CleanDist 的终端'
                with subprocess.Popen(['/usr/bin/sudo', '-v']) as authorization:
                    while authorization.poll() is None and not self._stop.wait(.1):
                        pass
                    if self._stop.is_set():
                        if authorization.poll() is None:
                            authorization.terminate()
                        self.status = '已取消采集'
                        return
                    if authorization.returncode:
                        raise ValueError('sudo 授权未完成，请从 Ubuntu 终端启动 CleanDist 后重试。')
            if self.path:
                self._device_capture()
                return
            perf = _perf()
            target = matching_process(self.key)
            pids = [target.pid, *(child.pid for child in target.children(recursive=True))]
            scope = ['-p', ','.join(map(str, pids))]
            command = [perf, 'record', '-q', '-B', '--max-size', f'{_MAX_BYTES // 1024**2}M',
                       '-e', 'block:block_rq_issue', '-c', '1', *scope, '-o', '-',
                       '--', '/bin/sh', '-c', 'echo CLEANDIST_PERF_READY >&2; exec "$@"',
                       'perf-workload', '/usr/bin/timeout', str(_DURATION),
                       '/usr/bin/dd', 'of=/dev/null', 'bs=1', 'count=1', 'status=none']
            if os.geteuid() != 0:
                command[:0] = ['/usr/bin/sudo', '-n', '--']
            with tempfile.TemporaryFile() as data, tempfile.TemporaryFile() as errors:
                # Only perf is elevated; its binary output goes to an already open file.
                # Closing stdin ends perf's waiting workload and flushes recorded samples.
                with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=data, stderr=errors,
                                      env={**os.environ, 'LC_ALL': 'C'}) as child:
                    self.status = '正在启动 perf 采集…'
                    while child.poll() is None and not self._stop.wait(.02):
                        if b'CLEANDIST_PERF_READY' in os.pread(errors.fileno(), 8192, 0):
                            self.status = f'perf 正在采集，最多 {_DURATION} 秒'
                    child.stdin.close()
                    result = child.wait()
                # GNU timeout returns 124 at the capture limit; perf propagates it.
                if result not in (0, 124, -signal.SIGTERM, 128 + signal.SIGTERM):
                    errors.seek(0)
                    message = errors.read(2048).decode('utf-8', 'replace').strip()
                    if message.startswith('sudo:'):
                        raise ValueError('sudo 未授权运行 perf，请重新开始采集。')
                    if 'not found for kernel' in message:
                        raise ValueError('未找到可用的 perf。Ubuntu 可安装 linux-tools-generic。')
                    raise ValueError(message or f'perf 采集失败（{result}）')
                data_size = data.seek(0, os.SEEK_END)
                data.seek(0)
                decoded = subprocess.run(
                    [perf, 'script', '--ns', '--show-lost-events', '-F', 'trace:pid,tid,time,event,trace', '-i', '-'],
                    stdin=data, capture_output=True, text=True, errors='replace', timeout=15,
                    env={**os.environ, 'LC_ALL': 'C'},
                )
                if decoded.returncode:
                    raise ValueError(decoded.stderr.strip() or 'perf 记录无法解码')
                rows = parse_trace(decoded.stdout)
                self.rows = rows
                errors.seek(0)
                capture_log = errors.read().decode(errors='replace')
                self.partial = data_size >= _MAX_BYTES or bool(re.search(
                    r'PERF_RECORD_LOST|\blost\s+[1-9]\d*|\b[1-9]\d*\s+(?:events|chunks) lost',
                    decoded.stdout + decoded.stderr + capture_log, re.IGNORECASE))
                self.status = '采集完成' if rows else '本次未观测到磁盘读写'
                if self.partial:
                    self.status = '记录达到大小上限或内核报告事件丢失，无法导出'
        except (OSError, ValueError, psutil.Error, subprocess.SubprocessError) as error:
            self.status = '采集失败：' + str(error)[:500]
        finally:
            self.reads = sum(row[5] == 'R' for row in self.rows)
            self.devices = dict(Counter(row[2] for row in self.rows))

    def _device_capture(self):
        layout = None if self.whole_device else file_layout(self.path)
        info = os.stat(self.path)
        disk = Path('/sys/dev/block', f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}').resolve()
        if not disk.is_dir():
            raise ValueError('所选文件的挂载点没有可追踪的本地块设备')
        if (disk / 'partition').exists():
            disk = disk.parent
        node = Path('/dev') / disk.name
        if not node.is_block_device():
            raise ValueError(f'当前命名空间无法访问块设备 {node}')
        cmd = ['/bin/bash', str(Path(__file__).with_name('device_capture.sh')), str(node), str(_DURATION)]
        if os.geteuid() != 0:
            cmd[:0] = ['/usr/bin/sudo', '-n', '--']
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=output, stderr=errors) as child:
                self.status = f'正在启动 {node} 的 blktrace 采集…'
                while child.poll() is None and not self._stop.wait(.02):
                    if os.pread(output.fileno(), 6, 0) == b'READY\n':
                        self.status = f'blktrace 正在采集 {node}，最多 {_DURATION} 秒'
                if child.poll() is None:
                    child.communicate(input=b'\n', timeout=_DURATION + 15)
                errors.seek(0)
                log = errors.read().decode(errors='replace')
                if child.returncode:
                    raise ValueError(log[-700:].strip() or 'blktrace 或 blkparse 执行失败')
            output.seek(0)
            rows = []
            skips = None
            for raw in output:
                line = raw.decode(errors='replace')
                if line.startswith('ROW '):
                    _, stamp, tid, dev, action, flags, sector, count = line.split()
                    kind = flags.lstrip('F')[:1]
                    if action == 'D' and kind in ('R', 'W') and int(count):
                        rows.append((int(stamp.replace('.', '')), int(tid), dev.replace(',', ':'),
                                     int(sector), int(count), kind))
                elif line.startswith('Skips:'):
                    skips = re.search(r'^Skips:\s+(\d+) forward \((\d+) -', line)
        total = re.search(r'Total:\s+\d+ events \(dropped (\d+)\)', log)
        self.dropped = int(total[1]) if total else None
        size_limit = 'CAPTURE_SIZE_LIMIT' in log
        self.partial = (size_limit or self.dropped != 0 or skips is None
                        or int(skips[1]) > 0 or int(skips[2]) > 0)
        if layout:
            if file_layout(self.path) != layout:
                raise ValueError('采集期间文件或扇区映射发生变化，请重新采集')
            major, minor = layout[1]
            rows = file_requests(rows, f'{major}:{minor}', layout[2])
        self.rows = sorted(rows, key=lambda row: row[0])
        self.status = '采集完成' if rows else '本次未观测到磁盘读写'
        if self.partial:
            self.status = ('原始采集达到 128 MiB 容量限制，无法导出' if size_limit
                           else '采集不完整或缺少丢失统计，无法导出')
