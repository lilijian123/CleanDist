"""主窗口与资源监测交互。"""
from collections import deque
from datetime import datetime
from pathlib import Path
import threading
import time

import psutil
from PySide6.QtCore import QModelIndex, Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QTabWidget, QTableWidgetItem,
    QTextEdit, QTreeView, QVBoxLayout, QWidget,
)
from ..core.files import execute_cleanup, prepare_cleanup, scan_directory, validate_plain_path
from ..core.analysis import analyze_files
from ..platforms import system
from ..core.memory import MemoryHistory, sample_memory
from .dialogs import PreviewDialog
from .files import FileModel
from .theme import STYLE, TEAL
from .widgets import Metric, NumberItem, TrendChart, button, label, format_size, table
from .workers import Job


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'资源观察站 · {system.NAME}原型')
        self.resize(1260, 870)
        self.setMinimumSize(1050, 760)
        self.setStyleSheet(STYLE)
        self.closing = False
        self.jobs = {}
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(3)
        self.history = MemoryHistory()
        self.memory_snapshot = None
        self.memory_busy = False
        self.selected_process = None
        self.current_snapshot = None
        self.comparison_snapshot = None
        self.file_changes = []
        self.file_analysis = None
        self.file_view_key = None
        self.directory_watch = None
        self.last_scan_time = 0
        self.scan_started_at = 0
        self.scan_progress = None
        self.scan_pending = False
        self.scan_generation = 0
        self.scan_busy = False
        self.scan_cancel = threading.Event()
        self.monitoring = False
        self.root = None
        self.cleanup_busy = False
        self.reports = deque(maxlen=20)
        self.demo_process = None
        self._build_ui()
        self.memory_timer = QTimer(self)
        self.memory_timer.timeout.connect(self.request_memory)
        self.memory_timer.start(1000)
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.poll_directory)
        self.file_timer.setInterval(1000)
        QTimer.singleShot(0, self.request_memory)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 22, 26, 12)
        layout.setSpacing(15)
        header = QHBoxLayout()
        mark = label('R', 'brand')
        mark.setFixedSize(52, 52)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(mark)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(label('资源观察站', 'title'))
        titles.addWidget(label('看见资源变化，让每次清理都有依据', 'muted'))
        header.addLayout(titles)
        header.addStretch()
        self.demo_button = button('体验演示', self.start_demo)
        header.addWidget(self.demo_button)
        header.addWidget(label(f'●  {system.NAME} · 本地运行', 'pill'))
        layout.addLayout(header)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self._build_memory_tab()
        self._build_files_tab()
        self.statusBar().showMessage('正在连接系统采样 · 应用不需要管理员权限')

    def _build_memory_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(13)
        cards = QHBoxLayout()
        self.used_card = Metric('系统内存占用率', '系统统计口径')
        self.available_card = Metric('可用内存', '系统可供应用使用的内存')
        self.total_card = Metric('物理内存总量', '当前设备')
        self.process_card = Metric('已采样进程', '每1秒刷新')
        for card in (self.used_card, self.available_card, self.total_card, self.process_card):
            cards.addWidget(card)
        layout.addLayout(cards)
        chart_frame = QFrame()
        chart_frame.setObjectName('card')
        chart_layout = QVBoxLayout(chart_frame)
        chart_layout.setContentsMargins(16, 12, 16, 6)
        self.trend_title = label('进程内存趋势', 'section')
        chart_layout.addWidget(self.trend_title)
        self.chart = TrendChart()
        chart_layout.addWidget(self.chart)
        layout.addWidget(chart_frame)
        line = QHBoxLayout()
        line.addWidget(label('进程工作集', 'section'))
        line.addStretch()
        line.addWidget(label('点击进程查看趋势 · 增量保留正负号', 'muted'))
        layout.addLayout(line)
        self.memory_table = table(['进程名称', 'PID', '当前工作集 RSS', '窗口内变化', '实际观察时长'])
        self.memory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 90), (2, 160), (3, 155), (4, 150)]:
            self.memory_table.setColumnWidth(column, width)
        self.memory_table.itemSelectionChanged.connect(self.select_process)
        layout.addWidget(self.memory_table, 1)
        self.memory_note = label('工作集反映驻留物理内存；增长不等于内存泄漏。', 'muted')
        layout.addWidget(self.memory_note)
        self.tabs.addTab(page, '01  内存监测')

    def _build_files_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        self.root_edit = QLineEdit()
        self.root_edit.setReadOnly(True)
        self.root_edit.setPlaceholderText('选择一个目录，观察文件大小如何变化')
        controls.addWidget(self.root_edit, 1)
        self.choose_button = button('选择目录', self.choose_directory, True)
        self.refresh_button = button('立即扫描', self.resume_scan)
        self.stop_button = button('停止监测', self.stop_directory)
        self.refresh_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        for item in (self.choose_button, self.refresh_button, self.stop_button):
            controls.addWidget(item)
        layout.addLayout(controls)
        cards = QHBoxLayout()
        self.file_count_card = Metric('已扫描文件', '普通文件')
        self.file_size_card = Metric('文件逻辑总大小', '不等同于物理磁盘占用')
        self.file_delta_card = Metric('本轮大小变化', '相比上次采样')
        self.disk_card = Metric('所在分区可用空间', '包含其他应用活动的影响')
        for card in (self.file_count_card, self.file_size_card, self.file_delta_card, self.disk_card):
            cards.addWidget(card)
        layout.addLayout(cards)
        self.scan_note = label('尚未选择目录。首次扫描建立汇总，之后按目录变化更新。', 'muted')
        self.scan_note.setWordWrap(True)
        layout.addWidget(self.scan_note)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        filters = QHBoxLayout()
        self.file_mode = QComboBox()
        for text, mode in [('目录层级', 'tree'), ('大文件', 'large'), ('最近增长', 'growth')]:
            self.file_mode.addItem(text, mode)
        self.file_order = QComboBox()
        self.file_order.addItem('按占用排序', 'size')
        self.file_order.addItem('按增长排序', 'growth')
        self.file_minimum = QComboBox()
        for megabytes in (1, 10, 50, 100, 500):
            self.file_minimum.addItem(f'≥ {megabytes} MB', megabytes * 1024 * 1024)
        self.file_minimum.setCurrentIndex(2)
        self.file_minimum.setEnabled(False)
        self.file_minimum.hide()
        self.file_search = QLineEdit()
        self.file_search.setPlaceholderText('搜索文件名或相对路径，回车查看')
        self.file_search.setClearButtonEnabled(True)
        for widget in (self.file_mode, self.file_order, self.file_minimum, self.file_search):
            filters.addWidget(widget, 1 if widget is self.file_search else 0)
        layout.addLayout(filters)
        self.file_tree = QTreeView()
        self.file_model = FileModel(self)
        self.file_tree.setModel(self.file_model)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_tree.header().setStretchLastSection(False)
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 125), (2, 90), (3, 125), (4, 95), (5, 150)]:
            self.file_tree.setColumnWidth(column, width)
        self.file_model.dataChanged.connect(self.update_selection)
        self.file_mode.currentIndexChanged.connect(self.render_files)
        self.file_order.currentIndexChanged.connect(self.render_files)
        self.file_minimum.currentIndexChanged.connect(self.render_files)
        self.file_search.returnPressed.connect(self.render_files)
        self.file_search.textChanged.connect(lambda text: self.render_files() if not text else None)
        layout.addWidget(self.file_tree, 1)
        details = QHBoxLayout()
        self.file_view_note = label('目录和小文件分组仅作汇总，展开后勾选具体文件。', 'muted')
        details.addWidget(self.file_view_note, 1)
        self.more_button = button('加载更多明细', self.load_more_files)
        self.more_button.setToolTip('选中目录或小文件分组，再加载该层的下一批明细')
        self.more_button.setEnabled(False)
        details.addWidget(self.more_button)
        layout.addLayout(details)
        self.selection_note = label('勾选文件后，可先查看清理预览。', 'muted')
        layout.addWidget(self.selection_note)
        self.last_operation = label('大文件和增长文件仅供分析，不会被自动认定为垃圾。', 'muted')
        self.last_operation.setWordWrap(True)
        layout.addWidget(self.last_operation)
        actions = QHBoxLayout()
        actions.addWidget(label('清理方式：移入回收站 · 不永久删除', 'muted'))
        actions.addStretch()
        self.report_button = button('查看清理报告', self.show_report)
        self.report_button.setEnabled(False)
        actions.addWidget(self.report_button)
        self.preview_button = button('预览选中文件', self.preview_selected, True)
        self.preview_button.setEnabled(False)
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        self.tabs.addTab(page, '02  文件增长与清理')

    def run_job(self, action, success, failure, finished=None):
        if self.closing:
            return
        job = Job(action)
        token = id(job)
        self.jobs[token] = job
        job.signals.result.connect(lambda result: success(result) if not self.closing else None)
        job.signals.error.connect(lambda error: failure(error) if not self.closing else None)

        def done():
            self.jobs.pop(token, None)
            if finished:
                finished()

        job.signals.finished.connect(done)
        self.thread_pool.start(job)

    def request_memory(self):
        if self.closing or self.memory_busy:
            return
        self.memory_busy = True
        self.run_job(lambda: sample_memory(self.history), self.accept_memory,
                     lambda error: self.statusBar().showMessage(f'内存采样暂不可用：{error}'),
                     lambda: setattr(self, 'memory_busy', False))

    def accept_memory(self, snapshot):
        self.memory_snapshot = snapshot
        self.used_card.value.setText(f'{snapshot.percent:.1f}%')
        self.available_card.value.setText(format_size(snapshot.available))
        self.total_card.value.setText(format_size(snapshot.total))
        self.process_card.value.setText(str(len(snapshot.processes)))
        self.memory_table.blockSignals(True)
        sort_column = self.memory_table.horizontalHeader().sortIndicatorSection()
        order = self.memory_table.horizontalHeader().sortIndicatorOrder()
        self.memory_table.setSortingEnabled(False)
        self.memory_table.setRowCount(len(snapshot.processes))
        for row, process in enumerate(snapshot.processes):
            name = QTableWidgetItem(process.name)
            name.setData(Qt.ItemDataRole.UserRole, process.key)
            self.memory_table.setItem(row, 0, name)
            self.memory_table.setItem(row, 1, NumberItem(str(process.key[0]), process.key[0]))
            self.memory_table.setItem(row, 2, NumberItem(format_size(process.rss), process.rss))
            delta = NumberItem(format_size(process.delta, True), process.delta)
            if process.delta:
                delta.setForeground(QColor('#ffbd83' if process.delta > 0 else TEAL))
            self.memory_table.setItem(row, 3, delta)
            self.memory_table.setItem(row, 4, NumberItem(f'{process.observed_seconds:.0f} 秒', process.observed_seconds))
        if self.selected_process not in {process.key for process in snapshot.processes}:
            self.selected_process = snapshot.processes[0].key if snapshot.processes else None
        if not hasattr(self, '_memory_sorted'):
            sort_column, order = 2, Qt.SortOrder.DescendingOrder
            self._memory_sorted = True
        self.memory_table.setSortingEnabled(True)
        self.memory_table.sortItems(sort_column, order)
        for row in range(self.memory_table.rowCount()):
            if self.memory_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == self.selected_process:
                # 刷新选中行时保留滚动位置。
                scroll = self.memory_table.verticalScrollBar().value()
                self.memory_table.selectRow(row)
                self.memory_table.verticalScrollBar().setValue(scroll)
                break
        self.memory_table.blockSignals(False)
        self.draw_process()
        updated_at = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        self.memory_note.setText(f'每1秒采样 · 最近更新 {updated_at} · 不可读/已退出 {snapshot.unreadable} 项 · RSS增长不等于内存泄漏')
        self.statusBar().showMessage(f'系统采样正常 · {updated_at}    |    目录监测：{"进行中" if self.monitoring else "未启动 / 已停止"}')
        if self.demo_process and self.demo_process.poll() is not None:
            self.demo_process = None
            self.demo_button.setText('体验演示')

    def select_process(self):
        selected = self.memory_table.selectionModel().selectedRows()
        if selected:
            self.selected_process = self.memory_table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self.draw_process()

    def draw_process(self):
        process = next((process for process in self.memory_snapshot.processes if process.key == self.selected_process), None) if self.memory_snapshot else None
        self.chart.points = process.points if process else ()
        self.chart.update()
        self.trend_title.setText(f'{process.name} · PID {process.key[0]} · 工作集趋势' if process else '进程内存趋势')

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, '选择要监测的目录', str(self.root or Path.home()))
        if path:
            self.start_directory(Path(path))

    def start_directory(self, root):
        if self.cleanup_busy:
            return
        try:
            root = validate_plain_path(root)
            if not root.is_dir():
                raise ValueError('请选择目录')
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '无法监测目录', str(error))
            return
        self.stop_directory()
        self.root = root
        self.root_edit.setText(str(root))
        self.current_snapshot = self.comparison_snapshot = None
        self.file_changes = []
        self.file_analysis = None
        self.file_view_key = None
        self.scan_progress = None
        self.scan_pending = False
        for card in (self.file_count_card, self.file_size_card, self.file_delta_card, self.disk_card):
            card.value.setText('—')
        filters = (self.file_mode, self.file_order, self.file_minimum, self.file_search)
        for widget in filters:
            widget.blockSignals(True)
        self.file_mode.setCurrentIndex(0)
        self.file_order.setCurrentIndex(0)
        self.file_minimum.setCurrentIndex(2)
        self.file_search.clear()
        for widget in filters:
            widget.blockSignals(False)
        self.render_files()
        self.file_view_note.setText('正在扫描新目录，完成后显示文件明细。')
        self.last_operation.setText('大文件和增长文件仅供分析，不会被自动认定为垃圾。')
        self.scan_note.setToolTip('')
        self.scan_note.setText('正在切换目录，等待上一轮扫描结束…' if self.scan_busy else '正在扫描新目录…')
        self.progress.show()
        self.monitoring = True
        self.directory_watch = system.DirectoryWatch(root)
        self.file_timer.start()
        self.stop_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.request_scan()

    def stop_directory(self):
        self.monitoring = False
        self.scan_generation += 1
        self.scan_cancel.set()
        if self.directory_watch is not None:
            self.directory_watch.close()
            self.directory_watch = None
        if hasattr(self, 'file_timer'):
            self.file_timer.stop()
        if hasattr(self, 'stop_button'):
            self.stop_button.setEnabled(False)
            self.scan_note.setText('监测已停止 · 保留最近一次结果，未执行清理' if self.current_snapshot
                                   else '扫描已停止 · 本次尚无结果')
            if self.current_snapshot is None:
                self.file_view_note.setText('扫描已停止，尚未生成本次结果。')
        if hasattr(self, 'progress'):
            self.progress.hide()

    def resume_scan(self):
        if not self.root or self.cleanup_busy:
            return
        self.monitoring = True
        if self.directory_watch is None:
            self.directory_watch = system.DirectoryWatch(self.root)
        self.file_timer.start()
        self.stop_button.setEnabled(True)
        self.request_scan()

    def poll_directory(self):
        if self.scan_busy and self.scan_progress is not None:
            generation, info = self.scan_progress
            if generation == self.scan_generation and self.monitoring:
                elapsed = time.monotonic() - self.scan_started_at
                self.scan_note.setText(f'{info.stage} · 已扫描 {info.files:,} 个文件 / {info.directories:,} 个目录 · 已用 {elapsed:.1f}秒 · 可随时停止')
        if not self.monitoring or self.scan_busy or self.cleanup_busy:
            return
        changed = self.directory_watch.changed() if self.directory_watch else None
        self.scan_pending = self.scan_pending or bool(changed)
        # 变化通知避免空闲时重复扫描，定时扫描用于补偿遗漏的通知。
        interval = 30 if changed is None else 300
        since_scan = time.monotonic() - self.last_scan_time
        min_interval = 30 if self.root is not None and self.root.parent == self.root else 0
        if (self.scan_pending and since_scan >= min_interval) or since_scan >= interval:
            self.request_scan()

    def request_scan(self):
        if self.closing or not self.monitoring or self.scan_busy or self.cleanup_busy or not self.root:
            return
        self.scan_busy = True
        generation, root = self.scan_generation, self.root
        previous = self.comparison_snapshot
        self.scan_started_at = time.monotonic()
        self.scan_progress = None
        self.scan_pending = False
        cancel = threading.Event()
        self.scan_cancel = cancel
        self.progress.show()
        self.scan_note.setText('正在只读扫描 · 可随时停止，清理前需要单独确认')

        def failed(error):
            if generation == self.scan_generation:
                state = '保留上次结果' if self.current_snapshot else '本次尚无结果'
                self.scan_note.setText(f'本轮扫描失败，{state}：{error}')
                if self.current_snapshot is None:
                    self.file_view_note.setText('扫描失败，请检查目录访问权限或重试。')

        def finished():
            self.scan_busy = False
            if generation == self.scan_generation:
                self.last_scan_time = time.monotonic()
            if not self.closing:
                self.progress.hide()
                if self.monitoring and generation != self.scan_generation:
                    QTimer.singleShot(0, self.request_scan)

        def scan():
            def report(info):
                self.scan_progress = (generation, info)
            snapshot = scan_directory(root, cancel, progress=report)
            return None if snapshot.cancelled else analyze_files(previous, snapshot, cancel=cancel, progress=report)

        self.run_job(scan, lambda result: self.accept_scan(generation, result), failed, finished)

    def accept_scan(self, generation, result):
        if self.closing or generation != self.scan_generation or not self.monitoring or result is None or self.cleanup_busy:
            return
        snapshot = result.snapshot
        self.file_analysis = result
        self.file_changes = result.changes
        self.current_snapshot = snapshot
        self.comparison_snapshot = result.comparison
        self.file_count_card.value.setText(f'{len(snapshot.files):,}')
        self.file_size_card.value.setText(format_size(snapshot.total_bytes))
        delta = result.directories[''].delta
        self.file_delta_card.value.setText(format_size(delta, True) if result.comparable else '统计不完整')
        self.disk_card.value.setText(format_size(snapshot.free_bytes))
        self.render_files()
        quality = '完整扫描' if snapshot.complete else f'部分结果 · 跳过 {snapshot.skipped} 项'
        if snapshot.truncated:
            quality += ' · 达到本次扫描限制'
        updated_at = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        watching = self.directory_watch is not None and self.directory_watch.available
        policy = '目录变化时更新 · 每5分钟校验' if watching else '变化通知不可用 · 每30秒扫描'
        if self.root.parent == self.root and watching:
            policy = '整盘变化合并更新 · 至少间隔30秒'
        engine = system.SCAN_NAME if snapshot.backend != 'python' else '兼容扫描'
        total = time.monotonic() - self.scan_started_at
        note = f'{quality} · {updated_at} · {engine} {snapshot.elapsed:.2f}秒 / 总耗时 {total:.2f}秒 · {policy}'
        self.scan_note.setText(note)
        self.scan_note.setToolTip('\n'.join(snapshot.errors) or '符号链接、重解析点及特殊文件会跳过；未捕获两轮扫描之间的短暂变化。')
        self.update_selection()

    def render_files(self, *_):
        mode = self.file_mode.currentData()
        self.file_minimum.setEnabled(mode == 'large')
        self.file_minimum.setVisible(mode == 'large')
        self.file_order.setEnabled(mode == 'tree' and not self.file_search.text().strip())
        view_key = (self.root, mode, self.file_minimum.currentData(), self.file_search.text(), self.file_order.currentData())
        preserve = view_key == self.file_view_key
        scroll = self.file_tree.verticalScrollBar().value() if preserve else 0
        expanded = []
        for key, node in self.file_model.nodes.items():
            index = self.file_model.createIndex(node.row, 0, node)
            if preserve and self.file_tree.isExpanded(index):
                expanded.append((key, len(node.children)))
        root_rows = self.file_model.rowCount() if preserve else 0
        self.file_model.set_analysis(self.file_analysis, mode, self.file_minimum.currentData(),
                                     self.file_search.text(), self.file_order.currentData())
        while self.file_model.rowCount() < root_rows and self.file_model.has_more():
            self.file_model.load_more()
        for key, count in sorted(expanded, key=lambda item: item[0].count('/')):
            node = self.file_model.nodes.get(key)
            if node:
                index = self.file_model.createIndex(node.row, 0, node)
                while len(node.children) < count and self.file_model.has_more(index):
                    self.file_model.load_more(index)
                self.file_tree.expand(index)
        self.file_view_key = view_key
        self.file_tree.verticalScrollBar().setValue(scroll)
        note = ('列表最多展示前500项，按需加载；可搜索相对路径缩小范围。' if self.file_model.flat
                else '汇总包含未展开的小文件；展开查看明细，每批200项。')
        self.file_view_note.setText(note)
        self.more_button.setEnabled(self.file_analysis is not None)
        self.update_selection()

    def load_more_files(self):
        index = self.file_tree.currentIndex()
        if index.isValid():
            index = index.siblingAtColumn(0)
            if not self.file_model.hasChildren(index):
                index = index.parent()
        if not self.file_model.has_more(index):
            index = QModelIndex()
        self.file_model.load_more(index)

    def selected_records(self):
        return self.file_model.selected_records()

    def update_selection(self, *_):
        records = self.selected_records()
        self.selection_note.setText(f'已勾选 {len(records)} 个文件 · 逻辑大小 {format_size(sum(record.size for record in records))} · 用户手动选择')
        self.preview_button.setEnabled(bool(records) and not self.cleanup_busy)

    def set_cleanup_busy(self, busy):
        self.cleanup_busy = busy
        self.choose_button.setEnabled(not busy)
        self.refresh_button.setEnabled(bool(self.root) and not busy)
        self.stop_button.setEnabled(self.monitoring and not busy)
        self.update_selection()

    def preview_selected(self):
        records = self.selected_records()
        if self.cleanup_busy or not records or not self.root:
            return
        self.scan_generation += 1
        self.scan_cancel.set()
        self.set_cleanup_busy(True)

        def failed(error):
            self.set_cleanup_busy(False)
            QMessageBox.warning(self, '预览未通过核验', str(error))

        self.run_job(lambda: prepare_cleanup(self.root, records), self.show_preview, failed)

    def show_preview(self, plan):
        dialog = PreviewDialog(plan, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.set_cleanup_busy(False)
            return

        def failed(error):
            self.set_cleanup_busy(False)
            self.last_operation.setText(f'清理未完成：{error}')

        self.last_operation.setText('正在移入回收站 · 执行前重新核验每个文件')
        self.run_job(lambda: execute_cleanup(plan), self.accept_cleanup, failed)

    def accept_cleanup(self, report):
        self.set_cleanup_busy(False)
        counts = {state: sum(result.state == state for result in report.results) for state in ('成功', '跳过', '失败')}
        text = f"已移入回收站 {counts['成功']} 项（{format_size(report.recycled_bytes)}）· 跳过 {counts['跳过']} · 失败 {counts['失败']}"
        self.last_operation.setText(text + '。回收站未清空，不代表已释放等量空间。')
        lines = [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text,
                 '移入回收站的大小为文件逻辑大小，非实际空间收益。']
        if report.before_free is not None and report.after_free is not None:
            lines.append('所在分区可用空间变化（全局）：' + format_size(report.after_free - report.before_free, True))
        lines.extend(f'[{result.state}] {result.record.path}\n  {result.message}' for result in report.results)
        self.reports.append('\n'.join(lines))
        self.report_button.setEnabled(True)
        if self.monitoring:
            self.request_scan()

    def show_report(self):
        if not self.reports:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('清理结果 · 仅保留本次运行的最近20次记录')
        dialog.resize(850, 540)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText('\n\n' + ('\n\n' + '─' * 45 + '\n\n').join(reversed(self.reports)))
        layout.addWidget(text)
        layout.addWidget(button('关闭', dialog.close))
        dialog.show()
        self._report_dialog = dialog

    def start_demo(self):
        if self.demo_process and self.demo_process.poll() is None:
            self.demo_process.terminate()
            self.demo_process = None
            self.demo_button.setText('体验演示')
            return
        if self.cleanup_busy:
            return
        from ..demo.workload import create_demo_directory, launch_demo
        root = create_demo_directory()
        self.demo_process = launch_demo(root)
        self.selected_process = (self.demo_process.pid, psutil.Process(self.demo_process.pid).create_time())
        self.demo_button.setText('停止演示')
        self.start_directory(root)
        self.tabs.setCurrentIndex(1)
        self.last_operation.setText('演示使用新建临时目录和独立测试进程，持续约2分钟。目录会保留供你体验清理。')

    def closeEvent(self, event):
        self.closing = True
        self.memory_timer.stop()
        self.stop_directory()
        if self.demo_process and self.demo_process.poll() is None:
            self.demo_process.terminate()
        event.accept()
