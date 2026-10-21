"""Command-line entry; load Qt only when starting the desktop interface."""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description='资源观察站 Windows桌面原型')
    parser.add_argument('--demo', action='store_true', help='启动自建临时数据的演示')
    args = parser.parse_args()

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .ui.window import MainWindow

    qt = QApplication(sys.argv[:1])
    qt.setFont(QFont('Microsoft YaHei UI', 10))
    window = MainWindow()
    window.show()
    if args.demo:
        QTimer.singleShot(400, window.start_demo)
    code = qt.exec()
    window.pool.waitForDone()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
