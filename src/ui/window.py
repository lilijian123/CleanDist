"""主窗口与资源监测交互。"""
from collections import deque
from datetime import datetime
from pathlib import Path
import threading
import time

import psutil
from PySide6.QtCore import QModelIndex, QSettings, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar, QTabWidget, QTableWidgetItem,
    QScrollArea, QSplitter, QStackedWidget, QTextEdit, QTreeView, QVBoxLayout, QWidget,
)
from ..core.files import execute_cleanup, prepare_cleanup, scan_directory, validate_plain_path
from ..core.analysis import analyze_files
from ..platforms import system
from ..core.memory import MemoryHistory, sample_memory
from .. import APP_NAME
from .dialogs import PreviewDialog
from .apps import AppPage
from .files import FileModel, SizeDelegate
from .processes import ProcessIcons, ProcessPanel
from .storage import StorageDialog
from .treemap import MapPage
from .theme import STYLE, LIGHT_STYLE, color
from .widgets import BrandMark, CachedDelegate, FocusEdit, Metric, NumberItem, SettingsButton, ThemeChoice, TrendChart, button, enable_pixel_scroll, label, format_size, number_font, table
from .workers import Job
from .ai import AiSettingsPage


class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
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
        self.scan_kind = 'folder'
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
        self.file_timer.timeout.connect(self.poll_directory)
        self.file_timer.setInterval(1000)
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
        self.file_model.set_fonts(font, 700 if light else 660)
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
        self.file_tree.viewport().update()
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
        details = QScrollArea()
        details.setWidgetResizable(True)
        details.setFrameShape(QFrame.Shape.NoFrame)
        details.setMinimumWidth(380)
        details.setWidget(self.process_panel)
        split.addWidget(details)
        split.setChildrenCollapsible(False)
        split.setSizes([650, 520])
        layout.addWidget(split, 1)
        self.tabs.addTab(page, '01  内存监测')

    def _build_files_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        self.scan_mode = QComboBox()
        self.scan_mode.addItem('文件夹扫描', 'folder')
        if system.HAS_DISK_SCAN:
            self.scan_mode.addItem('磁盘扫描', 'disk')
        self.scan_mode.setVisible(system.HAS_DISK_SCAN)
        controls.addWidget(self.scan_mode)
        self.target_inputs = QStackedWidget()
        folder_controls = QWidget()
        folder_layout = QHBoxLayout(folder_controls)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        self.root_edit = FocusEdit()
        self.root_edit.setReadOnly(True)
        self.root_edit.setPlaceholderText('选择文件夹')
        folder_layout.addWidget(self.root_edit, 1)
        self.choose_button = button('选择文件夹', self.choose_directory, True)
        folder_layout.addWidget(self.choose_button)
        self.target_inputs.addWidget(folder_controls)
        disk_controls = QWidget()
        disk_layout = QHBoxLayout(disk_controls)
        disk_layout.setContentsMargins(0, 0, 0, 0)
        self.drive_combo = QComboBox()
        self.drive_combo.setPlaceholderText('选择要扫描的盘符')
        self.drive_combo.setEnabled(False)
        disk_layout.addWidget(self.drive_combo, 1)
        self.disk_button = button('扫描磁盘', self.scan_disk, True)
        self.disk_button.setEnabled(False)
        disk_layout.addWidget(self.disk_button)
        self.target_inputs.addWidget(disk_controls)
        controls.addWidget(self.target_inputs, 1)
        self.scan_mode.currentIndexChanged.connect(self.change_scan_mode)
        self.refresh_button = button('重新扫描', self.resume_scan)
        self.stop_button = button('停止监测', self.stop_directory)
        self.refresh_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        for item in (self.refresh_button, self.stop_button):
            controls.addWidget(item)
        layout.addLayout(controls)
        cards = QHBoxLayout()
        # 文件大小为逻辑大小；分区可用空间由系统统计，包含其他应用活动的影响。
        self.file_count_card = Metric('已扫描文件')
        self.file_size_card = Metric('文件逻辑大小')
        self.file_delta_card = Metric('本轮增量')
        self.disk_card = Metric('磁盘可用空间')
        for card in (self.file_count_card, self.file_size_card, self.file_delta_card, self.disk_card):
            cards.addWidget(card)
        layout.addLayout(cards)
        self.scan_note = label('尚未开始扫描', 'muted')
        self.scan_note.setWordWrap(True)
        layout.addWidget(self.scan_note)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        result_header = QHBoxLayout()
        self.results_title = label('扫描结果', 'section')
        result_header.addWidget(self.results_title)
        self.file_path = FocusEdit()
        self.file_path.setObjectName('filePath')
        self.file_path.setReadOnly(True)
        self.file_path.setPlaceholderText('完整路径')
        result_header.addWidget(self.file_path, 1)
        self.usage_button = button('空间分析', lambda: self.open_storage('usage'))
        self.duplicate_button = button('识别重复', lambda: self.open_storage('duplicates'))
        self.usage_button.setEnabled(False)
        self.duplicate_button.setEnabled(False)
        result_header.addWidget(self.usage_button)
        result_header.addWidget(self.duplicate_button)
        layout.addLayout(result_header)
        filters = QHBoxLayout()
        self.file_mode = QListWidget()
        self.file_mode.setObjectName('fileModes')
        self.file_mode.setFixedWidth(152)
        self.file_mode.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for text, mode in [('目录层级', 'tree'), ('大文件', 'large'), ('最近增长', 'growth'), ('空间占用矩阵', 'map')]:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, mode)
            self.file_mode.addItem(item)
        self.file_mode.setCurrentRow(0)
        self.file_order = QComboBox()
        self.file_order.addItem('按占用排序', 'size')
        self.file_order.addItem('按增长排序', 'growth')
        self.file_minimum = QComboBox()
        for megabytes in (1, 10, 50, 100, 500):
            self.file_minimum.addItem(f'≥ {megabytes} MB', megabytes * 1024 * 1024)
        self.file_minimum.setCurrentIndex(2)
        self.file_minimum.setEnabled(False)
        self.file_minimum.hide()
        self.file_search = FocusEdit()
        self.file_search.setPlaceholderText('搜索文件名或路径，按回车')
        self.file_search.setClearButtonEnabled(True)
        for widget in (self.file_order, self.file_minimum, self.file_search):
            filters.addWidget(widget, 1 if widget is self.file_search else 0)
        layout.addLayout(filters)
        self.file_tree = QTreeView()
        self.file_model = FileModel(self)
        self.file_tree.setModel(self.file_model)
        self.file_tree.setItemDelegate(CachedDelegate(self.file_tree))
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setAnimated(True)
        enable_pixel_scroll(self.file_tree)
        self.file_tree.setIndentation(22)
        self.file_tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_tree.header().setStretchLastSection(False)
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in [(1, 190), (2, 75), (3, 115), (4, 90), (5, 145)]:
            self.file_tree.setColumnWidth(column, width)
        self.file_tree.setItemDelegateForColumn(1, SizeDelegate(self.file_tree))
        self.file_tree.selectionModel().currentChanged.connect(self.show_file_path)
        self.file_model.dataChanged.connect(self.update_selection)
        self.file_mode.currentRowChanged.connect(self.render_files)
        self.file_order.currentIndexChanged.connect(self.render_files)
        self.file_minimum.currentIndexChanged.connect(self.render_files)
        self.file_search.returnPressed.connect(self.render_files)
        self.file_search.textChanged.connect(lambda text: self.render_files() if not text else None)
        self.results_stack = QStackedWidget()
        self.results_stack.addWidget(self.file_tree)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch()
        self.empty_title = label('选择扫描目标', 'section')
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addStretch()
        self.results_stack.addWidget(empty_page)
        self.map_page = MapPage()
        self.map_page.map.file_clicked.connect(self.show_map_file)
        self.results_stack.addWidget(self.map_page)
        self.results_stack.setCurrentIndex(1)
        result_body = QHBoxLayout()
        result_body.setSpacing(14)
        result_body.addWidget(self.file_mode)
        result_content = QVBoxLayout()
        result_content.addWidget(self.results_stack, 1)
        details = QHBoxLayout()
        self.file_view_note = label('', 'muted')
        details.addWidget(self.file_view_note, 1)
        self.more_button = button('加载更多', self.load_more_files)
        self.more_button.setToolTip('加载当前层的下一批文件')
        self.more_button.setEnabled(False)
        details.addWidget(self.more_button)
        result_content.addLayout(details)
        result_body.addLayout(result_content, 1)
        layout.addLayout(result_body, 1)
        self.selection_note = label('未选择文件', 'muted')
        self.last_operation = label('', 'muted')
        self.last_operation.setWordWrap(True)
        self.last_operation.hide()
        layout.addWidget(self.last_operation)
        actions = QHBoxLayout()
        actions.addWidget(self.selection_note, 1)
        self.report_button = button('查看清理报告', self.show_report)
        self.report_button.setEnabled(False)
        actions.addWidget(self.report_button)
        self.preview_button = button('预览选中文件', self.preview_selected, True)
        # 清理仅移入回收站，预览和执行前分别核验文件身份。
        self.preview_button.setEnabled(False)
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        self.tabs.addTab(page, '02  文件增长与清理')

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
        self.statusBar().showMessage(f'更新 {updated_at} · {"监测中" if self.monitoring else "就绪"}')
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
        path = QFileDialog.getExistingDirectory(self, '选择文件夹', str(self.root or Path.home()))
        if path:
            self.start_directory(Path(path))

    def change_scan_mode(self, index):
        self.target_inputs.setCurrentIndex(index)
        if self.scan_mode.currentData() == 'disk':
            selected = self.drive_combo.currentData()
            self.drive_combo.clear()
            try:
                for root in system.list_disks():
                    self.drive_combo.addItem(str(root), str(root))
                self.drive_combo.setToolTip('扫描整个磁盘')
            except OSError as error:
                self.drive_combo.setToolTip(f'无法读取磁盘列表：{error}')
            if selected is not None:
                match = self.drive_combo.findData(selected)
                if match >= 0:
                    self.drive_combo.setCurrentIndex(match)
            if self.drive_combo.currentIndex() < 0 and self.drive_combo.count():
                self.drive_combo.setCurrentIndex(0)
            available = self.drive_combo.count() > 0 and not self.cleanup_busy
            self.drive_combo.setEnabled(available)
            self.disk_button.setEnabled(available)
            if not self.drive_combo.count():
                self.drive_combo.setPlaceholderText('当前没有可选磁盘')

    def scan_disk(self):
        root = self.drive_combo.currentData()
        if root is not None:
            self._start_scan(Path(root), 'disk')

    def start_directory(self, root):
        self._start_scan(root, 'folder')

    def _start_scan(self, root, kind):
        if self.cleanup_busy:
            return
        try:
            root = validate_plain_path(root)
            if not root.is_dir():
                raise ValueError('请选择目录')
            if kind == 'folder' and root.parent == root:
                raise ValueError('整盘扫描请使用“磁盘扫描”，并选择对应盘符。' if system.HAS_DISK_SCAN
                                 else '请选择具体文件夹，例如 /home；不支持扫描整个根文件系统。')
            if kind == 'disk' and root.parent != root:
                raise ValueError('请选择磁盘根目录')
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '无法监测目录', str(error))
            return
        for dialog in self.findChildren(StorageDialog):
            dialog.close()
        self.stop_directory()
        self.root = root
        self.scan_kind = kind
        self.scan_mode.setCurrentIndex(1 if kind == 'disk' else 0)
        if kind == 'folder':
            self.root_edit.setText(str(root))
        self.results_title.setText('磁盘扫描结果' if kind == 'disk' else '文件夹扫描结果')
        self.file_path.setText(str(root))
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
        self.file_mode.setCurrentRow(0)
        self.file_order.setCurrentIndex(0)
        self.file_minimum.setCurrentIndex(2)
        self.file_search.clear()
        for widget in filters:
            widget.blockSignals(False)
        self.render_files()
        self.file_view_note.setText('')
        self.empty_title.setText('正在扫描磁盘…' if kind == 'disk' else '正在扫描文件夹…')
        self.set_operation('')
        self.scan_note.setToolTip('')
        self.scan_note.setText('正在切换扫描目标…' if self.scan_busy else '正在扫描…')
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
            self.scan_note.setText('监测已停止' if self.current_snapshot
                                   else '扫描已停止')
            if self.current_snapshot is None:
                self.file_view_note.setText('')
                self.empty_title.setText('扫描已停止')
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
                self.scan_note.setText(f'{info.stage} · {info.files:,} 文件 / {info.directories:,} 目录 · {elapsed:.1f}秒')
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
        self.scan_note.setText('正在扫描…')
        if self.current_snapshot is None:
            self.results_stack.setCurrentIndex(1)
            self.empty_title.setText('正在扫描磁盘…' if self.scan_kind == 'disk' else '正在扫描文件夹…')

        def failed(error):
            if generation == self.scan_generation:
                state = '保留上次结果' if self.current_snapshot else '本次尚无结果'
                self.scan_note.setText(f'本轮扫描失败，{state}：{error}')
                if self.current_snapshot is None:
                    self.file_view_note.setText('扫描失败，请检查目录访问权限或重试。')
                    self.empty_title.setText('扫描未完成')

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
        if self.current_snapshot is None and self.scan_kind == 'disk':
            self.file_mode.blockSignals(True)
            self.file_mode.setCurrentRow(3)
            self.file_mode.blockSignals(False)
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
        # 扫描引擎与自动重扫策略由平台层和 poll_directory 管理，不在界面重复解释。
        total = time.monotonic() - self.scan_started_at
        note = f'{quality} · 耗时 {total:.1f}秒 · {updated_at}'
        self.scan_note.setText(note)
        self.scan_note.setToolTip('\n'.join(snapshot.errors))
        self.update_selection()

    def render_files(self, *_):
        mode = self.file_mode.currentItem().data(Qt.ItemDataRole.UserRole)
        is_map = mode == 'map'
        self.file_search.setEnabled(not is_map)
        self.file_minimum.setEnabled(mode == 'large')
        self.file_minimum.setVisible(mode == 'large')
        self.file_order.setEnabled(mode == 'tree' and not self.file_search.text().strip())
        view_key = (self.root, mode, self.file_minimum.currentData(), self.file_search.text(), self.file_order.currentData())
        preserve = view_key == self.file_view_key
        scroll = self.file_tree.verticalScrollBar().value() if preserve else 0
        expanded = []
        for key, node in self.file_model.nodes.items():
            index = node.index()
            if preserve and self.file_tree.isExpanded(index):
                expanded.append((key, node.rowCount()))
        root_rows = self.file_model.rowCount() if preserve else 0
        self.file_model.set_analysis(self.file_analysis, 'tree' if is_map else mode, self.file_minimum.currentData(),
                                     '' if is_map else self.file_search.text(), self.file_order.currentData())
        while self.file_model.rowCount() < root_rows and self.file_model.has_more():
            self.file_model.load_more()
        for key, count in sorted(expanded, key=lambda item: item[0].count('/')):
            node = self.file_model.nodes.get(key)
            if node:
                index = node.index()
                while node.rowCount() < count and self.file_model.has_more(index):
                    self.file_model.load_more(index)
                self.file_tree.expand(index)
        self.file_view_key = view_key
        self.file_tree.verticalScrollBar().setValue(scroll)
        self.results_stack.setCurrentIndex(0 if self.file_model.rowCount() else 1)
        self.map_page.set_analysis(self.file_analysis)
        if is_map and self.file_analysis is not None:
            self.results_stack.setCurrentWidget(self.map_page)
        if self.file_analysis is not None and not self.file_model.rowCount():
            self.empty_title.setText('暂无匹配结果')
        self.file_path.setText(str(self.root) if self.root else '')
        self.file_path.setToolTip(str(self.root) if self.root else '')
        scope = '结果' if self.file_model.flat else '顶层'
        note = f'{scope} {self.file_model.rowCount()} / {len(self.file_model.entries)} 项' if self.file_analysis else ''
        if self.file_model.flat and len(self.file_model.entries) == 500:
            note += ' · 前500项'
        self.file_view_note.setText('矩形面积表示本次已扫描文件的逻辑大小' if is_map and self.file_analysis else note)
        self.more_button.setEnabled(self.file_analysis is not None and not is_map)
        self.more_button.setVisible(not is_map)
        self.usage_button.setEnabled(self.current_snapshot is not None and not self.cleanup_busy)
        self.duplicate_button.setEnabled(self.current_snapshot is not None and not self.cleanup_busy)
        self.update_selection()

    def show_map_file(self, relative):
        if self.root is not None:
            path = str(self.root / relative)
            self.file_path.setText(path)
            self.file_path.setToolTip(path)

    def open_storage(self, mode):
        if self.current_snapshot is None or self.cleanup_busy or self.closing:
            return
        previous = getattr(self, 'storage_dialog', None)
        if previous is not None and not previous.closed:
            previous.close()
        self.storage_dialog = StorageDialog(self.current_snapshot, mode, self.run_job, self)
        self.storage_dialog.show()
        self.storage_dialog.start()

    def show_file_path(self, index, *_):
        if index.isValid():
            entry = self.file_model.itemFromIndex(index.siblingAtColumn(0)).entry
            relative = entry.relative
            if relative.startswith('\0small/'):
                relative = relative[len('\0small/'):]
            path = str(self.root / relative)
            self.file_path.setText(path)
            self.file_path.setToolTip(path)

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

    def set_operation(self, message):
        self.last_operation.setText(message)
        self.last_operation.setVisible(bool(message))

    def update_selection(self, *_):
        records = self.selected_records()
        self.selection_note.setText(f'已选 {len(records)} 项 · {format_size(sum(record.size for record in records))}')
        self.preview_button.setEnabled(bool(records) and not self.cleanup_busy)

    def set_cleanup_busy(self, busy):
        self.cleanup_busy = busy
        self.choose_button.setEnabled(not busy)
        self.scan_mode.setEnabled(not busy)
        self.drive_combo.setEnabled(not busy and self.drive_combo.count() > 0)
        self.disk_button.setEnabled(not busy and self.drive_combo.count() > 0)
        self.refresh_button.setEnabled(bool(self.root) and not busy)
        self.stop_button.setEnabled(self.monitoring and not busy)
        self.usage_button.setEnabled(self.current_snapshot is not None and not busy)
        self.duplicate_button.setEnabled(self.current_snapshot is not None and not busy)
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
            self.set_operation(f'清理未完成：{error}')

        self.set_operation('正在移入回收站…')
        self.run_job(lambda: execute_cleanup(plan), self.accept_cleanup, failed)

    def accept_cleanup(self, report):
        self.set_cleanup_busy(False)
        counts = {state: sum(result.state == state for result in report.results) for state in ('成功', '跳过', '失败')}
        text = f"已移入回收站 {counts['成功']} 项（{format_size(report.recycled_bytes)}）· 跳过 {counts['跳过']} · 失败 {counts['失败']}"
        self.set_operation(text)
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
        self.process_panel.shutdown()
        self.process_icons.shutdown()
        for dialog in self.findChildren(StorageDialog):
            dialog.close()
        self.apps_page.shutdown()
        self.memory_timer.stop()
        self.stop_directory()
        if self.demo_process and self.demo_process.poll() is None:
            self.demo_process.terminate()
        event.accept()
