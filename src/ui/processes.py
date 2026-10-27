"""为内存表按需补齐进程图标，不重建表格或改变采样流程。"""
import threading

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QStyle

from ..platforms import system


class ProcessIcons(QObject):
    def __init__(self, view, run_job, parent=None):
        super().__init__(parent)
        self.view, self.run_job = view, run_job
        self.icons = {}
        self.active = set()
        self.busy = False
        self.closed = False
        self.cancel = threading.Event()
        self.default = view.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(35)
        self.timer.timeout.connect(self.load)
        view.verticalScrollBar().valueChanged.connect(self.queue)
        view.viewport().installEventFilter(self)

    def refresh(self, keys):
        self.active = set(keys)
        self.icons = {key: icon for key, icon in self.icons.items() if key in self.active}
        self.queue()

    def queue(self, *_):
        if not self.closed:
            self.timer.start()

    def eventFilter(self, watched, event):
        if not self.closed and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.queue()
        return super().eventFilter(watched, event)

    def load(self):
        if self.closed or self.busy or not self.view.isVisible() or not self.view.rowCount():
            return
        top = max(0, self.view.rowAt(0))
        bottom = self.view.rowAt(self.view.viewport().height() - 1)
        if bottom < 0:
            bottom = self.view.rowCount() - 1
        nearby = (list(range(top, bottom + 1)) + list(range(max(0, top - 8), top))
                  + list(range(bottom + 1, min(self.view.rowCount(), bottom + 9))))
        missing = {}
        for row in nearby:
            item = self.view.item(row, 0)
            if item:
                key = item.data(Qt.ItemDataRole.UserRole)
                if key in self.active and key not in self.icons:
                    missing[key] = None
        requests = tuple(missing)[:32]
        if not requests:
            return
        self.busy = True

        def accept(images):
            if self.closed:
                return
            loaded = set(requests) & self.active
            for key in loaded:
                pixmap = QPixmap()
                data = images.get(key, b'')
                self.icons[key] = QIcon(pixmap) if data and pixmap.loadFromData(data) else self.default
            # 仅更新仍对应同一进程身份的名称单元格，保留滚动和选择。
            for row in range(self.view.rowCount()):
                item = self.view.item(row, 0)
                key = item.data(Qt.ItemDataRole.UserRole) if item else None
                if key in loaded:
                    item.setIcon(self.icons[key])

        def finished():
            self.busy = False
            self.queue()

        self.run_job(lambda: system.load_process_icons(requests, self.cancel),
                     accept, lambda error: accept({}), finished)

    def shutdown(self):
        self.closed = True
        self.cancel.set()
        self.timer.stop()
        self.view.viewport().removeEventFilter(self)
