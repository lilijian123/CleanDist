"""Windows 适配入口，集中处理原生扫描、通知与进程启动差异。"""
import subprocess
import sys

from .. import common
from ..common import recycle
from ..common import (APP_SOURCES, APP_PUBLISHER_LABEL, PROCESS_COUNTER_LABEL, PRIVATE_MEMORY_LABEL,
                      PRIVATE_MEMORY_NOTE, PROTECTED_PIDS, STOP_EXPLANATION,
                      process_counter, private_memory)
from .apps import scan_apps
from .icons import load_app_icons, load_process_icons
from .disks import list_disks
from .scan import scan_native
from .storage import allocated_bytes, open_metadata
from .watch import DirectoryWatch
from .processes import stop_process


NAME = 'Windows'
HAS_DISK_SCAN = True
FONT_FAMILY = 'Microsoft YaHei UI'
SCAN_NAME = '原生并行扫描'


def is_link(info):
    return common.is_link(info) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def python_executable():
    # 虚拟环境启动器会转发到另一个进程，直接使用基础解释器才能观测真实负载。
    return getattr(sys, '_base_executable', sys.executable)


def process_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW}
