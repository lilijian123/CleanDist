"""Linux 适配入口：XDG应用、inotify通知与安全的单进程SIGTERM。"""
from ..common import (
    FONT_FAMILY, SCAN_NAME, is_link, list_disks, process_options,
    python_executable, recycle, scan_native, allocated_bytes, open_metadata, context_counters,
)
from .apps import scan_apps
from .icons import load_app_icons, load_process_icons
from .processes import stop_process
from .watch import DirectoryWatch
from .analysis import sample_memory_breakdown, thread_identity
from .process_trace import FileTrace
from .identity import process_created


NAME = 'Linux'
HAS_DISK_SCAN = False
APP_SOURCES = ('桌面应用', 'Snap应用', 'Flatpak应用', '便携候选')
APP_PUBLISHER_LABEL = '软件包维护者'
PROCESS_COUNTER_LABEL = '线程 / 文件描述符'
PRIVATE_MEMORY_LABEL = '私有驻留（USS）'
PRIVATE_MEMORY_NOTE = '仅该进程使用的驻留物理内存（USS），来自 /proc；无读取权限时显示不可用。'
PROTECTED_PIDS = (0, 1)
STOP_EXPLANATION = '向该进程发送 SIGTERM 普通结束请求，不强制结束、不结束子进程。'


def process_counter(process):
    return process.num_fds()


def private_memory(process, memory):
    return process.memory_full_info().uss
