"""主窗口与资源监测交互。"""
from collections import deque
from datetime import datetime
from pathlib import Path
import threading
import os
from dataclasses import replace
import time

import psutil
from PySide6.QtCore import QModelIndex, QSettings, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QTabWidget, QTabBar, QToolButton, QTableWidgetItem,
    QScrollArea, QSplitter, QStackedWidget, QTextEdit, QTreeView, QVBoxLayout, QWidget,
)
from ..core.batch import ScanBatch, record_key, scan_batch, analyze_batch, build_matrix
from ..core.batch_cleanup import prepare_batch_cleanup, execute_batch_cleanup
from ..core.cleanup import CleanupAnalysis
from ..core.ai import summarize_cleanup
from ..core.storage import find_duplicates
from ..core.analysis import analyze_files
from ..platforms import system
from ..core.memory import MemoryHistory, sample_memory
from .. import APP_NAME
from .dialogs import PreviewDialog
from .cleanup import CleanupPage
from .apps import AppPage
from .file_tools import LargeFilesPage, DuplicatePage
from .processes import ProcessIcons, ProcessPanel
from .treemap import MapPage
from .theme import STYLE, LIGHT_STYLE, color
from .widgets import BrandMark, CachedDelegate, FocusEdit, Metric, ScanMetric, NumberItem, SettingsButton, ThemeChoice, TrendChart, button, enable_click_selection, enable_pixel_scroll, label, format_size, number_font, table
from .workers import Job
from .ai import AiSettingsPage, FileAiDialog


