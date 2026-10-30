"""大文件手动选择与重复副本核对页面。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView,
                              QProgressBar, QTreeView, QVBoxLayout, QWidget)

from .records import RecordGroup, RecordModel, record_key
from .widgets import CachedDelegate, FocusEdit, button, enable_pixel_scroll, format_size, label


class RecordTree(QTreeView):
    aiRequested = Signal(object)

    def __init__(self):
        super().__init__()
        self.clicked.connect(self.request_ai)

    def request_ai(self, index):
        if index.isValid() and index.column() == 6:
            self.aiRequested.emit(index)

    def keyPressEvent(self, event):
        if self.currentIndex().column() == 6 and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.request_ai(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)


def record_tree(model):
    tree = RecordTree()
    tree.setModel(model)
    tree.setUniformRowHeights(True)
    tree.setAnimated(False)
    tree.setAlternatingRowColors(True)
    tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    tree.setItemDelegate(CachedDelegate(tree))
    enable_pixel_scroll(tree)
    tree.header().setStretchLastSection(False)
    tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column, width in ((1, 110), (2, 70), (3, 140), (4, 165), (5, 155)):
        tree.setColumnWidth(column, width)
    tree.setColumnHidden(5, True)
    tree.header().setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
    tree.setColumnWidth(6, 48)
    return tree


class LargeFilesPage(QWidget):
    checkedChanged = Signal()
    filterChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records, self.blocked = (), ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = label('', 'muted')
        self.summary.setWordWrap(True)
        self.summary.hide()
        layout.addWidget(self.summary)
        filters = QHBoxLayout()
        self.minimum = QComboBox()
        for size in (1, 10, 50, 100, 500):
            self.minimum.addItem(f'至少 {size} MiB', size)
        self.minimum.setCurrentIndex(2)
        filters.addWidget(self.minimum)
        self.search = FocusEdit()
        self.search.setPlaceholderText('搜索文件路径')
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.clear_button = button('清空选择', self.clear_selection)
        filters.addWidget(self.clear_button)
        layout.addLayout(filters)
        self.model = RecordModel(self)
        self.tree = record_tree(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnHidden(2, True)
        layout.addWidget(self.tree, 1)
        self.minimum.currentIndexChanged.connect(self.render)
        self.search.returnPressed.connect(self.render)
        self.search.textChanged.connect(lambda text: self.render() if not text else None)
        self.model.checkedChanged.connect(self._selection_changed)
        self._selection_changed()

    def set_records(self, records, blocked=()):
        self.records = tuple(sorted(records, key=lambda record: (-record.size, str(record.path))))
        self.blocked = tuple(blocked)
        self.render()

    def render(self, *_):
        minimum, query = self.minimum.currentData() * 1024**2, self.search.text().strip().casefold()
        records = [record for record in self.records if record.size >= minimum
                   and (not query or query in str(record.path).casefold())]
        self.model.set_groups([RecordGroup('large', '大文件', records,
                              reason='大文件不一定无用；请确认用途后手动选择。')],
                              flat=True, blocked=self.blocked, allowed=self.records)
        self.summary.setText(f'{len(records):,} 个文件 · {format_size(sum(record.size for record in records))}')
        self.summary.setVisible(bool(self.records))
        self.filterChanged.emit()

    def _selection_changed(self):
        self.clear_button.setEnabled(bool(self.model.checked))
        self.checkedChanged.emit()

    def selected_records(self):
        return self.model.selected_records()

    def clear_selection(self):
        self.model.clear_selection()


class DuplicatePage(QWidget):
    startRequested = Signal()
    stopRequested = Signal()
    checkedChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = None
        self.busy = False
        self._updating = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.minimum = QComboBox()
        for title, size in (('500 KB 以上', 500*1024), ('1 MB 以上', 1024**2), ('50 MB 以上', 50*1024**2), ('200 MB 以上', 200*1024**2)):
            self.minimum.addItem(title, size)
        self.minimum.setToolTip('先按大小过滤，再读取内容比对；1 KB = 1024 B，1 MB = 1024 KB。')
        actions.addWidget(self.minimum)
        self.start_button = button('开始内容比对', self.startRequested.emit)
        self.stop_button = button('停止比对', self.stopRequested.emit)
        self.select_button = button('选择副本（每组保留一份）', self.select_copies)
        self.clear_button = button('清空选择', self.clear_selection)
        for item in (self.start_button, self.stop_button, self.select_button, self.clear_button):
            actions.addWidget(item)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = label('', 'muted')
        self.status.setWordWrap(True)
        self.status.hide()
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.model = RecordModel(self)
        self.tree = record_tree(self.model)
        layout.addWidget(self.tree, 1)
        self.model.checkedChanged.connect(self._selection_changed)
        self.set_busy(False)
        self._selection_changed()

    def set_result(self, result, blocked=()):
        self.result = result
        groups = []
        if result is not None:
            for number, records in enumerate(result.groups):
                if len(records) < 2:
                    continue
                groups.append(RecordGroup(f'duplicate/{number}', f'重复组 {number + 1:03} · {records[0].path.name}', records,
                    reason='已确认内容相同，但用途可能不同；每组至少保留一份。', keep_one=True))
        self._updating = True
        self.model.clear_selection()
        self.model.set_groups(groups, blocked=blocked)
        self._updating = False
        self._selection_changed()
        if result is None:
            self.status.clear()
        else:
            self.status.setText(f'{len(groups):,} 个重复组 · 冗余大小 {format_size(result.redundant_bytes)}'
                                + (' · 部分结果' if not result.complete else ''))
            self.status.setToolTip('\n'.join(result.errors))
        self.status.setVisible(result is not None)
        self.select_button.setEnabled(bool(groups) and not self.busy)

    def set_busy(self, busy):
        self.busy = busy
        self.minimum.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.select_button.setEnabled(not busy and bool(self.model.groups))
        self.tree.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            self.status.show()
            self.progress.setRange(0, 0)
            self.status.setText('正在比对文件内容…')

    def set_progress(self, stage, done, total):
        self.status.setText(f'{stage.partition(" · ")[0]} · {done:,} / {total:,}')
        self.status.setToolTip(stage)
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def select_copies(self):
        if self.busy:
            return
        records = []
        for group in self.model.groups:
            keeper = next((record for record in group.records if record_key(record) in self.model._blocked), group.records[0])
            keeper_key = record_key(keeper)
            records.extend(record for record in group.records if record_key(record) != keeper_key)
        self.model.replace_checked(records)

    def _selection_changed(self):
        if self._updating:
            return
        self.clear_button.setEnabled(bool(self.model.checked))
        self.checkedChanged.emit()

    def selected_records(self):
        return self.model.selected_records()

    def clear_selection(self):
        self.model.clear_selection()
