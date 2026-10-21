"""Bounded demo: writes only inside a freshly created, marked temporary directory."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import time


MARKER = '.resource-observatory-demo'


def create_demo_directory(base=None):
    root = Path(tempfile.mkdtemp(prefix='resource-observatory-demo-', dir=base))
    (root / MARKER).write_text('Resource Observatory disposable demo data', encoding='utf-8')
    (root / '演示缓存.tmp').write_bytes(b'\0' * (8 * 1024 * 1024))
    (root / '示例资料.txt').write_text('这是自建的演示资料。文件大小不能用于自动判断是否应该删除。\n', encoding='utf-8')
    (root / '运行日志.log').write_bytes(b'demo started\n')
    return root


def run_workload(root, duration=120):
    root = Path(root)
    if root.is_symlink() or not (root / MARKER).is_file():
        raise ValueError('演示仅可在带有自建标记的临时目录中运行')
    chunks = []
    started = time.monotonic()
    while time.monotonic() - started < duration:
        if len(chunks) < 8:
            # Touch actual pages; reserving virtual address space alone is not an RSS demo.
            chunks.append(bytearray(b'R' * (4 * 1024 * 1024)))
        with (root / '运行日志.log').open('ab') as stream:
            stream.write(b'demo data ' * 26000)
        time.sleep(2)


def launch_demo(root, duration=120):
    # The Windows venv executable is a forwarding launcher. Its RSS belongs to
    # the proxy, not the workload; use the base interpreter for this stdlib-only demo.
    executable = getattr(sys, '_base_executable', sys.executable)
    return subprocess.Popen(
        [executable, str(Path(__file__).resolve()), '--directory', str(root), '--duration', str(duration)],
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--duration', type=float, default=120)
    args = parser.parse_args()
    run_workload(args.directory, args.duration)
