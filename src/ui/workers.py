"""Qt background work with results delivered through signals."""
from PySide6.QtCore import QObject, QRunnable, Signal


class JobSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Job(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.result.emit(self.action())
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            self.signals.finished.emit()
