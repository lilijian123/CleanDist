"""读取有界 /proc 内存摘要和映射排行；不把读取失败解释成零。"""
from collections import defaultdict
from pathlib import Path
import re

import psutil

from ...core.process_analysis import MemoryMapping


_HEADER = re.compile(r'^[0-9a-f]+-[0-9a-f]+\s')
_VALUE = re.compile(r'^([A-Za-z_]+):\s+(\d+)\s+kB\s*$')


def thread_identity(process, tid):
    path = Path(psutil.PROCFS_PATH) / str(process.pid) / 'task' / str(tid) / 'stat'
    try:
        with path.open('rb') as stream:
            content = stream.read(8193)
    except FileNotFoundError:
        return None
    if len(content) > 8192 or not content.startswith(f'{tid} ('.encode()):
        return None
    # comm 可包含空格和右括号；最后一个右括号之后从字段3(state)开始。
    fields = content[content.rfind(b')') + 1:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def _read_values(path):
    with path.open('rb') as stream:
        content = stream.read(128 * 1024 + 1)
    if len(content) > 128 * 1024:
        raise OSError('内存摘要超过读取上限')
    return {match[1]: int(match[2]) * 1024 for line in content.decode('ascii', errors='replace').splitlines()
            if (match := _VALUE.match(line))}


def _read_mappings(path):
    groups = defaultdict(lambda: defaultdict(int))
    current = None
    consumed = 0
    truncated = False
    with path.open('rb') as stream:
        while True:
            line = stream.readline(16385)
            if not line:
                break
            consumed += len(line)
            if consumed > 16 * 1024 * 1024 or len(line) > 16384:
                truncated = True
                break
            text = line.decode('utf-8', errors='replace').rstrip()
            if _HEADER.match(text):
                fields = text.split(None, 5)
                current = fields[5] if len(fields) > 5 else '[anonymous]'
                if len(groups) >= 4096 and current not in groups:
                    truncated = True
                    break
                groups[current]  # 保留合法的零驻留映射。
            elif current is not None and (match := _VALUE.match(text)):
                if match[1] in ('Rss', 'Pss', 'Private_Clean', 'Private_Dirty', 'Private_Hugetlb', 'Anonymous'):
                    groups[current][match[1]] += int(match[2]) * 1024
    rows = [MemoryMapping(name, values.get('Rss'), values.get('Pss'),
                          (values.get('Private_Clean', 0) + values.get('Private_Dirty', 0) +
                           values.get('Private_Hugetlb', 0)) if 'Private_Clean' in values else None,
                          values.get('Anonymous')) for name, values in groups.items()]
    return tuple(sorted(rows, key=lambda row: row.rss or 0, reverse=True)[:50]), truncated


def sample_memory_breakdown(process):
    result = dict.fromkeys(('rss', 'uss', 'pss', 'anonymous', 'file_pss',
                            'shared_pss', 'swap', 'private_commit'))
    root = Path(psutil.PROCFS_PATH) / str(process.pid)
    errors = []
    try:
        values = _read_values(root / 'smaps_rollup')
        result.update(rss=values.get('Rss'), pss=values.get('Pss'),
                      anonymous=values.get('Anonymous'), file_pss=values.get('Pss_File'),
                      shared_pss=values.get('Pss_Shmem'), swap=values.get('Swap'))
        if 'Private_Clean' in values and 'Private_Dirty' in values:
            result['uss'] = values['Private_Clean'] + values['Private_Dirty'] + values.get('Private_Hugetlb', 0)
    except OSError as error:
        errors.append(f'内存摘要（smaps_rollup）：{type(error).__name__}')
    try:
        mappings, truncated = _read_mappings(root / 'smaps')
        if truncated:
            errors.append('映射读取达到16MiB/4096路径上限，排行只包含已读取部分')
    except OSError as error:
        errors.append(f'内存映射（smaps）：{type(error).__name__}')
        mappings = ()
    result.update(mappings=mappings, errors=tuple(errors))
    return result