class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        enable_click_selection()
        self.setWindowTitle(f'{APP_NAME} · {system.NAME}')
        self.resize(1260, 870)
        self.setMinimumSize(1050, 760)
        self.setStyleSheet(STYLE)
        self.setFont(number_font())
        self.settings = settings if settings is not None else QSettings(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'ResourceObserver', 'settings')
        self.closing = False
        self.centered = False
        self.jobs = {}
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(3)
        self.analysis_pool = QThreadPool(self)
        self.analysis_pool.setMaxThreadCount(1)
        self.history = MemoryHistory()
        self.memory_snapshot = None
        self.memory_busy = False
        self.selected_process = None
        self.current_snapshot = None
        self.batch_analysis = None
        self.roots = ()
        self.root = None
        self.scan_kind = 'folder'
        self.selected_disks = set()
        self.scan_generation = 0
        self.scan_busy = False
        self.scan_cancel = threading.Event()
        self.scan_progress = {}
        self.scan_started_at = 0
        self.content_generation = 0
        self.content_cancel = threading.Event()
        self.matrix_key = None
        self.duplicate_key = None
        self.duplicate_result = None
        self.scan_pool = QThreadPool(self)
        self.scan_pool.setMaxThreadCount(1)
        self.content_pool = QThreadPool(self)
        self.content_pool.setMaxThreadCount(1)
        self.cleanup_busy = False
        self.reports = deque(maxlen=20)
        self.demo_process = None
        self._build_ui()
        self.theme_combo.setCurrentIndex(1 if self.settings.value('theme', 'dark') == 'light' else 0)
        self.apply_theme()
        self.tabs.currentChanged.connect(lambda index: self.render_memory() if index == 0 else None)
        self.memory_timer = QTimer(self)
        self.memory_timer.timeout.connect(self.request_memory)
        self.memory_timer.start(1000)
        self.file_timer = QTimer(self)
        self.file_timer.timeout.connect(self.poll_scan)
        self.file_timer.setInterval(100)
        QTimer.singleShot(0, self.request_memory)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.centered:
            self.centered = True
            # 首次显示后窗口边框尺寸已知；只居中一次，不干扰用户后续拖动。
            screen = QApplication.screenAt(QCursor.pos()) or self.screen()
            area = screen.availableGeometry()
            border = self.frameGeometry().size() - self.size()
            width = max(1, area.width() - border.width() - 32)
            height = max(1, area.height() - border.height() - 32)
            self.setMinimumSize(min(self.minimumWidth(), width), min(self.minimumHeight(), height))
            self.resize(min(self.width(), width), min(self.height(), height))
            frame = self.frameGeometry()
            frame.moveCenter(area.center())
            self.move(frame.topLeft())

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(26, 22, 26, 12)
        layout.setSpacing(15)
        header = QHBoxLayout()
        header.setSpacing(12)
        self.brand = BrandMark()
        header.addWidget(self.brand)
        header.addWidget(label(APP_NAME, 'title'))
        header.addStretch()
        self.settings_button = SettingsButton(self.show_settings)
        header.addWidget(self.settings_button)
        self.demo_button = button('体验演示', self.start_demo)
        header.addWidget(self.demo_button)
        header.addWidget(label(system.NAME, 'pill'))
        layout.addLayout(header)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        self.tabs = QTabWidget()
        self.pages.addWidget(self.tabs)
        self._build_memory_tab()
        self._build_files_tab()
        self.apps_page = AppPage(self.run_job)
        self.tabs.addTab(self.apps_page, '03  应用扫描')
        self._build_settings_page()
        self.statusBar().showMessage('正在获取系统状态…')

    def _build_settings_page(self):
        self.settings_page = QWidget()
        layout = QVBoxLayout(self.settings_page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(18)
        header = QHBoxLayout()
        self.settings_back = button('返回', self.close_settings)
        header.addWidget(self.settings_back)
        header.addWidget(label('设置', 'section'))
        header.addStretch()
        layout.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(24)
        self.settings_nav = QListWidget()
        self.settings_nav.setObjectName('settingsNav')
        self.settings_nav.setFixedWidth(176)
        self.settings_nav.addItem('外观')
        self.settings_nav.addItem('AI 设置')
        self.settings_nav.setCurrentRow(0)
        body.addWidget(self.settings_nav)
        self.settings_stack = QStackedWidget()
        appearance_page = QWidget()
        appearance = QVBoxLayout(appearance_page)
        appearance.setContentsMargins(0, 0, 0, 0)
        appearance.setSpacing(18)
        appearance.addWidget(label('外观', 'title'))
        card = QFrame()
        card.setObjectName('card')
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 18, 20, 18)
        row.addWidget(label('主题'))
        row.addStretch()
        self.theme_combo = ThemeChoice()
        self.theme_combo.setObjectName('themeChoice')
        self.theme_combo.setAccessibleName('主题')
        self.theme_combo.addItem('深色', 'dark')
        self.theme_combo.addItem('浅色', 'light')
        self.theme_combo.setMinimumWidth(160)
        self.theme_combo.currentIndexChanged.connect(self.apply_theme)
        row.addWidget(self.theme_combo)
        appearance.addWidget(card)
        appearance.addStretch()
        self.settings_stack.addWidget(appearance_page)
        self.ai_settings = AiSettingsPage(self.settings, self.run_job)
        self.settings_stack.addWidget(self.ai_settings)
        self.settings_nav.currentRowChanged.connect(self.settings_stack.setCurrentIndex)
        body.addWidget(self.settings_stack, 1)
        layout.addLayout(body, 1)
        self.pages.addWidget(self.settings_page)
        self.process_panel.analysis.ai.settings_page = self.ai_settings
        self.process_panel.analysis.ai.open_settings = self.show_ai_settings
        self.file_ai_dialog = FileAiDialog(self.ai_settings, self.show_ai_settings, self)

    def show_ai_settings(self):
        self.settings_nav.setCurrentRow(1)
        self.show_settings()

    def show_settings(self):
        self.pages.setCurrentWidget(self.settings_page)
        self.demo_button.hide()

    def close_settings(self):
        self.pages.setCurrentWidget(self.tabs)
        self.demo_button.show()
        if self.tabs.currentIndex() == 0:
            self.render_memory()

    def apply_theme(self, *_):
        if not hasattr(self, 'apps_page'):
            return
        light = self.theme_combo.currentData() == 'light'
        self.setProperty('theme', 'light' if light else 'dark')
        self.brand.update()
        self.settings_button.refresh_icon()
        self.setWindowIcon(self.brand.icon())
        self.setStyleSheet(LIGHT_STYLE if light else STYLE)
        palette = QPalette()
        for role, dark, day in (
            (QPalette.ColorRole.Window, '#151515', '#fcfcfb'),
            (QPalette.ColorRole.Base, '#151515', '#fcfcfb'),
            (QPalette.ColorRole.AlternateBase, '#1a1a19', '#f5f5f3'),
            (QPalette.ColorRole.WindowText, '#d6d6d1', '#30302d'),
            (QPalette.ColorRole.Text, '#d6d6d1', '#30302d'),
            (QPalette.ColorRole.Button, '#20201e', '#efefec'),
            (QPalette.ColorRole.ButtonText, '#d6d6d1', '#30302d'),
            (QPalette.ColorRole.Highlight, '#293029', '#e0e9df'),
            (QPalette.ColorRole.HighlightedText, '#e1e8de', '#253d2b'),
            (QPalette.ColorRole.ToolTipBase, '#20201e', '#efefec'),
            (QPalette.ColorRole.ToolTipText, '#d6d6d1', '#30302d'),
        ):
            palette.setColor(role, QColor(day if light else dark))
        self.setPalette(palette)
        font = number_font(light)
        self.setFont(font)
        self.apps_page.model.number_font = font
        for page in (self.cleanup_page, self.large_page, self.duplicate_page):
            page.model.set_fonts(font, 700 if light else 660)
        for metric in self.findChildren(Metric):
            metric.value.setFont(font)
        for row in range(self.memory_table.rowCount()):
            for column in range(1, self.memory_table.columnCount()):
                item = self.memory_table.item(row, column)
                if item:
                    item.setFont(font)
            delta = self.memory_table.item(row, 3)
            if delta and delta.data(Qt.ItemDataRole.UserRole + 1):
                delta.setForeground(QColor(color(self, 'positive' if delta.data(Qt.ItemDataRole.UserRole + 1) > 0 else 'accent')))
        for delegate in self.findChildren(CachedDelegate):
            delegate.cache.clear()
        self.chart.update()
        for page in (self.cleanup_page, self.large_page, self.duplicate_page):
            page.tree.viewport().update()
        self.apps_page.table.viewport().update()
        self.settings.setValue('theme', 'light' if light else 'dark')
        self.settings.sync()

    def _build_memory_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(13)
        cards = QHBoxLayout()
        self.used_card = Metric('内存占用率')
        self.available_card = Metric('可用内存')
        self.total_card = Metric('物理内存')
        self.process_card = Metric('进程数')
        for card in (self.used_card, self.available_card, self.total_card, self.process_card):
            cards.addWidget(card)
        layout.addLayout(cards)
        split = QSplitter(Qt.Orientation.Horizontal)
        listing = QWidget()
        listing.setMinimumWidth(400)
        list_layout = QVBoxLayout(listing)
        list_layout.setContentsMargins(0, 0, 0, 0)
        line = QHBoxLayout()
        line.addWidget(label('进程工作集', 'section'))
        line.addStretch()
        list_layout.addLayout(line)
        self.memory_table = table(['进程名称', 'PID', '当前工作集 RSS', '窗口内变化', '实际观察时长'])
        self.memory_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.memory_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 65), (2, 125), (3, 120), (4, 105)]:
            self.memory_table.setColumnWidth(column, width)
        self.memory_table.itemSelectionChanged.connect(self.select_process)
        self.memory_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.memory_table.customContextMenuRequested.connect(self.process_menu)
        self.memory_table.setIconSize(QSize(20, 20))
        self.process_icons = ProcessIcons(self.memory_table, self.run_job, self)
        list_layout.addWidget(self.memory_table, 1)
        split.addWidget(listing)
        self.process_panel = ProcessPanel(self.run_job, self.request_memory,
            fast_job=lambda *args: self.run_job(*args, pool=self.analysis_pool))
        self.chart = self.process_panel.chart
        self.trend_title = self.process_panel.title
        self.process_panel.setMinimumWidth(380)
        split.addWidget(self.process_panel)
        split.setChildrenCollapsible(False)
        split.setSizes([650, 520])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, '01  内存监测')

    def _build_files_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(10)
        controls = QHBoxLayout()
        self.scan_mode = QComboBox()
        self.scan_mode.addItem('文件夹扫描', 'folder')
        if system.HAS_DISK_SCAN:
            self.scan_mode.addItem('多磁盘扫描', 'disk')
        self.scan_mode.setVisible(system.HAS_DISK_SCAN)
        controls.addWidget(self.scan_mode)
        self.target_inputs = QStackedWidget()
        folder = QWidget()
        folder_layout = QHBoxLayout(folder)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.root_edit = FocusEdit()
        self.root_edit.setPlaceholderText('输入文件夹路径，例如 /tmp、/var 或 /mnt 下的目录' if system.NAME == 'Linux' else '输入文件夹完整路径')
        folder_layout.addWidget(self.root_edit, 1)
        self.choose_button = button('浏览文件夹', self.choose_directory)
        self.path_scan_button = button('扫描路径', self.scan_path, True)
        folder_layout.addWidget(self.choose_button)
        folder_layout.addWidget(self.path_scan_button)
        self.root_edit.returnPressed.connect(self.scan_path)
        self.target_inputs.addWidget(folder)
        disk = QWidget()
        disk_layout = QHBoxLayout(disk)
        disk_layout.setContentsMargins(0, 0, 0, 0)
        self.drive_picker = QToolButton()
        self.drive_picker.setText('选择磁盘（可多选）')
        self.drive_picker.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.disk_menu = QMenu(self.drive_picker)
        self.drive_picker.setMenu(self.disk_menu)
        self.disk_menu.aboutToShow.connect(self.refresh_disks)
        disk_layout.addWidget(self.drive_picker, 1)
        self.disk_button = button('扫描所选磁盘', self.scan_disk, True)
        self.disk_button.setEnabled(False)
        disk_layout.addWidget(self.disk_button)
        self.target_inputs.addWidget(disk)
        controls.addWidget(self.target_inputs, 1)
        self.scan_mode.currentIndexChanged.connect(self.change_scan_mode)
        self.refresh_button = button('重新扫描', self.resume_scan)
        self.stop_button = button('停止扫描', self.stop_directory)
        self.refresh_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)
        cards = QHBoxLayout()
        self.file_count_card = ScanMetric('已扫描文件')
        self.file_size_card = ScanMetric('文件总大小')
        self.target_card = ScanMetric('完成目标')
        self.disk_card = ScanMetric('可用空间')
        for card in (self.file_count_card, self.file_size_card, self.target_card, self.disk_card):
            cards.addWidget(card, 1)
        layout.addLayout(cards)
        status_row = QHBoxLayout()
        self.scan_note = label('尚未扫描', 'muted')
        self.scan_note.setTextFormat(Qt.TextFormat.PlainText)
        status_row.addWidget(self.scan_note, 1)
        self.scan_details_button = QToolButton()
        self.scan_details_button.setText('扫描详情')
        self.scan_details_button.setAutoRaise(True)
        self.scan_details_button.clicked.connect(self.show_scan_details)
        self.scan_details_button.hide()
        status_row.addWidget(self.scan_details_button)
        layout.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.file_mode = QTabBar()
        for title in ('分类清理', '大文件清理', '重复文件清理', '空间矩阵分析'):
            self.file_mode.addTab(title)
        self.file_mode.setExpanding(False)
        layout.addWidget(self.file_mode)
        scope = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem('全部扫描目标', None)
        self.source_combo.setMinimumWidth(180)
        scope.addWidget(self.source_combo)
        self.file_path = FocusEdit()
        self.file_path.setReadOnly(True)
        self.file_path.setObjectName('filePath')
        self.file_path.setPlaceholderText('文件完整路径 / 扫描范围')
        scope.addWidget(self.file_path, 1)
        layout.addLayout(scope)
        self.cleanup_page = CleanupPage(self)
        self.large_page = LargeFilesPage(self)
        self.duplicate_page = DuplicatePage(self)
        self.map_page = MapPage(self)
        self.map_page.map.file_clicked.connect(self.show_map_file)
        self.file_workspace = QStackedWidget()
        for content in (self.cleanup_page, self.large_page, self.duplicate_page, self.map_page):
            self.file_workspace.addWidget(content)
        layout.addWidget(self.file_workspace, 1)
        self.last_operation = label('', 'muted')
        self.last_operation.setWordWrap(True)
        self.last_operation.hide()
        layout.addWidget(self.last_operation)
        actions = QHBoxLayout()
        self.selection_note = label('未选择文件', 'muted')
        actions.addWidget(self.selection_note, 1)
        self.report_button = button('查看清理报告', self.show_report)
        self.report_button.setEnabled(False)
        actions.addWidget(self.report_button)
        self.preview_button = button('预览选中文件', self.preview_selected, True)
        self.preview_button.setEnabled(False)
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        for content in (self.cleanup_page, self.large_page, self.duplicate_page):
            content.checkedChanged.connect(self.update_selection)
            content.tree.aiRequested.connect(self.show_file_ai)
        self.file_mode.currentChanged.connect(self.change_file_tool)
        self.source_combo.currentIndexChanged.connect(self.change_scope)
        self.large_page.filterChanged.connect(self.update_selection)
        self.duplicate_page.startRequested.connect(self.start_duplicates)
        self.duplicate_page.stopRequested.connect(self.stop_content)
        self.duplicate_page.minimum.currentIndexChanged.connect(self.change_duplicate_options)
        self.tabs.addTab(page, '02  清理与空间')

    def show_file_ai(self, index):
        if self.closing or not index.isValid():
            return
        model = index.model()
        number, _ = model.location(index)
        group, record = model.groups[number], model.record_at(index)
        protected = (record_key(record) in model._blocked if record is not None else
                     any(record_key(item) in model._blocked for item in group.records))
        summary = summarize_cleanup(group, record, platform=system.NAME,
                                    complete=bool(self.current_snapshot and self.current_snapshot.complete),
                                    read_only=protected)
        self.file_ai_dialog.show_summary(summary)

    def run_job(self, action, success, failure, finished=None, *, pool=None):
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
        (pool if pool is not None else self.thread_pool).start(job)

    def request_memory(self):
        if self.closing or self.memory_busy:
            return
        self.memory_busy = True
        self.run_job(lambda: sample_memory(self.history), self.accept_memory,
                     lambda error: self.statusBar().showMessage(f'内存采样暂不可用：{error}'),
                     lambda: setattr(self, 'memory_busy', False))

    def accept_memory(self, snapshot):
        self.memory_snapshot = snapshot
        # 后台继续采样，隐藏页面只保存最新快照，切回时再绘制进程表。
        if self.pages.currentWidget() is self.tabs and self.tabs.currentIndex() == 0:
            self.render_memory()
        updated_at = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        self.statusBar().showMessage(f'更新 {updated_at} · {"扫描中" if self.scan_busy else "就绪"}')
        if self.demo_process and self.demo_process.poll() is not None:
            self.demo_process = None
            self.demo_button.setText('体验演示')

    def render_memory(self):
        snapshot = self.memory_snapshot
        if snapshot is None:
            return
        self.used_card.value.setText(f'{snapshot.percent:.1f}%')
        self.available_card.value.setText(format_size(snapshot.available))
        self.total_card.value.setText(format_size(snapshot.total))
        self.process_card.value.setText(str(len(snapshot.processes)))
        self.process_icons.refresh(process.key for process in snapshot.processes)
        # 刷新期间只重绘一次，并在重建、排序和恢复选中行后还原用户的像素位置。
        scroll_y = self.memory_table.verticalScrollBar().value()
        scroll_x = self.memory_table.horizontalScrollBar().value()
        self.memory_table.setUpdatesEnabled(False)
        self.memory_table.blockSignals(True)
        try:
            sort_column = self.memory_table.horizontalHeader().sortIndicatorSection()
            order = self.memory_table.horizontalHeader().sortIndicatorOrder()
            self.memory_table.setSortingEnabled(False)
            self.memory_table.setRowCount(len(snapshot.processes))
            for row, process in enumerate(snapshot.processes):
                name = QTableWidgetItem(process.name)
                name.setData(Qt.ItemDataRole.UserRole, process.key)
                name.setIcon(self.process_icons.icons.get(process.key, self.process_icons.default))
                self.memory_table.setItem(row, 0, name)
                self.memory_table.setItem(row, 1, NumberItem(str(process.key[0]), process.key[0]))
                self.memory_table.setItem(row, 2, NumberItem(format_size(process.rss), process.rss))
                delta = NumberItem(format_size(process.delta, True), process.delta)
                if process.delta:
                    delta.setForeground(QColor(color(self, 'positive' if process.delta > 0 else 'accent')))
                self.memory_table.setItem(row, 3, delta)
                self.memory_table.setItem(row, 4, NumberItem(f'{process.observed_seconds:.0f} 秒', process.observed_seconds))
                for column in range(1, 5):
                    self.memory_table.item(row, column).setFont(number_font(self.property('theme') == 'light'))
            if self.selected_process not in {process.key for process in snapshot.processes}:
                self.selected_process = snapshot.processes[0].key if snapshot.processes else None
            if not hasattr(self, '_memory_sorted'):
                sort_column, order = 2, Qt.SortOrder.DescendingOrder
                self._memory_sorted = True
            self.memory_table.horizontalHeader().setSortIndicator(sort_column, order)
            self.memory_table.setSortingEnabled(True)
            for row in range(self.memory_table.rowCount()):
                if self.memory_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == self.selected_process:
                    self.memory_table.selectRow(row)
                    break
        finally:
            self.memory_table.blockSignals(False)
            self.memory_table.verticalScrollBar().setValue(scroll_y)
            self.memory_table.horizontalScrollBar().setValue(scroll_x)
            self.memory_table.setUpdatesEnabled(True)
        self.draw_process()

    def select_process(self):
        selected = self.memory_table.selectionModel().selectedRows()
        if selected:
            self.selected_process = self.memory_table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self.draw_process()

    def process_menu(self, position):
        row = self.memory_table.rowAt(position.y())
        if row < 0:
            return
        self.memory_table.selectRow(row)
        panel = self.process_panel
        key = panel.key
        menu = QMenu(self.memory_table)
        ordinary = menu.addAction('结束进程')
        ordinary.setEnabled(panel.stop_button.isEnabled())
        force = None
        if getattr(system, 'CAN_FORCE_STOP', False):
            force = menu.addAction('强制结束')
            force.setEnabled(panel.force_button.isEnabled())
        chosen = menu.exec(self.memory_table.viewport().mapToGlobal(position))
        # 菜单打开时仍在刷新列表；所选身份改变后不能结束另一个进程。
        if chosen is not None and panel.key == key:
            panel.stop_selected(force=chosen == force)

    def draw_process(self):
        process = next((process for process in self.memory_snapshot.processes if process.key == self.selected_process), None) if self.memory_snapshot else None
        self.process_panel.set_reading(process)

    def choose_directory(self):
        initial = self.root or ('/' if system.NAME == 'Linux' else Path.home())
        path = QFileDialog.getExistingDirectory(self, '选择任意可访问文件夹', str(initial))
        if path:
            self.root_edit.setText(path)
            self.start_directory(Path(path))

    def scan_path(self):
        text = self.root_edit.text().strip()
        if text:
            self.start_directory(Path(text).expanduser())
        else:
            self.set_operation('请先输入或浏览选择文件夹。')

    def change_scan_mode(self, index):
        self.target_inputs.setCurrentIndex(index)
        if self.scan_mode.currentData() == 'disk':
            self.refresh_disks()

    def refresh_disks(self):
        self.disk_menu.clear()
        try:
            roots = tuple(system.list_disks())
        except OSError as error:
            roots = ()
            self.drive_picker.setToolTip(str(error))
        available = {str(root) for root in roots}
        self.selected_disks.intersection_update(available)
        for root in roots:
            action = self.disk_menu.addAction(str(root))
            action.setCheckable(True)
            action.setChecked(str(root) in self.selected_disks)
            action.toggled.connect(lambda checked, root=str(root): self.select_disk(root, checked))
        self.update_disk_choice()

    def select_disk(self, root, checked):
        if checked:
            self.selected_disks.add(root)
        else:
            self.selected_disks.discard(root)
        self.update_disk_choice()

    def update_disk_choice(self):
        self.drive_picker.setText('、'.join(sorted(self.selected_disks)) or '选择磁盘（可多选）')
        self.disk_button.setEnabled(bool(self.selected_disks) and not self.cleanup_busy)

    def scan_disk(self):
        if self.selected_disks:
            self._start_scan_roots(tuple(Path(root) for root in sorted(self.selected_disks)), 'disk')

    def start_directory(self, root):
        self._start_scan_roots((root,), 'folder')

    def _start_scan_roots(self, roots, kind):
        if self.cleanup_busy:
            return
        roots = tuple(dict.fromkeys(Path(os.path.abspath(Path(root).expanduser())) for root in roots))
        if not roots:
            return
        if kind == 'folder' and any(root.parent == root for root in roots):
            QMessageBox.warning(self, '请选择具体文件夹',
                '整盘扫描请选择磁盘。' if system.HAS_DISK_SCAN else '可扫描 /tmp、/var、/mnt 等具体目录；不扫描整个根文件系统 /。')
            return
        self.stop_directory()
        self.roots, self.root, self.scan_kind = roots, roots[0], kind
        if kind == 'folder':
            self.root_edit.setText(str(roots[0]))
        self.current_snapshot = self.batch_analysis = None
        self.duplicate_key = self.duplicate_result = self.matrix_key = None
        self.cleanup_page.set_analysis(None)
        self.large_page.set_records(())
        self.duplicate_page.set_result(None)
        self.map_page.set_analysis(None)
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem('全部扫描目标', None)
        self.source_combo.blockSignals(False)
        self.file_mode.setCurrentIndex(0)
        self.file_path.setText('；'.join(map(str, roots)))
        self.set_operation('')
        for card in (self.file_count_card, self.file_size_card, self.target_card, self.disk_card):
            card.set_value('—')
        self.request_scan()

    def request_scan(self):
        if not self.roots or self.cleanup_busy or self.closing:
            return
        self.scan_cancel.set()
        self.scan_generation += 1
        generation, roots = self.scan_generation, self.roots
        cancel = threading.Event()
        self.scan_cancel = cancel
        self.scan_busy = True
        self.scan_started_at = time.monotonic()
        self.scan_progress = {}
        self.set_scan_status('正在扫描…')
        for card in (self.file_count_card, self.file_size_card, self.target_card, self.disk_card):
            card.set_value('—')
        self.progress.show()
        self.stop_button.setEnabled(True)
        self.refresh_button.setEnabled(False)
        self.file_timer.start()
        self.stop_content()

        def scan():
            def report(root, info):
                if generation == self.scan_generation:
                    self.scan_progress[str(root)] = info
            batch = scan_batch(roots, cancel, report)
            return None if cancel.is_set() else analyze_batch(batch, cancel)

        def failed(error):
            if generation == self.scan_generation:
                self.set_scan_status('扫描失败', str(error))

        def finished():
            if generation == self.scan_generation:
                self.scan_busy = False
                self.progress.hide()
                self.stop_button.setEnabled(False)
                self.refresh_button.setEnabled(True)
                self.file_timer.stop()
                self.update_selection()

        self.run_job(scan, lambda result: self.accept_scan(generation, result), failed, finished, pool=self.scan_pool)

    def accept_scan(self, generation, result):
        if self.closing or generation != self.scan_generation or result is None or self.cleanup_busy:
            return
        self.batch_analysis, self.current_snapshot = result, result.snapshot
        batch = result.snapshot
        self.duplicate_key = self.duplicate_result = self.matrix_key = None
        self.duplicate_page.set_result(None)
        selected = self.source_combo.currentData()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem('全部扫描目标', None)
        for root in batch.roots:
            self.source_combo.addItem(str(root), str(root))
        match = self.source_combo.findData(selected)
        self.source_combo.setCurrentIndex(max(0, match))
        self.source_combo.blockSignals(False)
        self.render_scan_metrics(batch)
        quality = '扫描完成' if batch.complete else '部分完成'
        details = (f'{quality}\n扫描文件：{len(batch.files):,}\n文件逻辑大小：{format_size(batch.total_bytes)}'
                   f'\n完成目标：{len(batch.snapshots)} / {len(self.roots)}\n跳过：{batch.skipped:,} 项'
                   f'\n耗时：{time.monotonic()-self.scan_started_at:.1f} 秒'
                   '\n可用空间已按磁盘去重。\n\n' + '\n'.join(batch.errors))
        self.set_scan_status(quality, details.strip())
        self.render_files()

    def render_scan_metrics(self, batch):
        self.file_count_card.set_value(f'{len(batch.files):,}', '个')
        self.file_size_card.set_value(format_size(batch.total_bytes))
        self.target_card.set_value(str(len(batch.snapshots)), f'/ {len(self.roots)}')
        self.disk_card.set_value(format_size(batch.free_bytes))

    def set_scan_status(self, text, details=''):
        self.scan_note.setText(text)
        self.scan_note.setToolTip(details)
        self.scan_details_button.setVisible(bool(details))

    def show_scan_details(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('扫描详情')
        dialog.resize(640, 420)
        layout = QVBoxLayout(dialog)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self.scan_note.toolTip() or self.scan_note.text())
        layout.addWidget(text)
        layout.addWidget(button('关闭', dialog.close))
        dialog.exec()
        dialog.deleteLater()

    def poll_scan(self):
        if self.scan_busy:
            info = tuple(self.scan_progress.items())
            total = sum(item.files for _, item in info)
            paths = ' · '.join(f'{root}：{item.stage} {item.files:,} 文件' for root, item in info)
            self.file_count_card.set_value(f'{total:,}', '个')
            completed = sum(item.stage == '扫描完成' for _, item in info)
            self.target_card.set_value(str(completed), f'/ {len(self.roots)}')
            self.set_scan_status('正在扫描…', f'已枚举 {total:,} 个文件 · {time.monotonic()-self.scan_started_at:.1f}秒\n{paths}')
        if hasattr(self, 'content_progress') and self.content_progress and self.duplicate_page.busy:
            self.duplicate_page.set_progress(*self.content_progress)

    def stop_directory(self):
        self.scan_generation += 1
        self.scan_cancel.set()
        self.scan_busy = False
        self.stop_content()
        if hasattr(self, 'file_timer'):
            self.file_timer.stop()
        if hasattr(self, 'stop_button'):
            self.stop_button.setEnabled(False)
            self.refresh_button.setEnabled(bool(self.roots))
            self.progress.hide()
            self.scan_note.setText('扫描已停止')
            if self.current_snapshot is not None and not self.closing:
                self.render_scan_metrics(self.current_snapshot)
        self.update_selection()

    def resume_scan(self):
        self.request_scan()

    def stop_content(self):
        self.content_generation += 1
        self.content_cancel.set()
        self.content_progress = None
        self.matrix_key = None
        if hasattr(self, 'duplicate_page'):
            was_busy = self.duplicate_page.busy
            self.duplicate_page.set_busy(False)
            if was_busy:
                self.duplicate_page.status.setText('内容比对已停止，不使用未完成的结果。')

    def clear_file_selection(self):
        for page in (self.cleanup_page, self.large_page, self.duplicate_page):
            page.clear_selection()

    def change_file_tool(self, index):
        self.stop_content()
        self.clear_file_selection()
        self.file_workspace.setCurrentIndex(index)
        self.render_files()

    def change_scope(self, *_):
        self.stop_content()
        self.clear_file_selection()
        self.duplicate_key = self.duplicate_result = None
        self.duplicate_page.set_result(None)
        self.render_files()

    def scoped_batch(self):
        if self.current_snapshot is None:
            return None
        source = self.source_combo.currentData()
        return self.current_snapshot if source is None else ScanBatch(tuple(
            snapshot for snapshot in self.current_snapshot.snapshots if str(snapshot.root) == source))

    def scoped_cleanup(self):
        analysis = self.batch_analysis.cleanup
        source = self.source_combo.currentData()
        if source is None:
            return analysis
        items = tuple(replace(item, records=[record for record in item.records if str(record.root) == source])
                      for item in analysis.items)
        items = tuple(item for item in items if item.records)
        return CleanupAnalysis(self.scoped_batch(), items, {record_key(record): item for item in items for record in item.records})

    def render_files(self, *_):
        mode = self.file_mode.currentIndex()
        self.file_workspace.setCurrentIndex(mode)
        if self.batch_analysis is None:
            self.update_selection()
            return
        if mode == 0:
            self.cleanup_page.set_analysis(self.scoped_cleanup())
        elif mode == 1:
            self.render_large()
        elif mode == 2:
            if self.duplicate_key != self.duplicate_view_key():
                self.duplicate_page.set_result(None)
        elif mode == 3:
            self.request_matrix()
        self.update_selection()

    def render_large(self):
        source = self.source_combo.currentData()
        records = (record for record in self.batch_analysis.large_records if source is None or str(record.root) == source)
        self.large_page.set_records(records, self.batch_analysis.cleanup.readonly_records())

    def view_key(self):
        return id(self.batch_analysis), self.source_combo.currentData()

    def duplicate_view_key(self):
        return self.view_key(), self.duplicate_page.minimum.currentData()

    def change_duplicate_options(self, *_):
        self.stop_content()
        self.duplicate_key = self.duplicate_result = None
        self.duplicate_page.set_result(None)
        self.update_selection()

    def request_matrix(self):
        key = self.view_key()
        if self.matrix_key == key:
            return
        self.matrix_key = key
        generation = self.content_generation
        cancel = threading.Event()
        self.content_cancel = cancel
        batch = self.scoped_batch()
        self.map_page.set_analysis(None)
        self.statusBar().showMessage('正在生成空间矩阵…')

        def accept(result):
            if not cancel.is_set() and generation == self.content_generation and key == self.view_key() and result is not None:
                self.map_page.set_analysis(result)
                self.statusBar().showMessage('空间矩阵就绪')
        def failed(error):
            if generation == self.content_generation:
                self.matrix_key = None
                self.statusBar().showMessage(f'空间矩阵未完成：{error}')
        self.run_job(lambda: build_matrix(batch, cancel), accept, failed, pool=self.content_pool)

    def start_duplicates(self):
        if self.current_snapshot is None or self.scan_busy or self.cleanup_busy:
            return
        self.stop_content()
        generation, key = self.content_generation, self.duplicate_view_key()
        cancel = threading.Event()
        self.content_cancel = cancel
        batch = self.scoped_batch()
        blocked = tuple(self.batch_analysis.cleanup.readonly_records())
        minimum = self.duplicate_page.minimum.currentData()
        self.duplicate_page.set_result(None)
        self.duplicate_page.set_busy(True)
        self.content_progress = None
        self.file_timer.start()
        def progress(stage, done, total):
            if generation == self.content_generation:
                self.content_progress = stage, done, total
        def accept(result):
            if generation == self.content_generation and not cancel.is_set() and key == self.duplicate_view_key():
                self.duplicate_result, self.duplicate_key = result, key
                self.duplicate_page.set_result(result, blocked)
        def failed(error):
            if generation == self.content_generation:
                self.duplicate_page.status.setText(f'内容比对未完成：{error}')
        def finished():
            if generation == self.content_generation:
                self.duplicate_page.set_busy(False)
                if not self.scan_busy:
                    self.file_timer.stop()
        self.run_job(lambda: find_duplicates(batch, cancel, progress, minimum_bytes=minimum), accept, failed, finished, pool=self.content_pool)

    def show_map_file(self, record):
        self.file_path.setText(str(record.path))
        self.file_path.setToolTip(str(record.path))

    def selected_records(self):
        index = self.file_mode.currentIndex()
        return (self.cleanup_page, self.large_page, self.duplicate_page)[index].selected_records() if index < 3 else []

    def set_operation(self, message):
        self.last_operation.setText(message)
        self.last_operation.setVisible(bool(message))

    def update_selection(self, *_):
        if not hasattr(self, 'preview_button'):
            return
        records = self.selected_records()
        self.selection_note.setText(f'已选 {len(records)} 个文件 · {format_size(sum(record.size for record in records))}'
                                    if self.file_mode.currentIndex() < 3 else '')
        self.preview_button.setEnabled(bool(records) and not self.cleanup_busy and not self.scan_busy)

    def set_cleanup_busy(self, busy):
        self.cleanup_busy = busy
        for widget in (self.file_mode, self.source_combo, self.file_workspace, self.scan_mode,
                       self.choose_button, self.path_scan_button, self.root_edit, self.drive_picker):
            widget.setEnabled(not busy)
        self.update_disk_choice()
        self.refresh_button.setEnabled(bool(self.roots) and not busy and not self.scan_busy)
        self.update_selection()

    def preview_selected(self):
        records = tuple(self.selected_records())
        if self.cleanup_busy or self.scan_busy or not records or self.current_snapshot is None:
            return
        classified = self.batch_analysis.cleanup.by_file
        notes = []
        for record in records:
            item = classified.get(record_key(record))
            if item is not None and item.risk == 'high':
                self.set_operation(f'不可直接清理：{item.name}。{item.reason}')
                return
            name = item.name if item else ('重复文件副本' if self.file_mode.currentIndex() == 2 else '手动选择的大文件')
            risk = item.risk if item and self.file_mode.currentIndex() == 0 else 'medium'
            reason = item.reason if item else '请确认文件用途与保留位置，清理后可能影响应用或个人数据。'
            notes.append((record_key(record), name, risk, reason))
        batch = self.current_snapshot
        groups = self.duplicate_result.groups if self.file_mode.currentIndex() == 2 and self.duplicate_result else ()
        readonly = tuple(self.batch_analysis.cleanup.readonly_records())
        self.set_cleanup_busy(True)
        def failed(error):
            self.set_cleanup_busy(False)
            self.set_operation(f'预览未通过核验：{error}')
        self.run_job(lambda: prepare_batch_cleanup(batch, records, risk_notes=tuple(notes), duplicate_groups=groups, readonly=readonly),
                     self.show_preview, failed, pool=self.content_pool)

    def show_preview(self, plan):
        dialog = PreviewDialog(plan, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.set_cleanup_busy(False)
            return
        def failed(error):
            self.set_cleanup_busy(False)
            self.set_operation(f'清理未完成：{error}')
        self.set_operation('正在移入回收站…')
        self.run_job(lambda: execute_batch_cleanup(plan), self.accept_cleanup, failed, pool=self.content_pool)

    def accept_cleanup(self, report):
        self.set_cleanup_busy(False)
        counts = {state: sum(result.state == state for result in report.results) for state in ('成功', '跳过', '失败')}
        text = f"已移入回收站 {counts['成功']} 项（{format_size(report.recycled_bytes)}）· 跳过 {counts['跳过']} · 失败 {counts['失败']}"
        self.set_operation(text)
        lines = [datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text,
                 '大小为文件逻辑大小，非立即释放的磁盘空间。']
        lines.extend(f'[{result.state}] {result.record.path}\n  {result.message}' for result in report.results)
        self.reports.append('\n'.join(lines))
        self.report_button.setEnabled(True)
        self.clear_file_selection()
        self.duplicate_result = self.duplicate_key = None
        self.duplicate_page.set_result(None)
        # 从已有快照移除已处理记录，再在后台整理索引；无需清理后再遍历整盘。
        excluded = {record_key(result.record) for result in report.results if result.state in ('成功', '跳过')}
        old = self.current_snapshot
        snapshots = tuple(replace(snapshot, files={key: record for key, record in snapshot.files.items()
                          if record_key(record) not in excluded}) for snapshot in old.snapshots)
        batch = ScanBatch(snapshots, old.errors, old.elapsed)
        generation = self.scan_generation
        self.set_cleanup_busy(True)
        def updated(result):
            if generation == self.scan_generation and result is not None:
                self.set_cleanup_busy(False)
                self.accept_scan(generation, result)
                self.scan_note.setText('清理结果已更新')
        def failed(error):
            self.set_cleanup_busy(False)
            self.set_operation(text + f' · 统计更新失败：{error}，请重新扫描。')
        self.run_job(lambda: analyze_batch(batch), updated, failed, pool=self.content_pool)

    def show_report(self):
        if not self.reports:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('清理报告')
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
        self.selected_process = (self.demo_process.pid, system.process_created(psutil.Process(self.demo_process.pid)))
        self.demo_button.setText('停止演示')
        self.start_directory(root)
        self.tabs.setCurrentIndex(1)
        self.set_operation('演示目录已创建')

    def closeEvent(self, event):
        self.closing = True
        self.ai_settings.shutdown()
        self.file_ai_dialog.shutdown()
        self.process_panel.shutdown()
        self.process_icons.shutdown()
        self.apps_page.shutdown()
        self.memory_timer.stop()
        self.stop_directory()
        if self.demo_process and self.demo_process.poll() is None:
            self.demo_process.terminate()
        event.accept()
