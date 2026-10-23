"""应用清单的数据、去重与位置筛选；便携程序只作为待确认候选。"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class AppRecord:
    name: str
    version: str
    publisher: str
    path: Path | None
    source: str
    icon_path: Path | None = field(default=None, compare=False)
    icon_index: int = field(default=0, compare=False)
    icon: bytes = field(default=b'', compare=False, repr=False)

    @property
    def drive(self):
        return self.path.anchor if self.path else '未定位'


@dataclass(frozen=True)
class AppScanResult:
    apps: tuple
    errors: tuple
    skipped: int
    cancelled: bool
    elapsed: float


@dataclass(frozen=True)
class AppProgress:
    stage: str
    found: int


def select_apps(records, roots=None, *, include_unknown=False):
    roots = tuple(Path(root) for root in roots) if roots is not None else None
    selected = {}
    for record in records:
        if roots is not None:
            if record.path is None:
                if not include_unknown:
                    continue
            elif not any(record.path.is_relative_to(root) for root in roots):
                continue
        key = (record.name.casefold(), record.version.casefold(),
               os.path.normcase(str(record.path)) if record.path else '', record.source)
        selected.setdefault(key, record)
    order = {'桌面应用': 0, '商店应用': 1, '便携候选': 2}
    return tuple(sorted(selected.values(), key=lambda app: (order.get(app.source, 3),
                                                            app.name.casefold(), str(app.path))))


def is_portable_name(name):
    # 名称过滤只是前置筛选；还需要快捷方式或便携配置作为应用依据。
    stem = Path(name).stem.casefold()
    maintenance = {'setup', 'install', 'uninstall', 'uninstaller', 'update', 'updater',
                   'crashpad_handler', 'qtwebengineprocess', 'maintenancetool',
                   'notification_helper', 'elevation_service', 'vc_redist.x64', 'vc_redist.x86'}
    tokens = re.split(r'[\s._-]+', stem)
    return (Path(name).suffix.casefold() == '.exe' and stem not in maintenance
            and not set(tokens).intersection({'setup', 'install', 'installer', 'uninstall',
                                               'uninstaller', 'update', 'updater', 'helper', 'crashpad'})
            and not re.fullmatch(r'unins\d+', stem))
