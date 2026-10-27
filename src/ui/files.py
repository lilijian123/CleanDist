"""按批次生成 Qt 原生树条目，滚动时复用显示值，无需逐个创建控件。"""
from datetime import datetime

from PySide6.QtCore import QEvent, QModelIndex, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle
from ..core.analysis import DirectoryEntry, rank_files
from .widgets import CachedDelegate, format_size, number_font
from .theme import color


class FileModel(QStandardItemModel):
    BATCH = 200
    HEADERS = ('名称 / 相对路径', '大小 / 占比', '文件数', '本轮增量', '状态', '修改时间')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.root = self.invisibleRootItem()
        self.root.fetched = True
        self.nodes = {}
        self.checked = {}
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
        self.itemChanged.connect(self._selection_changed)
        if parent is not None:
            parent.installEventFilter(self)

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
        self.checked = {key: files[key] for key, old in self.checked.items()
                        if key in files and files[key].fingerprint == old.fingerprint}
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
            tooltip = '汇总项，展开后勾选具体文件' if directory else str(entry.record.path)
            for column, item in enumerate(items):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(tooltip)
                if column in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if not directory and entry.count:
                first.setFlags(first.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                first.setData(Qt.CheckState.Checked if entry.relative in self.checked else Qt.CheckState.Unchecked,
                              Qt.ItemDataRole.CheckStateRole)
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

    def _selection_changed(self, item):
        entry = getattr(item, 'entry', None)
        if entry is None or isinstance(entry, DirectoryEntry) or not entry.count:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.checked[entry.relative] = entry.record
        else:
            self.checked.pop(entry.relative, None)

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
