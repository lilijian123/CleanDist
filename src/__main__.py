"""命令行入口，仅在启动桌面界面时加载 Qt。"""
import argparse
import sys

from .platforms import system
from . import APP_NAME


def main():
    parser = argparse.ArgumentParser(description=f'{APP_NAME} {system.NAME}桌面原型')
    parser.add_argument('--demo', action='store_true', help='启动自建临时数据的演示')
    args = parser.parse_args()

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
