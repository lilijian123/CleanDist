"""按需加载的树形模型，文件记录保存在工作线程生成的分析数据中，无需逐个创建控件。"""
from datetime import datetime

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from ..core.analysis import DirectoryEntry, rank_files
from .widgets import format_size


class Node:
    def __init__(self, entry=None, parent=None, row=0):
        self.entry, self.parent, self.row = entry, parent, row
        self.children = []
        self.fetched = False


class FileModel(QAbstractItemModel):
    BATCH = 200
    HEADERS = ('名称 / 相对路径', '逻辑大小', '文件数', '本轮增量', '状态', '修改时间')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.root = Node()
        self.nodes = {}
        self.checked = {}
        self.flat = False
        self.entries = []
        self.children = {}

    def set_analysis(self, analysis, mode='tree', min_size=0, query='', order='size'):
        self.beginResetModel()
        self.analysis = analysis
        self.flat = mode != 'tree' or bool(query.strip())
        self.children = (analysis.growth_children if order == 'growth' else analysis.children) if analysis else {}
        self.entries = (rank_files(analysis, mode, min_size, query) if self.flat else self.children['']) if analysis else []
        self.root = Node()
        self.nodes = {}
        files = analysis.snapshot.files if analysis else {}
        self.checked = {key: files[key] for key, old in self.checked.items()
                        if key in files and files[key].fingerprint == old.fingerprint}
        self._append_nodes(self.root, self.entries[:self.BATCH])
        self.endResetModel()

    def _append_nodes(self, node, entries):
        node.fetched = True
        for entry in entries:
            child = Node(entry, node, len(node.children))
            node.children.append(child)
            self.nodes[entry.relative] = child

    def _node_from_index(self, index):
        return index.internalPointer() if index.isValid() else self.root

    def _child_entries(self, node):
        if node is self.root:
            return self.entries
        if not self.flat and isinstance(node.entry, DirectoryEntry):
            return self.children.get(node.entry.relative, [])
        return []

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= len(self.HEADERS) or (parent.isValid() and parent.column() != 0):
            return QModelIndex()
        children = self._node_from_index(parent).children
        return self.createIndex(row, column, children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = self._node_from_index(index).parent
        return QModelIndex() if node is self.root else self.createIndex(node.row, 0, node)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() and parent.column() != 0 else len(self._node_from_index(parent).children)

    def hasChildren(self, parent=QModelIndex()):
        return not (parent.isValid() and parent.column() != 0) and bool(self._child_entries(self._node_from_index(parent)))

    def canFetchMore(self, parent=QModelIndex()):
        return not self._node_from_index(parent).fetched and self.has_more(parent)

    def has_more(self, parent=QModelIndex()):
        node = self._node_from_index(parent)
        return not (parent.isValid() and parent.column() != 0) and len(node.children) < len(self._child_entries(node))

    def fetchMore(self, parent=QModelIndex()):
        if not self.canFetchMore(parent):
            return
        self.load_more(parent)

    def load_more(self, parent=QModelIndex()):
        if not self.has_more(parent):
            return
        node = self._node_from_index(parent)
        start = len(node.children)
        batch = self._child_entries(node)[start:start + self.BATCH]
        self.beginInsertRows(parent, start, start + len(batch) - 1)
        self._append_nodes(node, batch)
        self.endInsertRows()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self._node_from_index(index).entry
        directory = isinstance(entry, DirectoryEntry)
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == 0 and not directory and entry.count:
            return Qt.CheckState.Checked if entry.relative in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return ('汇总项，展开后勾选具体文件' if directory else str(entry.record.path))
        if role == Qt.ItemDataRole.ForegroundRole and column == 3 and entry.delta:
            return QColor('#ffbd83' if entry.delta > 0 else '#53dfbf')
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (1, 2, 3):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return entry.relative if self.flat else entry.name
            if column == 1:
                return format_size(entry.size)
            if column == 2:
                return f'{entry.count:,}'
            if column == 3:
                return format_size(entry.delta, True) if self.analysis.comparable else '未确认'
            if column == 4:
                return entry.state
            if column == 5:
                return datetime.fromtimestamp(entry.modified_ns / 1e9).strftime('%m-%d %H:%M:%S') if entry.modified_ns else '—'

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        entry = self._node_from_index(index).entry
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0 and not isinstance(entry, DirectoryEntry) and entry.count:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or not (self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        entry = self._node_from_index(index).entry
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.checked[entry.relative] = entry.record
        else:
            self.checked.pop(entry.relative, None)
        self.dataChanged.emit(index, index, [role])
        return True

    def selected_records(self):
        return list(self.checked.values())
