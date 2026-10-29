"""用稳定的内核启动计数识别进程；墙上时钟仅用于展示。"""
from functools import lru_cache
import os
from pathlib import Path

import psutil


@lru_cache(maxsize=1)
def _clock():
    # 保持现有二元组的时间戳形态，但固定原点，避免WSL/NTP校时导致假PID复用。
    return psutil.boot_time(), os.sysconf('SC_CLK_TCK')


def process_created(process):
    path = Path(psutil.PROCFS_PATH) / str(process.pid) / 'stat'
    try:
        with path.open('rb') as stream:
            data = stream.read(16385)
    except PermissionError as error:
        raise psutil.AccessDenied(process.pid) from error
    except FileNotFoundError as error:
        raise psutil.NoSuchProcess(process.pid) from error
    if len(data) > 16384:
        raise ValueError('进程身份数据超过读取上限')
    fields = data.rsplit(b')', 1)[-1].split()
    if len(fields) < 20:
        raise ValueError('无法确认进程启动计数')
    ticks = int(fields[19])
    if ticks < 0:
        raise ValueError('无效的进程启动计数')
    epoch, frequency = _clock()
    return epoch + ticks / frequency
