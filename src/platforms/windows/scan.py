"""构建并缓存轻量 Windows 枚举程序，将元数据流转换为紧凑记录。"""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import time


@lru_cache(maxsize=1)
def get_scan_helper():
    source = Path(__file__).with_name('scan.cs')
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    compiler = next((windows / 'Microsoft.NET' / kind / 'v4.0.30319' / 'csc.exe'
                     for kind in ('Framework64', 'Framework')
                     if (windows / 'Microsoft.NET' / kind / 'v4.0.30319' / 'csc.exe').is_file()), None)
    if compiler is None:
        return None
    cache_dir = Path(tempfile.gettempdir()) / 'resource-cleaner-native' / hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    executable = cache_dir / 'scan.exe'
    if not executable.is_file():
        temp_executable = cache_dir / f'building-{os.getpid()}.exe'
        result = subprocess.run([str(compiler), '/nologo', '/optimize+', '/target:exe',
                                 f'/out:{temp_executable}', str(source)], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if result.returncode:
            return None
        try:
            os.replace(temp_executable, executable)
        except OSError:
            if not executable.is_file():
                raise
            temp_executable.unlink(missing_ok=True)
    return executable


def scan_native(root, cancel, limit, progress):
    from ...core.files import DirectorySnapshot, FileRecord, ScanProgress, free_disk_bytes
    start = time.monotonic()
    files, directories, errors = {}, [], []
    skipped = 0
    truncated = False
    if progress:
        progress(ScanProgress('准备原生扫描', 0, 0, 0, 'windows-native'))
    if cancel.is_set():
        return DirectorySnapshot(root, {}, False, 0, (), True, False, time.time(), 0, None,
                                 (), 'windows-native')
    executable = get_scan_helper()
    if executable is None:
        return None
    process = subprocess.Popen([str(executable), str(root)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                               bufsize=1024 * 1024)
    finished = threading.Event()

    def stop_on_cancel():
        while not finished.wait(.05):
            if cancel.is_set():
                try:
                    process.terminate()
                except OSError:
                    pass
                return

    watcher = threading.Thread(target=stop_on_cancel, daemon=True)
    watcher.start()
    stream = process.stdout
    last_report = 0

    def read_exact(count):
        data = stream.read(count)
        if len(data) != count:
            raise EOFError('原生扫描数据未完整返回')
        return data

    def read_text():
        length, = struct.unpack('<I', read_exact(4))
        if length > 1024 * 1024:
            raise ValueError('原生扫描返回了无效的路径长度')
        return read_exact(length).decode('utf-8')

    try:
        if read_exact(4) != b'RSC1':
            raise ValueError('原生扫描数据版本不匹配')
        while not cancel.is_set():
            kind = stream.read(1)
            if not kind:
                break
            if kind == b'F':
                relative = read_text()
                size, modified, created = struct.unpack('<QQQ', read_exact(24))
                if limit is not None and len(files) >= limit:
                    truncated = True
                    break
                files[relative] = FileRecord(root, relative, size, (modified - 116444736000000000) * 100,
                                             0, 0, (created - 116444736000000000) * 100)
            elif kind == b'D':
                relative = read_text()
                if relative:
                    directories.append(relative)
            elif kind == b'K':
                skipped += 1
            elif kind == b'E':
                relative = read_text()
                code, = struct.unpack('<i', read_exact(4))
                if not relative:
                    raise ValueError(f'无法读取所选目录：Windows错误 {code}')
                skipped += 1
                if len(errors) < 5:
                    errors.append(f'{relative}：Windows错误 {code}')
            else:
                raise ValueError('原生扫描返回了未知记录')
            now = time.monotonic()
            if progress and now - last_report >= .25:
                progress(ScanProgress('扫描目录', len(files), len(directories), now - start, 'windows-native'))
                last_report = now
        if not (cancel.is_set() or truncated) and process.wait() != 0:
            raise ValueError('原生扫描进程异常退出，未使用部分结果作为完整扫描')
    except EOFError:
        if not cancel.is_set():
            raise
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait()
        finished.set()
        watcher.join()
        stream.close()
    elapsed = time.monotonic() - start
    if progress:
        progress(ScanProgress('扫描完成', len(files), len(directories), elapsed, 'windows-native'))
    cancelled = cancel.is_set()
    return DirectorySnapshot(root, files, not (skipped or truncated or cancelled), skipped,
                             tuple(errors), cancelled, truncated, time.time(), elapsed,
                             free_disk_bytes(root), tuple(directories), 'windows-native')
