"""Linux 待适配入口，暂复用通用兼容行为，尚未在 Linux 系统验收。"""
from ..common import (
    DirectoryWatch, FONT_FAMILY, SCAN_NAME, is_link, list_disks, process_options,
    python_executable, recycle, scan_apps, scan_native, load_app_icons, load_process_icons,
    allocated_bytes, open_metadata,
)


NAME = 'Linux（待适配）'
