"""选中进程详情、手动普通结束，以及内存表的按需图标。"""
import threading
from datetime import datetime
import os

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QMessageBox, QSizePolicy, QStyle, QVBoxLayout, QWidget

from ..platforms import system
from ..core.processes import cpu_percentage, sample_process_details, summarize_memory
from .widgets import TrendChart, button, format_size, label
from .process_analysis import AnalysisPane


class ProcessPanel(QWidget):
    """只为选中进程采集详情；旧选择、关闭后的回调不会更新界面。"""
    def __init__(self, run_job, refresh=None, fast_job=None):
        super().__init__()
        self.run_job, self.refresh = run_job, refresh
        self.key = None
        self.details = None
        self.busy = self.stopping = self.closed = False
        self.setMinimumWidth(350)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 0, 4, 0)
        layout.setSpacing(12)
        header = QHBoxLayout()
        self.title = label('选择进程查看详情', 'section')
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.stop_button = button('结束进程', self.stop_selected)
        self.stop_button.setObjectName('danger')
        self.stop_button.setEnabled(False)
        header.addWidget(self.stop_button)
        self.force_button = button('强制结束', lambda: self.stop_selected(force=True))
        self.force_button.setObjectName('danger')
        self.force_button.setVisible(getattr(system, 'CAN_FORCE_STOP', False))
        self.force_button.setEnabled(False)
        header.addWidget(self.force_button)
        layout.addLayout(header)
        self.operation = label('', 'muted')
        self.operation.setWordWrap(True)
        self.operation.hide()
        layout.addWidget(self.operation)
        self.quick_summary = label('选择进程后显示关键指标', 'muted')
        self.quick_summary.setWordWrap(True)
        layout.addWidget(self.quick_summary)
        actions = QHBoxLayout()
        self.details_button = button('进程详情 ▸', self.show_details)
        self.resource_button = button('资源明细 / 趋势 / 依据 ▸', self.show_resources)
        actions.addWidget(self.details_button)
        actions.addWidget(self.resource_button)
        actions.addStretch()
        main_layout = layout
        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle('进程详情')
        self.details_dialog.resize(720, 650)
        self.details_dialog.setSizeGripEnabled(True)
        layout = QVBoxLayout(self.details_dialog)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)
        information = QGridLayout()
        information.setHorizontalSpacing(16)
        self.fields = {}
        for row, (key, caption) in enumerate((('pid', 'PID'), ('path', '程序路径'), ('started', '启动时间'),
                ('parent', '父进程'), ('status', '运行状态'), ('cpu', 'CPU（整机）'),
                ('threads', system.PROCESS_COUNTER_LABEL), ('private', system.PRIVATE_MEMORY_LABEL))):
            information.addWidget(label(caption, 'muted'), row, 0)
            value = label('不可用')
            value.setWordWrap(True)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            value.setMaximumHeight(44)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fields[key] = value
            information.addWidget(value, row, 1)
        information.setColumnStretch(1, 1)
        self.fields['cpu'].setToolTip('两次CPU时间差除以观察时长和逻辑处理器数；首次采样不可用。')
        self.fields['private'].setToolTip(system.PRIVATE_MEMORY_NOTE)
        layout.addLayout(information)
        metrics = QFrame()
        metrics.setObjectName('card')
        grid = QGridLayout(metrics)
        self.metrics = {}
        for column, (key, caption) in enumerate((('current', '当前RSS'), ('peak', '窗口峰值'),
                                                ('mean', '采样均值'), ('minimum', '窗口最低'))):
            grid.addWidget(label(caption, 'muted'), 0, column)
            value = label('不可用', 'processMetric')
            self.metrics[key] = value
            grid.addWidget(value, 1, column)
        layout.addWidget(metrics)
        line = QHBoxLayout()
        self.observation = label('等待有效采样', 'muted')
        line.addWidget(self.observation, 1)
        self.chart_mode = QComboBox()
        self.chart_mode.addItem('占用总量', False)
        self.chart_mode.addItem('增量细节', True)
        self.chart_mode.setCurrentIndex(1)
        self.chart_mode.setToolTip('增量细节相对于窗口起点，可显示较小的增长和下降。')
        self.chart_mode.currentIndexChanged.connect(self.change_chart)
        line.addWidget(self.chart_mode)
        layout.addLayout(line)
        self.chart = TrendChart()
        self.chart.relative = True
        self.chart.setMinimumHeight(110)
        layout.addWidget(self.chart, 1)
        self.summary = label('', 'muted')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.note = label('统计限于观察窗口，工作集变化不能直接判定内存泄漏。', 'muted')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        layout.addStretch()
        self.analysis = AnalysisPane(run_job, fast_job=fast_job)
        main_layout.addWidget(self.analysis, 1)
        main_layout.addLayout(actions)

    def open_ai(self):
        if not self.closed and self.key:
            self.analysis.ai.analyze_button.setFocus()
            self.analysis.request_sample()

    def show_details(self):
        self.details_dialog.show()
        self.details_dialog.raise_()
        self.request_details()

    def show_resources(self):
        self.analysis.show_details()

    def hideEvent(self, event):
        self.details_dialog.hide()
        super().hideEvent(event)

    def change_chart(self, *_):
        self.chart.relative = bool(self.chart_mode.currentData())
        self.chart.update()

    def update_stop_buttons(self):
        enabled = bool(self.key and not self.closed and not self.stopping and
                       self.key[0] not in (*system.PROTECTED_PIDS, os.getpid()))
        self.stop_button.setEnabled(enabled)
        self.force_button.setEnabled(enabled and getattr(system, 'CAN_FORCE_STOP', False))

    def set_reading(self, reading):
        if self.closed:
            return
        key = reading.key if reading else None
        self.analysis.set_key(key, reading.name if reading else '')
        if key != self.key:
            self.key, self.details = key, None
            self.update_stop_buttons()
            self.operation.clear()
            self.operation.hide()
            for field in self.fields.values():
                field.setText('不可用')
        self.chart.points = reading.points if reading else ()
        self.chart.update()
        self.title.setText(reading.name if reading else '选择进程查看详情')
        self.details_dialog.setWindowTitle(f'进程详情 · {reading.name} · PID {key[0]}' if reading else '进程详情')
        self.quick_summary.setText(f'PID {key[0]} · RSS {format_size(reading.rss)} · 窗口变化 {format_size(reading.delta, True)}'
                                   if reading else '选择进程后显示关键指标')
        stats = summarize_memory(reading.points) if reading else None
        for name, value in self.metrics.items():
            number = getattr(stats, name) if stats else None
            if name == 'current' and reading and reading.rss is None:
                number = None
            value.setText(format_size(number))
        if stats:
            self.observation.setText(f'窗口 {stats.seconds:.0f}秒 · {stats.samples}次采样')
            rate = format_size(stats.rate, True) + '/秒' if stats.rate is not None else '等待第二次采样'
            self.summary.setText(f'窗口净变化 {format_size(stats.delta, True)} · 平均变化 {rate}\n'
                                 f'波动范围 {format_size(stats.peak - stats.minimum)}；均值为有效样本均值。')
        else:
            self.observation.setText('等待有效采样')
            self.summary.clear()
        if key:
            self.fields['pid'].setText(str(key[0]))
            self.fields['started'].setText(datetime.fromtimestamp(key[1]).strftime('%Y-%m-%d %H:%M:%S'))
            self.request_details()

    def request_details(self):
        if self.closed or self.busy or not self.key or not self.isVisible():
            return
        key = self.key
        self.busy = True

        def accept(details):
            if self.closed or self.key != key:
                return
            percent = cpu_percentage(self.details, details)
            self.details = details
            self.fields['path'].setText(details.path or '不可用')
            self.fields['path'].setToolTip(details.path or '不可用')
            if details.started_at is not None:
                self.fields['started'].setText(datetime.fromtimestamp(details.started_at).strftime('%Y-%m-%d %H:%M:%S'))
            parent = str(details.parent_pid) if details.parent_pid is not None else '不可用'
            self.fields['parent'].setText(f'{details.parent_name} · PID {parent}' if details.parent_name else parent)
            self.fields['status'].setText({'running': '运行中', 'sleeping': '等待中', 'stopped': '已停止',
                                          'zombie': '已退出'}.get(details.status, details.status or '不可用'))
            self.fields['cpu'].setText('不可用' if details.cpu_time is None else
                                       f'{percent:.1f}%' if percent is not None else '等待有效的间隔采样')
            self.fields['threads'].setText(f'{details.threads if details.threads is not None else "不可用"} / '
                                           f'{details.handles if details.handles is not None else "不可用"}')
            self.fields['private'].setText(format_size(details.private))
            self.note.setText(('部分信息不可读取。' if details.errors else '') +
                              '统计限于观察窗口，工作集变化不能直接判定内存泄漏。')
            self.note.setToolTip('\n'.join(details.errors))
            self.update_stop_buttons()

        def failed(error):
            if not self.closed and self.key == key:
                self.details = None
                self.update_stop_buttons()
                self.note.setText(f'详情暂不可用：{error}')

        def finished():
            self.busy = False
            if not self.closed and self.key != key:
                self.request_details()

        self.run_job(lambda: sample_process_details(key), accept, failed, finished)

    def stop_selected(self, *, force=False):
        if self.closed or self.stopping or not self.key or not self.stop_button.isEnabled():
            return
        if force and not getattr(system, 'CAN_FORCE_STOP', False):
            return
        key, name = self.key, self.title.text()
        path = (self.details.path if self.details and self.details.key == key else None) or '路径不可用'
        mode = '强制结束' if force else '结束进程'
        explanation = ('立即终止所选进程，未保存的数据将无法恢复；不结束子进程。' if force
                       else system.STOP_EXPLANATION)
        answer = QMessageBox.question(self, mode,
            f'是否{mode} {name}（PID {key[0]}）？\n{path}\n\n未保存的数据可能丢失。'
            f'\n{explanation}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes or self.closed:
            return
        self.stopping = True
        self.update_stop_buttons()
        if self.key == key:
            self.operation.setText(f'正在请求{mode} PID {key[0]}…')
            self.operation.show()

        def show(message):
            if not self.closed and self.key == key:
                self.operation.setText(f'PID {key[0]}：{message}')
                self.operation.show()

        def finished():
            self.stopping = False
            self.update_stop_buttons()
            if not self.closed:
                self.request_details()
                if self.refresh:
                    self.refresh()

        self.run_job(lambda: system.stop_process(key, force=True) if force else system.stop_process(key),
                     lambda result: show(result.message), show, finished)

    def shutdown(self):
        self.closed = True
        self.details_dialog.close()
        self.analysis.shutdown()


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
