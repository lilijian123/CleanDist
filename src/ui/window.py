"""Main window and resource-monitoring interactions."""
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import threading

import psutil
from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QTabWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)
from ..core.files import diff_snapshots, execute_cleanup, prepare_cleanup, scan_directory, validate_plain_path
from ..core.memory import MemoryHistory, sample_memory
from .dialogs import PreviewDialog
from .theme import STYLE, TEAL
from .widgets import Metric, NumberItem, TrendChart, button, label, size_text, table
from .workers import Job


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('资源观察站 · Windows原型')
        self.resize(1260, 870)
        self.setMinimumSize(1050, 760)
        self.setStyleSheet(STYLE)
        self.closing = False
        self.jobs = {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.history = MemoryHistory()
        self.memory_snapshot = None
        self.memory_busy = False
        self.selected_process = None
        self.current_snapshot = None
        self.comparison_snapshot = None
        self.file_changes = []
        self.scan_generation = 0
        self.scan_busy = False
        self.scan_cancel = threading.Event()
        self.monitoring = False
        self.root = None
        self.cleanup_busy = False
        self.reports = deque(maxlen=20)
        self.demo_process = None
        self._build()
        self.memory_timer = QTimer(self)
        self.memory_timer.timeout.connect(self.request_memory)
        self.memory_timer.start(1000)
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.request_scan)
        self.file_timer.setInterval(5000)
        QTimer.singleShot(0, self.request_memory)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        box = QVBoxLayout(central)
        box.setContentsMargins(26, 22, 26, 12)
        box.setSpacing(15)
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
        header.addWidget(label('●  Windows · 本地运行', 'pill'))
        box.addLayout(header)
        self.tabs = QTabWidget()
        box.addWidget(self.tabs)
        self._memory_page()
        self._files_page()
        self.statusBar().showMessage('正在连接系统采样 · 应用不需要管理员权限')

    def _memory_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 16, 0, 0)
        box.setSpacing(13)
        cards = QHBoxLayout()
        self.used_card = Metric('系统内存占用率', '系统统计口径')
        self.available_card = Metric('可用内存', '系统可供应用使用的内存')
        self.total_card = Metric('物理内存总量', '当前设备')
        self.process_card = Metric('已采样进程', '每1秒刷新')
        for card in (self.used_card, self.available_card, self.total_card, self.process_card):
            cards.addWidget(card)
        box.addLayout(cards)
        chart_box = QFrame()
        chart_box.setObjectName('card')
        chart_layout = QVBoxLayout(chart_box)
        chart_layout.setContentsMargins(16, 12, 16, 6)
        self.trend_title = label('进程内存趋势', 'section')
        chart_layout.addWidget(self.trend_title)
        self.chart = TrendChart()
        chart_layout.addWidget(self.chart)
        box.addWidget(chart_box)
        line = QHBoxLayout()
        line.addWidget(label('进程工作集', 'section'))
        line.addStretch()
        line.addWidget(label('点击进程查看趋势 · 增量保留正负号', 'muted'))
        box.addLayout(line)
        self.memory_table = table(['进程名称', 'PID', '当前工作集 RSS', '窗口内变化', '实际观察时长'])
        self.memory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in [(1, 90), (2, 160), (3, 155), (4, 150)]:
            self.memory_table.setColumnWidth(col, width)
        self.memory_table.itemSelectionChanged.connect(self.select_process)
        box.addWidget(self.memory_table, 1)
        self.memory_note = label('工作集反映驻留物理内存；增长不等于内存泄漏。', 'muted')
        box.addWidget(self.memory_note)
        self.tabs.addTab(page, '01  内存监测')

    def _files_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 16, 0, 0)
        box.setSpacing(12)
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
        box.addLayout(controls)
        cards = QHBoxLayout()
        self.file_count_card = Metric('已扫描文件', '普通文件')
        self.file_size_card = Metric('文件逻辑总大小', '不等同于物理磁盘占用')
        self.file_delta_card = Metric('本轮大小变化', '相比上次采样')
        self.disk_card = Metric('所在分区可用空间', '包含其他应用活动的影响')
        for card in (self.file_count_card, self.file_size_card, self.file_delta_card, self.disk_card):
            cards.addWidget(card)
        box.addLayout(cards)
        self.scan_note = label('尚未选择目录。只读扫描每5秒进行一次，不会自动清理。', 'muted')
        self.scan_note.setWordWrap(True)
        box.addWidget(self.scan_note)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        box.addWidget(self.progress)
        self.file_table = table(['选择', '文件 / 相对路径', '逻辑大小', '本轮增量', '状态', '修改时间'])
        self.file_table.setColumnWidth(0, 58)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in [(2, 125), (3, 125), (4, 95), (5, 155)]:
            self.file_table.setColumnWidth(col, width)
        self.file_table.itemChanged.connect(self.update_selection)
        box.addWidget(self.file_table, 1)
        self.selection_note = label('勾选文件后，可先查看清理预览。', 'muted')
        box.addWidget(self.selection_note)
        self.last_operation = label('大文件和增长文件仅供分析，不会被自动认定为垃圾。', 'muted')
        self.last_operation.setWordWrap(True)
        box.addWidget(self.last_operation)
        actions = QHBoxLayout()
        actions.addWidget(label('清理方式：移入回收站 · 不永久删除', 'muted'))
        actions.addStretch()
        self.report_button = button('查看清理报告', self.show_report)
        self.report_button.setEnabled(False)
        actions.addWidget(self.report_button)
        self.preview_button = button('预览选中文件', self.preview_selected, True)
        self.preview_button.setEnabled(False)
        actions.addWidget(self.preview_button)
        box.addLayout(actions)
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
        self.pool.start(job)

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
        self.available_card.value.setText(size_text(snapshot.available))
        self.total_card.value.setText(size_text(snapshot.total))
        self.process_card.value.setText(str(len(snapshot.processes)))
        self.memory_table.blockSignals(True)
        sorting = self.memory_table.horizontalHeader().sortIndicatorSection()
        order = self.memory_table.horizontalHeader().sortIndicatorOrder()
        self.memory_table.setSortingEnabled(False)
        self.memory_table.setRowCount(len(snapshot.processes))
        for row, process in enumerate(snapshot.processes):
            name = QTableWidgetItem(process.name)
            name.setData(Qt.ItemDataRole.UserRole, process.key)
            self.memory_table.setItem(row, 0, name)
            self.memory_table.setItem(row, 1, NumberItem(str(process.key[0]), process.key[0]))
            self.memory_table.setItem(row, 2, NumberItem(size_text(process.rss), process.rss))
            delta = NumberItem(size_text(process.delta, True), process.delta)
            if process.delta:
                delta.setForeground(QColor('#ffbd83' if process.delta > 0 else TEAL))
            self.memory_table.setItem(row, 3, delta)
            self.memory_table.setItem(row, 4, NumberItem(f'{process.observed_seconds:.0f} 秒', process.observed_seconds))
        if self.selected_process not in {p.key for p in snapshot.processes}:
            self.selected_process = snapshot.processes[0].key if snapshot.processes else None
        if not hasattr(self, '_memory_sorted'):
            sorting, order = 2, Qt.SortOrder.DescendingOrder
            self._memory_sorted = True
        self.memory_table.setSortingEnabled(True)
        self.memory_table.sortItems(sorting, order)
        for row in range(self.memory_table.rowCount()):
            if self.memory_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == self.selected_process:
                # Preserve scroll position while refreshing a selected row.
                scroll = self.memory_table.verticalScrollBar().value()
                self.memory_table.selectRow(row)
                self.memory_table.verticalScrollBar().setValue(scroll)
                break
        self.memory_table.blockSignals(False)
        self.draw_process()
        stamp = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        self.memory_note.setText(f'每1秒采样 · 最近更新 {stamp} · 不可读/已退出 {snapshot.unreadable} 项 · RSS增长不等于内存泄漏')
        self.statusBar().showMessage(f'系统采样正常 · {stamp}    |    目录监测：{"进行中" if self.monitoring else "未启动 / 已停止"}')
        if self.demo_process and self.demo_process.poll() is not None:
            self.demo_process = None
            self.demo_button.setText('体验演示')

    def select_process(self):
        selected = self.memory_table.selectionModel().selectedRows()
        if selected:
            self.selected_process = self.memory_table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self.draw_process()

    def draw_process(self):
        process = next((p for p in self.memory_snapshot.processes if p.key == self.selected_process), None) if self.memory_snapshot else None
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
        self.file_table.setRowCount(0)
        self.update_selection()
        self.monitoring = True
        self.file_timer.start()
        self.stop_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.request_scan()

    def stop_directory(self):
        self.monitoring = False
        self.scan_generation += 1
        self.scan_cancel.set()
        if hasattr(self, 'file_timer'):
            self.file_timer.stop()
        if hasattr(self, 'stop_button'):
            self.stop_button.setEnabled(False)
            self.scan_note.setText('监测已停止 · 保留最近一次结果，未执行清理')
        if hasattr(self, 'progress'):
            self.progress.hide()

    def resume_scan(self):
        if not self.root or self.cleanup_busy:
            return
        self.monitoring = True
        self.file_timer.start()
        self.stop_button.setEnabled(True)
        self.request_scan()

    def request_scan(self):
        if self.closing or not self.monitoring or self.scan_busy or self.cleanup_busy or not self.root:
            return
        self.scan_busy = True
        generation, root = self.scan_generation, self.root
        cancel = threading.Event()
        self.scan_cancel = cancel
        self.progress.show()
        self.scan_note.setText('正在只读扫描 · 可随时停止，清理前需要单独确认')

        def failed(error):
            if generation == self.scan_generation:
                self.scan_note.setText(f'本轮扫描失败，保留上次结果：{error}')

        def finished():
            self.scan_busy = False
            if not self.closing:
                self.progress.hide()
                if self.monitoring and generation != self.scan_generation:
                    QTimer.singleShot(0, self.request_scan)

        self.run_job(lambda: scan_directory(root, cancel), lambda s: self.accept_scan(generation, s), failed, finished)

    def accept_scan(self, generation, snapshot):
        if self.closing or generation != self.scan_generation or not self.monitoring or snapshot.cancelled or self.cleanup_busy:
            return
        previous = self.comparison_snapshot
        selected = {(r.relative, r.fingerprint) for r in self.selected_records()}
        self.file_changes = diff_snapshots(previous, snapshot)
        self.current_snapshot = snapshot
        if not snapshot.complete and previous:
            self.comparison_snapshot = replace(snapshot, files={**previous.files, **snapshot.files})
        else:
            self.comparison_snapshot = snapshot
        self.file_count_card.value.setText(f'{len(snapshot.files):,}')
        self.file_size_card.value.setText(size_text(snapshot.total_bytes))
        delta = sum(c.delta for c in self.file_changes)
        self.file_delta_card.value.setText(size_text(delta, True) if snapshot.complete and (previous is None or previous.complete) else '统计不完整')
        self.disk_card.value.setText(size_text(snapshot.free_bytes))
        self.file_table.blockSignals(True)
        shown = self.file_changes[:2000]
        self.file_table.setRowCount(len(shown))
        for row, change in enumerate(shown):
            record = change.record
            check = QTableWidgetItem()
            check.setData(Qt.ItemDataRole.UserRole, record)
            if change.state not in ('已删除', '未确认'):
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked if (record.relative, record.fingerprint) in selected else Qt.CheckState.Unchecked)
            else:
                check.setFlags(Qt.ItemFlag.NoItemFlags)
            self.file_table.setItem(row, 0, check)
            name = QTableWidgetItem(record.relative)
            name.setToolTip(str(record.path))
            self.file_table.setItem(row, 1, name)
            self.file_table.setItem(row, 2, NumberItem(size_text(0 if change.state == '已删除' else record.size), record.size))
            delta_item = NumberItem(size_text(change.delta, True), change.delta)
            if change.delta:
                delta_item.setForeground(QColor('#ffbd83' if change.delta > 0 else TEAL))
            self.file_table.setItem(row, 3, delta_item)
            state = QTableWidgetItem(change.state)
            if change.state in ('增长', '新增'):
                state.setForeground(QColor('#ffbd83'))
            self.file_table.setItem(row, 4, state)
            self.file_table.setItem(row, 5, QTableWidgetItem(datetime.fromtimestamp(record.modified_ns / 1e9).strftime('%m-%d %H:%M:%S')))
        self.file_table.blockSignals(False)
        quality = '完整扫描' if snapshot.complete else f'部分结果 · 跳过 {snapshot.skipped} 项'
        if snapshot.truncated:
            quality += ' · 已达50,000个文件上限'
        stamp = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        note = f'{quality} · {stamp} · 耗时 {snapshot.elapsed:.2f}秒 · 每5秒刷新 · 显示 {len(shown):,}/{len(self.file_changes):,} 项'
        self.scan_note.setText(note)
        self.scan_note.setToolTip('\n'.join(snapshot.errors) or '符号链接、重解析点及特殊文件会跳过；未捕获两轮扫描之间的短暂变化。')
        self.update_selection()

    def selected_records(self):
        selected = []
        for row in range(self.file_table.rowCount()):
            item = self.file_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(item.data(Qt.ItemDataRole.UserRole))
        return selected

    def update_selection(self, *_):
        records = self.selected_records()
        self.selection_note.setText(f'已勾选 {len(records)} 个文件 · 逻辑大小 {size_text(sum(r.size for r in records))} · 用户手动选择')
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
        counts = {state: sum(r.state == state for r in report.results) for state in ('成功', '跳过', '失败')}
        text = f"已移入回收站 {counts['成功']} 项（{size_text(report.recycled_bytes)}）· 跳过 {counts['跳过']} · 失败 {counts['失败']}"
        self.last_operation.setText(text + '。回收站未清空，不代表已释放等量空间。')
        lines = [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text,
                 '移入回收站的大小为文件逻辑大小，非实际空间收益。']
        if report.before_free is not None and report.after_free is not None:
            lines.append('所在分区可用空间变化（全局）：' + size_text(report.after_free - report.before_free, True))
        lines.extend(f'[{r.state}] {r.record.path}\n  {r.message}' for r in report.results)
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
        box = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText('\n\n' + ('\n\n' + '─' * 45 + '\n\n').join(reversed(self.reports)))
        box.addWidget(text)
        box.addWidget(button('关闭', dialog.close))
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
