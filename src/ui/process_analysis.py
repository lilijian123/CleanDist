"""选中进程的资源分析；采样任务和跟踪器都绑定进程身份。"""
from datetime import datetime
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout, QHeaderView,
    QSizePolicy, QStackedWidget, QTabWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from ..core.process_analysis import AnalysisHistory, sample_analysis
from ..platforms import system
from .widgets import TrendChart, button, format_size, label, table
from .ai import ProcessAiWidget


def percentage(value, signed=False):
    return '不可用' if value is None else f'{value:.1f}%'


def count(value, signed=False):
    return '不可用' if value is None else f'{value:,.0f}'


def rate(value, signed=False):
    return '不可用' if value is None else f'{value:,.1f}/秒'


def byte_rate(value, signed=False):
    return '不可用' if value is None else format_size(value) + '/秒'


class AnalysisPane(QWidget):
    def __init__(self, run_job, parent=None, fast_job=None):
        super().__init__(parent)
        self.run_job = run_job
        self.run_fast_job = fast_job or run_job
        self.key = self.report = self.trace = None
        self.process_name = ''
        self.report_at = None
        self.closing_trace = None
        self.history = AnalysisHistory()
        self.busy = self.closed = False
        self.deep_busy = False
        self.detail_error = ''
        self.last_deep = None
        self.trace_version = None
        self.deep_tables = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)
        self.status = label('选择进程后自动采样', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.findings = QTextEdit()
        self.findings.setReadOnly(True)
        self.findings.setMinimumHeight(85)
        self.findings.setPlaceholderText('积累有效样本后，显示趋势和可核对的观察证据。')
        self.ai = ProcessAiWidget(lambda: (self.report, self.process_name, self.report_at), self)
        layout.addWidget(self.ai, 1)
        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle('资源明细 / 趋势 / 依据')
        self.details_dialog.resize(800, 620)
        self.details_dialog.setSizeGripEnabled(True)
        dialog_layout = QVBoxLayout(self.details_dialog)
        self.detail_status = label('', 'muted')
        self.detail_status.setWordWrap(True)
        dialog_layout.addWidget(self.detail_status)
        detail_tabs = QTabWidget()
        dialog_layout.addWidget(detail_tabs)
        detail_page, details_layout = self.page()
        detail_tabs.addTab(detail_page, '资源明细')
        trend_page, trend_layout = self.page()
        detail_tabs.addTab(trend_page, '变化趋势')
        detail_tabs.addTab(self.findings, '观察依据')
        line = QHBoxLayout()
        self.section = QComboBox()
        for caption in ('内存构成', 'CPU / 线程', 'I/O / 资源', '文件关联'):
            self.section.addItem(caption)
        line.addWidget(self.section, 1)
        details_layout.addLayout(line)
        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.values = {}
        memory, memory_layout = self.page()
        self.add_values(memory_layout, (
            ('rss', '驻留内存 RSS', format_size), ('uss', '私有驻留 USS', format_size),
            ('pss', '比例分摊 PSS', format_size), ('anonymous', '匿名内存', format_size),
            ('file_pss', '文件映射 PSS', format_size), ('shared_pss', '共享内存 PSS', format_size),
            ('swap', '交换占用', format_size), ('private_commit', 'Windows 私有提交', format_size)))
        memory_layout.addWidget(label('映射占用排行 · 当前详细快照', 'muted'))
        self.mappings = self.make_table(('映射路径', 'RSS', 'PSS', '私有驻留'), memory_layout)
        memory_layout.addStretch()
        self.stack.addWidget(memory)
        cpu, cpu_layout = self.page()
        self.add_values(cpu_layout, (('cpu_percent', 'CPU · 整机', percentage),
            ('cpu_user_percent', '用户态 · 整机', percentage),
            ('cpu_system_percent', '内核态 · 整机', percentage)))
        cpu_layout.addWidget(label('线程排行 · 单核口径 · 需要两次详细快照', 'muted'))
        self.threads = self.make_table(('TID', '总CPU', '用户态', '内核态'), cpu_layout)
        cpu_layout.addStretch()
        self.stack.addWidget(cpu)
        io, io_layout = self.page()
        self.add_values(io_layout, (('read_bps', '读取字节速率', byte_rate),
            ('write_bps', '写入字节速率', byte_rate), ('read_iops', '读取操作速率', rate),
            ('write_iops', '写入操作速率', rate), ('read_chars_bps', '逻辑读取量 · Linux', byte_rate),
            ('write_chars_bps', '逻辑写入量 · Linux', byte_rate),
            ('threads', '线程数', count), ('resources', system.PROCESS_COUNTER_LABEL.split(' / ')[-1], count),
            ('ctx_voluntary_rate', '主动上下文切换', rate),
            ('ctx_involuntary_rate', '被动上下文切换', rate)))
        io_note = label('字节速率来自进程累计计数的差值。Linux存储层字节计数与逻辑字符量分开；Windows进程I/O可包含其他设备操作。首次、计数回退或系统未提供时不可用。', 'muted')
        io_note.setWordWrap(True)
        io_layout.addWidget(io_note)
        io_layout.addStretch()
        self.stack.addWidget(io)
        files, files_layout = self.page()
        note = label('当前可读取的常规文件快照，每约5秒更新；不代表完整访问历史。悬停可查看完整路径。', 'muted')
        note.setWordWrap(True)
        files_layout.addWidget(note)
        self.files = self.make_table(('文件路径', 'FD', '模式', '偏移'), files_layout)
        trace_line = QHBoxLayout()
        self.trace_button = button('开始文件跟踪', self.toggle_trace)
        self.trace_button.setEnabled(False)
        trace_line.addWidget(self.trace_button)
        self.trace_status = label('未开启跟踪', 'muted')
        self.trace_status.setWordWrap(True)
        trace_line.addWidget(self.trace_status, 1)
        files_layout.addLayout(trace_line)
        trace_note = label('Linux：仅记录开启后的读写系统调用，受strace/调试权限限制；不记录数据内容，不覆盖mmap修改。跟踪会增加运行开销，停止跟踪不结束目标。', 'muted')
        trace_note.setWordWrap(True)
        files_layout.addWidget(trace_note)
        self.events = self.make_table(('时间 / TID', '操作', '文件路径', '字节数', '耗时'), files_layout)
        self.events.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.events.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.events.setColumnWidth(0, 125)
        self.stack.addWidget(files)
        details_layout.addWidget(self.stack, 1)
        self.section.currentIndexChanged.connect(self.stack.setCurrentIndex)
        line = QHBoxLayout()
        line.addWidget(label('趋势指标', 'muted'))
        self.metric_combo = QComboBox()
        self.formats = {}
        for name, caption, formatter in (
            ('rss', 'RSS', format_size), ('uss', 'USS', format_size), ('pss', 'PSS', format_size),
            ('cpu_percent', 'CPU · 整机', percentage), ('read_bps', '读取字节/秒', byte_rate),
            ('write_bps', '写入字节/秒', byte_rate), ('resources', 'FD / 句柄', count),
            ('threads', '线程数', count)):
            self.metric_combo.addItem(caption, name)
            self.formats[name] = formatter
        line.addWidget(self.metric_combo, 1)
        trend_layout.addLayout(line)
        self.chart = TrendChart()
        self.chart.empty_text = '当前指标暂无有效数据'
        trend_layout.addWidget(self.chart, 1)
        self.metric_combo.currentIndexChanged.connect(self.render_chart)
        self.errors = label('', 'muted')
        self.errors.setWordWrap(True)
        self.errors.setMaximumHeight(80)
        details_layout.addWidget(self.errors)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.request_sample)
        self.timer.start()
        self.trace_timer = QTimer(self)
        self.trace_timer.setInterval(500)
        self.trace_timer.timeout.connect(self.poll_trace)
        self.trace_timer.start()

    def show_details(self):
        self.details_dialog.show()
        self.details_dialog.raise_()
        self.request_sample()

    def page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(8)
        return widget, layout

    def add_values(self, layout, entries):
        grid = QGridLayout()
        for row, (name, caption, formatter) in enumerate(entries):
            grid.addWidget(label(caption, 'muted'), row, 0)
            value = label('不可用')
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(value, row, 1)
            self.values[name] = value, formatter
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

    def make_table(self, headers, layout):
        view = table(headers)
        view.setMinimumHeight(90)
        view.setMaximumHeight(170)
        for column in range(len(headers)):
            view.setColumnWidth(column, 90 if column else 165)
        view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(view)
        return view

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.request_sample)

    def hideEvent(self, event):
        self.details_dialog.hide()
        super().hideEvent(event)

    def set_key(self, key, name=None):
        if name is not None:
            self.process_name = name
        self.details_dialog.setWindowTitle(f'资源明细 · {self.process_name} · PID {key[0]}' if key else '资源明细 / 趋势 / 依据')
        if self.closed or key == self.key:
            return
        self.stop_trace()
        self.key, self.report, self.last_deep = key, None, None
        self.detail_error = ''
        self.report_at = None
        self.ai.reset()
        self.history.reset()
        self.deep_tables = None
        self.findings.clear()
        self.errors.clear()
        self.status.setText('等待进程采样…' if key else '选择进程开始分析')
        self.detail_status.clear()
        for value, _ in self.values.values():
            value.setText('不可用')
        for view in (self.mappings, self.threads, self.files, self.events):
            view.setRowCount(0)
        self.trace_version = None
        self.trace_status.setText('正在停止上次跟踪器…' if self.closing_trace else '未开启跟踪')
        self.trace_button.setEnabled(key is not None and self.closing_trace is None)
        self.render_chart()
        self.request_sample()

    def request_sample(self):
        if self.closed or self.busy or not self.key or not self.isVisible():
            return
        key = self.key
        self.busy = True

        def accept(sample):
            if self.closed or self.key != key or sample.key != key:
                return
            self.report = self.history.add(sample)
            self.report_at = sample.counter_at if sample.counter_at is not None else sample.captured_at
            self.render_report()
            self.request_details()

        def failed(error):
            if not self.closed and self.key == key:
                self.status.setText(f'分析暂不可用：{error}')
                self.report = None
                self.report_at = None
                self.ai.refresh()
                self.history.reset()
                self.deep_tables = None
                self.last_deep = None
                for value, _ in self.values.values():
                    value.setText('不可用')
                for view in (self.mappings, self.threads, self.files):
                    view.setRowCount(0)
                self.findings.clear()
                self.errors.setText('采样已中断，恢复后重新建立观察窗口。')
                self.render_chart()

        def finished():
            self.busy = False
            if not self.closed and key != self.key:
                self.request_sample()

        self.run_fast_job(lambda: sample_analysis(key, deep=False), accept, failed, finished)

    def request_details(self):
        if (self.closed or self.deep_busy or not self.key or self.report is None or not self.isVisible()
                or self.last_deep is not None and time.monotonic() - self.last_deep < 5):
            return
        key = self.key
        self.deep_busy = True
        self.last_deep = time.monotonic()

        def accept(sample):
            if not self.closed and self.key == key and sample.key == key:
                report = self.history.update_details(sample)
                if report is not None:
                    self.report = report
                    self.detail_error = ''
                    self.render_report()

        def failed(error):
            if not self.closed and self.key == key:
                self.detail_error = f'详细采样暂不可用：{error}'
                if self.report is not None:
                    self.render_report()

        def finished():
            self.deep_busy = False
            if not self.closed and self.key != key:
                self.request_details()

        self.run_job(lambda: sample_analysis(key, deep=True), accept, failed, finished)

    def rows(self, view, rows):
        scroll = view.verticalScrollBar().value()
        view.setUpdatesEnabled(False)
        try:
            view.setRowCount(len(rows))
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    text = str(value) if value is not None else '不可用'
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    view.setItem(row, column, item)
            view.verticalScrollBar().setValue(scroll)
        finally:
            view.setUpdatesEnabled(True)

    def render_report(self):
        report = self.report
        self.ai.refresh()
        age = f'{report.deep_age:.0f}秒前' if report.deep_age is not None else '暂无'
        suffix = ' · 详细指标采集中' if self.deep_busy else ''
        self.status.setText(f'观察 {report.seconds:.0f}秒 · {report.samples}次采样')
        self.detail_status.setText(f'详细快照 {age}{suffix}')
        for name, (value, formatter) in self.values.items():
            value.setText(formatter(report.metrics.get(name)))
        self.findings.setPlainText('\n\n'.join(f'{item.title}\n{item.evidence}' for item in report.findings)
            or '暂未积累足够的持续变化证据。正常波动也可能引起增长，不能据此直接判断泄漏或卡死。')
        self.errors.setText('\n'.join((*report.errors[:6], *((self.detail_error,) if self.detail_error else ()))))
        self.errors.setToolTip('\n'.join(report.errors))
        deep_version = report.deep_at, report.deep_age is not None and 0 <= report.deep_age <= 15
        if deep_version != self.deep_tables:
            self.rows(self.mappings, [(item.path, format_size(item.rss), format_size(item.pss), format_size(item.private))
                                      for item in report.mappings[:20]])
            self.rows(self.threads, [(item.tid, percentage(item.cpu_percent), percentage(item.user_percent), percentage(item.system_percent))
                                     for item in report.threads[:20]])
            self.rows(self.files, [(item.path, item.fd if item.fd >= 0 else None, item.mode, item.position)
                                   for item in report.open_files[:200]])
            self.deep_tables = deep_version
        self.render_chart()

    def render_chart(self, *_):
        name = self.metric_combo.currentData()
        self.chart.label_formatter = self.formats[name]
        self.chart.minimum_ceiling = 1024 * 1024 if self.formats[name] in (format_size, byte_rate) else 1
        self.chart.points = tuple(self.report.curves.get(name, ())) if self.report else ()
        self.chart.update()

    def toggle_trace(self):
        if self.closed or not self.key:
            return
        if self.closing_trace is not None:
            return
        if self.trace is not None:
            self.stop_trace()
            return
        try:
            self.trace = system.FileTrace(self.key)
            self.trace.start()
            self.trace_version = None
            self.trace_button.setText('停止文件跟踪')
            self.trace_status.setText('正在连接目标进程…')
        except (OSError, ValueError, RuntimeError) as error:
            self.stop_trace()
            self.trace_status.setText(str(error))

    def poll_trace(self):
        if self.closed:
            return
        if self.closing_trace is not None:
            state = self.closing_trace.snapshot()
            if not state.finished:
                return
            self.closing_trace = None
            self.trace_button.setEnabled(self.key is not None)
            self.trace_status.setText(state.status.split('；', 1)[0])
            self.trace_status.setToolTip(state.status)
        if self.trace is None:
            return
        state = self.trace.snapshot()
        if state.key != self.key:
            return
        slow = [event.duration_seconds for event in state.events
                if event.duration_seconds is not None and event.duration_seconds >= .1]
        latency = f' · ≥100ms调用 {len(slow)}次' if slow else ''
        self.trace_status.setText(f'{state.status.split("；", 1)[0]}\n显示最近 {min(200, len(state.events))}/{len(state.events)}条'
                                 f' · 丢弃 {state.dropped} · 未解析 {state.unparsed}{latency}')
        self.trace_status.setToolTip(state.status + '\n调用耗时包含等待与调度，不等同于设备硬件延迟。')
        if state.events != self.trace_version:
            self.rows(self.events, [(datetime.fromtimestamp(item.timestamp).strftime('%H:%M:%S.%f')[:-3] + f' / {item.tid}',
                                    item.operation, item.path, item.bytes_count,
                                    f'{item.duration_seconds * 1000:.2f} ms' if item.duration_seconds is not None else None)
                                   for item in state.events[-200:]])
            self.trace_version = state.events
        if not state.active:
            self.stop_trace(preserve_status=True)

    def stop_trace(self, preserve_status=False):
        if self.trace is not None:
            self.trace.close()
            if not self.trace.snapshot().finished:
                self.closing_trace = self.trace
            self.trace = None
        self.trace_button.setText('开始文件跟踪')
        self.trace_button.setEnabled(self.key is not None and self.closing_trace is None and not self.closed)
        if not self.closed and not preserve_status:
            self.trace_status.setText('正在停止跟踪器并解除附加…' if self.closing_trace else '跟踪器已停止')

    def shutdown(self):
        self.closed = True
        self.details_dialog.close()
        self.ai.shutdown()
        self.timer.stop()
        self.trace_timer.stop()
        self.stop_trace()
