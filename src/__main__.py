"""命令行入口，默认启动Vue桌面。"""
import argparse
import sys

from .platforms import system
from . import APP_NAME


def main():
    parser = argparse.ArgumentParser(description=f'{APP_NAME} {system.NAME}桌面应用')
    parser.add_argument('--demo', action='store_true', help='启动自建临时数据的演示')
    parser.add_argument('--legacy', action='store_true', help='启动旧版Qt界面进行兼容性对照')
    args = parser.parse_args()

    if not args.legacy:
        from .desktop.app import main as desktop_main
        return desktop_main(demo=args.demo)

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .ui.window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setFont(QFont(system.FONT_FAMILY, 10))
    window = MainWindow()
    window.show()
    if args.demo:
        QTimer.singleShot(400, window.start_demo)
    code = app.exec()
    window.thread_pool.waitForDone()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
