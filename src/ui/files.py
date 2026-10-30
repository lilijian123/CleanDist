"""按批次生成 Qt 原生树条目，滚动时复用显示值，无需逐个创建控件。"""
from datetime import datetime

from PySide6.QtCore import QEvent, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle
from ..core.analysis import DirectoryEntry, rank_files
from .widgets import CachedDelegate, format_size, number_font
from .theme import color


class FileModel(QStandardItemModel):
    checkedChanged = Signal()
    BATCH = 200
    HEADERS = ('名称 / 相对路径', '大小 / 占比', '文件数', '本轮增量', '状态', '修改时间')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.nodes = {}
        self.checked = {}
        self.checked_counts = {}
        self.blocked = set()
        self.blocked_counts = {}
        self.syncing_checks = False
        self.flat = False
        self.entries = []
        self.children = {}
        self.total_size = 0
        style = QApplication.style()
        self.folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.group_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        self.folder_font = QFont()
        self.folder_font.setWeight(QFont.Weight(660))
        self.number_font = number_font()
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.horizontalHeaderItem(1).setToolTip('占本次已扫描文件逻辑总大小的比例，不代表实际磁盘使用率。')
        self.dataChanged.connect(self._selection_changed)
        if parent is not None:
            parent.installEventFilter(self)

    @property
    def root(self):
        # This C++-owned root has no Python destruction hook. Keeping its borrowed
        # wrapper after model deletion can alias a new Qt object at the same address.
        root = self.invisibleRootItem()
        root.fetched = True
        return root

    def set_analysis(self, analysis, mode='tree', min_size=0, query='', order='size'):
        # 移除旧行期间保持旧分析数据有效，避免 hasChildren 与尚存子行矛盾。
        self.removeRows(0, self.rowCount())
        self.analysis = analysis
        self.total_size = analysis.directories[''].size if analysis else 0
        self.flat = mode != 'tree' or bool(query.strip())
        self.children = (analysis.growth_children if order == 'growth' else analysis.children) if analysis else {}
        self.entries = (rank_files(analysis, mode, min_size, query) if self.flat else self.children['']) if analysis else []
        self.nodes = {}
        files = analysis.snapshot.files if analysis else {}
        cleanup = analysis.cleanup if analysis else None
        self.blocked = {record.relative for record in cleanup.readonly_records()} if cleanup else set()
        self.blocked_counts = {}
        for key in self.blocked:
            self._count_path(self.blocked_counts, files[key], 1)
        self.checked = {key: files[key] for key, old in self.checked.items()
                        if key in files and key not in self.blocked and files[key].root == old.root and files[key].fingerprint == old.fingerprint}
        self.checked_counts = {}
        for record in self.checked.values():
            self._adjust_counts(record, 1)
        self.root.fetched = True
        self._append_nodes(self.root, self.entries[:self.BATCH])

    def _append_nodes(self, node, entries):
        node.fetched = True
        for entry in entries:
            directory = isinstance(entry, DirectoryEntry)
            share = entry.size / self.total_size if self.total_size else 0
            values = (
                entry.relative if self.flat else entry.name,
                f'{format_size(entry.size)}  ·  {share:.1%}',
                f'{entry.count:,}' if directory else '—',
                format_size(entry.delta, True) if self.analysis.comparable else '未确认',
                entry.state,
                datetime.fromtimestamp(entry.modified_ns / 1e9).strftime('%m-%d %H:%M:%S') if entry.modified_ns else '—',
            )
            items = [QStandardItem(value) for value in values]
            first = items[0]
            first.entry = entry
            first.fetched = False
            if directory:
                first.setColumnCount(len(self.HEADERS))
            first.setIcon(self.group_icon if directory and entry.state == '明细分组' else self.folder_icon if directory else self.file_icon)
            items[1].setData(share, Qt.ItemDataRole.UserRole)
            tooltip = ('勾选本项可选择其中已扫描的文件；文件夹本身保留。' if directory else str(entry.record.path))
            if entry.relative in self.blocked:
                tooltip += '\n仅查看：' + self.analysis.cleanup.by_file[entry.relative].reason
            elif directory and self.blocked_counts.get(entry.relative):
                tooltip += '\n系统管理或高风险文件仅查看，不参与批选。'
            for column, item in enumerate(items):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(tooltip)
                if column in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            selectable = entry.count - self.blocked_counts.get(entry.relative, 0) if directory else int(entry.count and entry.relative not in self.blocked)
            if selectable:
                first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                first.setData(self._check_state(entry), Qt.ItemDataRole.CheckStateRole)
            self._style_row(items, entry)
            self.nodes[entry.relative] = first
            node.appendRow(items)

    def _node_from_index(self, index):
        return self.itemFromIndex(index.siblingAtColumn(0)) if index.isValid() else self.root

    def _child_entries(self, node):
        if node is self.root:
            return self.entries
        if not self.flat and isinstance(node.entry, DirectoryEntry):
            return self.children.get(node.entry.relative, [])
        return []

    def hasChildren(self, parent=QModelIndex()):
        return not (parent.isValid() and parent.column() != 0) and bool(self._child_entries(self._node_from_index(parent)))

    def canFetchMore(self, parent=QModelIndex()):
        return self.has_more(parent) and not self._node_from_index(parent).fetched

    def has_more(self, parent=QModelIndex()):
        node = self._node_from_index(parent)
        return not (parent.isValid() and parent.column() != 0) and node.rowCount() < len(self._child_entries(node))

    def fetchMore(self, parent=QModelIndex()):
        if not self.canFetchMore(parent):
            return
        self.load_more(parent)

    def load_more(self, parent=QModelIndex()):
        if not self.has_more(parent):
            return
        node = self._node_from_index(parent)
        start = node.rowCount()
        batch = self._child_entries(node)[start:start + self.BATCH]
        self._append_nodes(node, batch)

    def _style_row(self, items, entry):
        if isinstance(entry, DirectoryEntry):
            items[0].setFont(self.folder_font)
        for column in (1, 2, 3, 5):
            items[column].setFont(self.number_font)
        if entry.delta:
            items[3].setData(QColor(color(self.parent(), 'positive' if entry.delta > 0 else 'accent')),
                             Qt.ItemDataRole.ForegroundRole)

    def set_fonts(self, font, folder_weight):
        self.number_font = font
        self.folder_font.setWeight(QFont.Weight(folder_weight))
        for node in self.nodes.values():
            parent = node.parent() or self.root
            self._style_row([parent.child(node.row(), column) for column in range(len(self.HEADERS))], node.entry)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.DynamicPropertyChange and bytes(event.propertyName()) == b'theme':
            for node in self.nodes.values():
                if node.entry.delta:
                    parent = node.parent() or self.root
                    parent.child(node.row(), 3).setData(
                        QColor(color(self.parent(), 'positive' if node.entry.delta > 0 else 'accent')),
                        Qt.ItemDataRole.ForegroundRole)
        return super().eventFilter(watched, event)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or not (self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        return super().setData(index, Qt.CheckState(value), role)

    def _adjust_counts(self, record, delta):
        self._count_path(self.checked_counts, record, delta)

    def _count_path(self, counts, record, delta):
        parent = record.relative.rpartition('/')[0]
        group = '\0small/' + parent
        if record.size < 1024 * 1024 and group in self.children:
            counts[group] = counts.get(group, 0) + delta
        while True:
            counts[parent] = counts.get(parent, 0) + delta
            if not parent:
                break
            parent = parent.rpartition('/')[0]

    def _check_state(self, entry):
        if not isinstance(entry, DirectoryEntry):
            return Qt.CheckState.Checked if entry.relative in self.checked else Qt.CheckState.Unchecked
        count = self.checked_counts.get(entry.relative, 0)
        eligible = entry.count - self.blocked_counts.get(entry.relative, 0)
        return (Qt.CheckState.Checked if count == eligible and count else
                Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked)

    def _entry_records(self, entry):
        pending = [entry]
        while pending:
            current = pending.pop()
            if isinstance(current, DirectoryEntry):
                pending.extend(self.children.get(current.relative, ()))
            elif current.count and current.relative not in self.blocked:
                record = self.analysis.snapshot.files.get(current.relative)
                if record is not None:
                    yield record

    def _sync_checks(self):
        parents = {}
        self.syncing_checks = True
        previous = self.blockSignals(True)
        try:
            for node in self.nodes.values():
                if not node.entry.count:
                    continue
                state = self._check_state(node.entry)
                if node.checkState() != state:
                    node.setCheckState(state)
                    parent = node.parent()
                    parents[parent.entry.relative if parent is not None else None] = parent.index() if parent is not None else QModelIndex()
        finally:
            self.blockSignals(previous)
        try:
            for parent in parents.values():
                self.dataChanged.emit(self.index(0, 0, parent), self.index(self.rowCount(parent) - 1, 0, parent),
                                      [Qt.ItemDataRole.CheckStateRole])
        finally:
            self.syncing_checks = False

    def _selection_changed(self, top_left, bottom_right, roles):
        if self.syncing_checks or Qt.ItemDataRole.CheckStateRole not in roles:
            return
        item = self.itemFromIndex(top_left)
        entry = getattr(item, 'entry', None)
        if entry is None or not entry.count or item.checkState() == Qt.CheckState.PartiallyChecked:
            return
        selected = item.checkState() == Qt.CheckState.Checked
        changed = False
        for record in self._entry_records(entry):
            if selected != (record.relative in self.checked):
                if selected:
                    self.checked[record.relative] = record
                else:
                    self.checked.pop(record.relative)
                self._adjust_counts(record, 1 if selected else -1)
                changed = True
        if changed:
            self._sync_checks()
            self.checkedChanged.emit()

    def selected_records(self):
        return list(self.checked.values())


class SizeDelegate(CachedDelegate):
    """在大小文本下绘制占比条，不创建逐行控件或读取文件元数据。"""
    def draw(self, painter, option, index):
        super().draw(painter, option, index)
        share = index.data(Qt.ItemDataRole.UserRole) or 0
        bar = QRectF(option.rect.left() + 12, option.rect.bottom() - 5,
                     max(0, option.rect.width() - 24), 3)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color(self.view, 'grid')))
        painter.drawRoundedRect(bar, 1.5, 1.5)
        bar.setWidth(bar.width() * min(1, max(0, share)))
        painter.setBrush(QColor(color(self.view, 'accent')))
        painter.drawRoundedRect(bar, 1.5, 1.5)
        painter.restore()
