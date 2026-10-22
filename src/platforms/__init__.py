"""统一选择平台实现，业务与界面只通过 system 访问平台差异。"""
import sys


if sys.platform == 'win32':
    from . import windows as system
elif sys.platform.startswith('linux'):
    from . import linux as system
else:
    from . import common as system
