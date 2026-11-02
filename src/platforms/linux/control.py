"""cgroup v2 controls in an explicitly delegated subtree only."""
import os
from pathlib import Path
import re
import select
import uuid

from ...core.processes import matching_process


class Controller:
    def __init__(self, root=None, mount=Path('/sys/fs/cgroup')):
        configured = root if root is not None else os.environ.get('CLEANDIST_CGROUP_ROOT')
        self.root = Path(configured).resolve() if configured else None
        self.mount = Path(mount).resolve()
        self.groups = {}

    def capabilities(self):
        reason, controllers = '', set()
        if self.root is None:
            reason = '未配置 CLEANDIST_CGROUP_ROOT。需由 systemd Delegate=yes 或管理员委派 cgroup v2 子树，并在上级启用 cpu、memory、io 控制器；本应用不提权。'
        elif self.root == self.mount or self.mount not in self.root.parents:
            reason = '只接受 cgroup v2 挂载内的专用委派子树，不能使用挂载根目录'
        elif not (self.mount / 'cgroup.controllers').exists():
            reason = '当前环境未挂载 cgroup v2'
        elif not os.access(self.root, os.W_OK) or not os.access(self.root / 'cgroup.procs', os.W_OK):
            reason = '委派子树不可写，请核对目录和 cgroup.procs 权限；未尝试提权'
        else:
            try: controllers = set((self.root / 'cgroup.subtree_control').read_text().split())
            except OSError as error: reason = str(error)
        return {'platform': 'Linux', 'cpu': 'cpu' in controllers, 'memory': 'memory' in controllers,
                'memory_soft': 'memory' in controllers, 'io': 'io' in controllers,
                'reason': reason or ('上级未委派或未启用控制器：' + '、'.join(sorted({'cpu', 'memory', 'io'} - controllers)) if controllers != {'cpu', 'memory', 'io'} else ''),
                'root': str(self.root) if self.root else '',
                'scope': '所选进程和迁移后创建且未自行移出组的子进程；已有子进程不迁移',
                'memory_label': 'cgroup memory.current（整组已计费内存，含页缓存，不等同 RSS；迁移前的页仍计费于原组）',
                'io_label': 'io.max 指定块设备实际存储 I/O；缓存写入可能不立即落盘',
                'clear_note': '清除本应用限制；进程可留在无限制的受管组，组内其他上级限制仍有效'}

    def apply(self, key, *, cpu_cores, memory_bytes, hard_memory, device, read_bps, write_bps):
        capabilities = self.capabilities()
        required = [('cpu', cpu_cores is not None), ('memory', memory_bytes is not None),
                    ('io', read_bps is not None or write_bps is not None)]
        for controller, requested in required:
            if requested and not capabilities[controller]:
                raise ValueError(f'{controller} 控制器不可用：' + capabilities['reason'])
        if cpu_cores is not None and cpu_cores < .01:
            raise ValueError('当前 CPU 周期下最小限制为 0.01 核心当量')
        if read_bps is not None or write_bps is not None:
            if not device or not re.fullmatch(r'\d+:\d+', device):
                raise ValueError('I/O 限制需选择 major:minor 形式的真实块设备')
            if not (Path('/sys/dev/block') / device).exists():
                raise ValueError('块设备不存在；WSL、网络盘或不支持的挂载不能宣称已限速')
        # A pidfd prevents treating an exited process as a still-live identity.
        if not hasattr(os, 'pidfd_open'): raise ValueError('需要 pidfd 身份校验支持，未附加')
        descriptor = os.pidfd_open(key[0], 0)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            matching_process(key)
            if poller.poll(0): raise ValueError('目标进程已退出')
            if key not in self.groups:
                membership = Path(f'/proc/{key[0]}/cgroup').read_text().splitlines()
                relative = next((line[3:] for line in membership if line.startswith('0::')), None)
                source = (self.mount / relative.lstrip('/')).resolve() if relative is not None else None
                if source is None or (source != self.root and self.root not in source.parents):
                    raise ValueError('目标不在委派子树内；请在该委派环境中启动目标后重选，不迁移系统其他组的进程')
                group = self.root / ('cleandist-' + str(key[0]) + '-' + uuid.uuid4().hex[:8])
                group.mkdir()
                self.groups[key] = {'path': group, 'device': None}
            record = self.groups[key]
            group = record['path']
            # Replacement semantics: omitted fields clear this app's prior limit.
            old = {name: (group / name).read_text() for name in ('cpu.max', 'memory.high', 'memory.max') if (group / name).exists()}
            old_device = record['device']
            requested_device = device if read_bps is not None or write_bps is not None else None
            old_io = (group / 'io.max').read_text().splitlines() if (group / 'io.max').exists() else []
            try:
                if (group / 'cpu.max').exists():
                    (group / 'cpu.max').write_text(f'{max(1000, round(cpu_cores * 100000)) if cpu_cores is not None else "max"} 100000')
                if (group / 'memory.high').exists():
                    (group / 'memory.high').write_text(str(memory_bytes) if memory_bytes is not None and not hard_memory else 'max')
                    # O_NONBLOCK avoids waiting indefinitely for reclaim/OOM on the desktop request.
                    fd = os.open(group / 'memory.max', os.O_WRONLY | os.O_NONBLOCK)
                    try: os.write(fd, str(memory_bytes).encode() if memory_bytes is not None and hard_memory else b'max')
                    finally: os.close(fd)
                if old_device and old_device != requested_device:
                    (group / 'io.max').write_text(f'{old_device} rbps=max wbps=max')
                if device and (read_bps is not None or write_bps is not None):
                    (group / 'io.max').write_text(f'{device} rbps={read_bps or "max"} wbps={write_bps or "max"}')
                matching_process(key)
                if poller.poll(0): raise ValueError('目标进程已退出')
                (group / 'cgroup.procs').write_text(str(key[0]))
                if poller.poll(0): raise ValueError('目标进程已退出，限制未确认')
            except (OSError, ValueError):
                for name, content in old.items(): (group / name).write_text(content)
                if device: (group / 'io.max').write_text(f'{device} rbps=max wbps=max')
                for content in old_io: (group / 'io.max').write_text(content)
                raise
            record['device'] = requested_device
            if requested_device: record['monitored_device'] = requested_device
            return {'scope': capabilities['scope'], 'group': str(group), 'device': record['device'],
                    'memory_metric': 'memory.current', 'memory_mode': 'hard' if hard_memory else 'pressure'}
        finally:
            os.close(descriptor)

    def clear(self, key):
        record = self.groups[key]
        group = record['path']
        for name, content in (('cpu.max', 'max 100000'), ('memory.high', 'max'), ('memory.max', 'max')):
            if (group / name).exists(): (group / name).write_text(content)
        if record['device']:
            (group / 'io.max').write_text(f'{record["device"]} rbps=max wbps=max')
        record['device'] = None

    def memory_usage(self, key):
        path = self.groups[key]['path'] / 'memory.current'
        return int(path.read_text()) if path.exists() else None

    def io_usage(self, key):
        record = self.groups[key]
        device = record.get('monitored_device')
        result = {'device': device, 'read_bytes': None, 'write_bytes': None}
        path = record['path'] / 'io.stat'
        if device and path.exists():
            for line in path.read_text().splitlines():
                parts = line.split()
                if parts and parts[0] == device:
                    counters = dict(part.split('=', 1) for part in parts[1:])
                    result.update(read_bytes=int(counters['rbytes']) if 'rbytes' in counters else None,
                                  write_bytes=int(counters['wbytes']) if 'wbytes' in counters else None)
        return result

    def shutdown(self):
        errors = []
        for key, record in self.groups.items():
            try:
                self.clear(key)
                # Populated cgroups remain unlimited; never move or signal their members.
                try: record['path'].rmdir()
                except OSError: pass
            except OSError as error: errors.append(str(error))
        if errors: raise OSError('清除 cgroup 限制失败：' + '; '.join(errors))
