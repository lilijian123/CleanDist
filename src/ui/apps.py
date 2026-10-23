"""应用扫描页：只读展示三类来源，不提供启动、卸载或删除操作。"""
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QLabel,
    QMenu, QStackedWidget, QStyle, QTableView, QVBoxLayout, QWidget,
)

from ..platforms import system
from .widgets import CachedDelegate, FocusEdit, Metric, button, enable_pixel_scroll, label, number_font


class AppModel(QAbstractTableModel):
    HEADERS = ('应用名称', '类型', '版本', '发布者', '盘符', '位置')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.apps = ()
        self.icons = {}
        self.icon_data = {}
        self.default_icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.number_font = number_font()

    def set_apps(self, apps, source='', query=''):
        query = query.strip().casefold()
        if apps is not self.apps:
            self.apps = apps
            self.icons.clear()
            self.icon_data.clear()
        self.beginResetModel()
        self.rows = tuple(app for app in apps if (not source or app.source == source)
                          and (not query or query in ' '.join((app.name, app.version, app.publisher,
                                                              str(app.path or ''))).casefold()))
        self.endResetModel()

    def apply_icons(self, images):
        self.icon_data.update(images)
        rows = [row for row, app in enumerate(self.rows) if (app.icon_path, app.icon_index) in images]
        if rows:
            self.dataChanged.emit(self.index(rows[0], 0), self.index(rows[-1], 0),
                                  [Qt.ItemDataRole.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        app = self.rows[index.row()]
        if role == Qt.ItemDataRole.FontRole and index.column() in (2, 4):
            return self.number_font
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            data = app.icon or self.icon_data.get((app.icon_path, app.icon_index), b'')
            if data not in self.icons:
                pixmap = QPixmap()
                self.icons[data] = QIcon(pixmap) if data and pixmap.loadFromData(data) else self.default_icon
            return self.icons[data]
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(app.path) if app.path else '未登记位置'
        if role == Qt.ItemDataRole.DisplayRole:
            return (app.name, app.source, app.version or '—', app.publisher or '—',
                    app.drive, str(app.path) if app.path else '未定位')[index.column()]


class AppPage(QWidget):
    def __init__(self, run_job):
        super().__init__()
        self.run_job = run_job
        self.apps = ()
        self.busy = False
        self.cancel = threading.Event()
        self.progress = None
        self.started = 0
        self.closed = False
        self.icon_busy = False
        self.icon_cancel = threading.Event()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        self.drive_button = button('全部盘符', lambda: None)
        menu = QMenu(self.drive_button)
        self.all_action = menu.addAction('全部盘符')
        self.all_action.setCheckable(True)
        self.all_action.setChecked(True)
        self.all_action.triggered.connect(self.select_all)
        menu.addSeparator()
        self.drive_actions = []
        try:
            roots = system.list_disks()
        except OSError:
            roots = ()
        for root in roots:
            action = menu.addAction(str(root))
            action.setData(str(root))
            action.setCheckable(True)
            action.setChecked(True)
            action.triggered.connect(self.update_drives)
            self.drive_actions.append(action)
        self.drive_button.setMenu(menu)
        controls.addWidget(self.drive_button)
        self.scan_button = button('开始扫描', self.start_scan, True)
        self.stop_button = button('停止', self.stop_scan)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.scan_button)
        controls.addWidget(self.stop_button)
        controls.addStretch()
        layout.addLayout(controls)
        cards = QHBoxLayout()
        self.cards = {source: Metric(source) for source in ('桌面应用', '商店应用', '便携候选', '未定位')}
        for card in self.cards.values():
            cards.addWidget(card)
        layout.addLayout(cards)
        self.status = label('尚未扫描', 'muted')
        layout.addWidget(self.status)
        filters = QHBoxLayout()
        self.source = QComboBox()
        self.source.addItem('全部类型', '')
        for source in ('桌面应用', '商店应用', '便携候选'):
            self.source.addItem(source, source)
        self.search = FocusEdit()
        self.search.setPlaceholderText('搜索名称、发布者或位置')
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.source)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.model = AppModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegate(CachedDelegate(self.table))
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(24, 24))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        for column, width in ((0, 220), (1, 100), (2, 140), (3, 170), (4, 75)):
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        enable_pixel_scroll(self.table)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.empty = QLabel('点击“开始扫描”')
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName('section')
        self.stack.addWidget(self.empty)
        self.stack.setCurrentIndex(1)
        layout.addWidget(self.stack, 1)
        self.count = label('', 'muted')
        layout.addWidget(self.count)
        self.source.currentIndexChanged.connect(self.render)
        self.search.textChanged.connect(self.render)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(200)
        self.icon_timer = QTimer(self)
        self.icon_timer.setSingleShot(True)
        self.icon_timer.setInterval(35)
        self.icon_timer.timeout.connect(self.load_icons)
        self.table.verticalScrollBar().valueChanged.connect(self.queue_icons)
        self.table.viewport().installEventFilter(self)
        self.update_drives()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize):
            self.queue_icons()
        return super().eventFilter(watched, event)

    def queue_icons(self, *_):
        if not self.closed:
            self.icon_timer.start()

    def load_icons(self):
        if self.closed or self.busy or self.icon_busy or not self.table.isVisible() or not self.model.rows:
            return
        top = max(0, self.table.rowAt(0))
        bottom = self.table.rowAt(self.table.viewport().height() - 1)
        bottom = bottom if bottom >= 0 else len(self.model.rows) - 1
        # 当前可见行优先，上下各预取 12 行；单批最多 32 个，失败也缓存。
        nearby = (list(range(top, bottom + 1)) + list(range(max(0, top - 12), top))
                  + list(range(bottom + 1, min(len(self.model.rows), bottom + 13))))
        missing = {}
        for row in nearby:
            app = self.model.rows[row]
            key = (app.icon_path, app.icon_index)
            if app.icon_path and not app.icon and key not in self.model.icon_data:
                missing[key] = None
        requests = tuple(missing)[:32]
        if not requests:
            return
        cancel = self.icon_cancel
        self.icon_busy = True

        def accept(images):
            if not self.closed and not cancel.is_set():
                self.model.apply_icons({key: images.get(key, b'') for key in requests})

        def finished():
            self.icon_busy = False
            self.queue_icons()

        self.run_job(lambda: system.load_app_icons(requests, cancel), accept,
                     lambda error: accept({}), finished)

    def select_all(self, checked):
        for action in self.drive_actions:
            action.setChecked(checked)
        self.update_drives()

    def update_drives(self, *_):
        selected = [action.data() for action in self.drive_actions if action.isChecked()]
        all_selected = bool(selected) and len(selected) == len(self.drive_actions)
        self.all_action.setChecked(all_selected)
        self.drive_button.setText('全部盘符' if all_selected else '、'.join(selected) or '选择盘符')
        self.scan_button.setEnabled(bool(selected) and not self.busy)

    def selected_roots(self):
        if self.all_action.isChecked():
            return None
        from pathlib import Path
        return tuple(Path(action.data()) for action in self.drive_actions if action.isChecked())

    def start_scan(self):
        if self.busy or not any(action.isChecked() for action in self.drive_actions):
            return
        roots = self.selected_roots()
        self.icon_cancel.set()
        self.icon_cancel = threading.Event()
        self.busy = True
        self.cancel = threading.Event()
        cancel = self.cancel
        self.progress = None
        self.started = time.monotonic()
        self.apps = ()
        self.source.setCurrentIndex(0)
        self.search.clear()
        self.render()
        for card in self.cards.values():
            card.value.setText('—')
        self.empty.setText('正在扫描应用…')
        self.status.setText('正在扫描…')
        self.status.setToolTip('')
        self.scan_button.setEnabled(False)
        self.drive_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        def report(value):
            # 工作线程仅更新不可变进度，Qt 控件由页面定时器在主线程更新。
            self.progress = value

        def failed(error):
            self.status.setText(f'扫描失败：{error}')
            self.empty.setText('扫描未完成')

        def finished():
            self.busy = False
            self.drive_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self.update_drives()
            self.queue_icons()

        self.run_job(lambda: system.scan_apps(roots, cancel, report), self.accept, failed, finished)

    def stop_scan(self):
        self.cancel.set()
        if self.busy:
            self.status.setText('正在停止…')

    def poll(self):
        if self.busy and self.progress is not None and not self.cancel.is_set():
            elapsed = time.monotonic() - self.started
            self.status.setText(f'{self.progress.stage} · 已发现 {self.progress.found:,} 项 · {elapsed:.1f}秒')

    def accept(self, result):
        self.apps = result.apps
        for source, card in self.cards.items():
            count = sum(app.path is None for app in self.apps) if source == '未定位' else sum(app.source == source for app in self.apps)
            card.value.setText(f'{count:,}')
        self.render()
        self.queue_icons()
        state = '已停止' if result.cancelled else '部分结果' if result.errors or result.skipped else '扫描完成'
        detail = f' · 跳过 {result.skipped:,} 项' if result.skipped else ''
        self.status.setText(f'{state} · {len(self.apps):,} 项{detail} · {result.elapsed:.1f}秒')
        self.status.setToolTip('\n'.join(result.errors))

    def render(self, *_):
        self.model.set_apps(self.apps, self.source.currentData(), self.search.text())
        self.stack.setCurrentIndex(0 if self.model.rowCount() else 1)
        self.empty.setText('暂无匹配应用')
        self.count.setText(f'显示 {self.model.rowCount():,} / {len(self.apps):,} 项' if self.apps else '')
        if hasattr(self, 'icon_timer'):
            self.queue_icons()

    def shutdown(self):
        self.closed = True
        self.cancel.set()
        self.icon_cancel.set()
        self.timer.stop()
        self.icon_timer.stop()
