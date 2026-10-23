"""快照范围内的只读空间分析与重复文件分组界面。"""
from datetime import datetime
import threading

from PySide6.QtCore import QAbstractItemModel, QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QProgressBar, QTreeView, QVBoxLayout

from ..core.storage import analyze_usage, find_duplicates
from .widgets import CachedDelegate, Metric, button, enable_pixel_scroll, format_size, label, number_font


class UsageModel(QAbstractTableModel):
    HEADERS = ('顶层目录 / 根目录文件', '独立逻辑大小', '已分配空间', '独立文件数')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return
        name, (logical, allocated, count) = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (name, format_size(logical), format_size(allocated), f'{count:,}')[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return name
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column():
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class DuplicateModel(QAbstractItemModel):
    HEADERS = ('文件 / 重复组', '副本数 / 类型', '单份大小', '冗余逻辑大小 / 完整路径')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups = ()

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= 4 or row >= self.rowCount(parent):
            return QModelIndex()
        return self.createIndex(row, column, parent.row() + 1 if parent.isValid() else 0)

    def parent(self, index):
        if index.isValid() and index.internalId():
            return self.createIndex(index.internalId() - 1, 0, 0)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self.groups)
        if parent.column() or parent.internalId():
            return 0
        return len(self.groups[parent.row()])

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return
        if index.internalId():
            record = self.groups[index.internalId() - 1][index.row()]
            values = (record.path.name, '独立文件', format_size(record.size), str(record.path))
            tooltip = str(record.path)
        else:
            group = self.groups[index.row()]
            values = (f'重复组 {index.row() + 1:03} · {group[0].path.name}', str(len(group)),
                      format_size(group[0].size), format_size(group[0].size * (len(group) - 1)))
            tooltip = '内容校验一致；冗余量为逻辑大小，不代表立即可释放空间。'
        if role == Qt.ItemDataRole.DisplayRole:
            return values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return tooltip
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (1, 2):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class StorageDialog(QDialog):
    def __init__(self, snapshot, mode, run_job, parent=None):
        super().__init__(parent)
        self.snapshot, self.mode, self.run_job = snapshot, mode, run_job
        self.cancel = threading.Event()
        self.closed = False
        self.busy = False
        self.result = None
        self.progress_info = None
        self.setWindowTitle('空间利用率分析' if mode == 'usage' else '重复文件识别')
        self.resize(1080, 690)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(label(self.windowTitle(), 'section'))
        path = label(str(snapshot.root), 'muted')
        path.setWordWrap(True)
        layout.addWidget(path)
        stamp = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S')
        layout.addWidget(label(f'快照 {stamp} · {len(snapshot.files):,} 个文件' + (' · 扫描范围不完整' if not snapshot.complete else ''), 'muted'))
        cards = QHBoxLayout()
        titles = ('独立逻辑大小', '已分配空间', '正分配差额', '逻辑大于分配') if mode == 'usage' else ('重复组', '组内文件', '冗余逻辑大小', '硬链接别名')
        self.metrics = [Metric(title) for title in titles]
        for metric in self.metrics:
            metric.value.setFont(number_font(self.parent().property('theme') == 'light' if self.parent() else False))
            cards.addWidget(metric)
        self.metrics[2].setToolTip('分配量大于逻辑大小的差额，不含未统计的目录和文件系统元数据。' if mode == 'usage'
                                   else '按每组保留一份计算逻辑冗余，硬链接及共享存储可能使实际收益更少。')
        self.metrics[3].setToolTip('稀疏、压缩或驻留数据等可能造成逻辑大小大于分配量，不能直接归为浪费。' if mode == 'usage'
                                   else '本次重复候选中指向同一文件身份的额外路径，不计为独立副本。')
        layout.addLayout(cards)
        self.status = label('等待分析', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.model = UsageModel(self) if mode == 'usage' else DuplicateModel(self)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setItemDelegate(CachedDelegate(self.tree))
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(mode != 'usage')
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        enable_pixel_scroll(self.tree)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in ((1, 130), (2, 150), (3, 170 if mode == 'usage' else 330)):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setStretchLastSection(False)
        layout.addWidget(self.tree, 1)
        self.detail = label('', 'muted')
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        actions = QHBoxLayout()
        self.stop_button = button('停止分析', self.stop)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        actions.addWidget(button('关闭', self.close))
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.poll)

    def start(self):
        if self.busy or self.closed:
            return
        self.busy = True
        self.timer.start()

        def progress(stage, done, total):
            self.progress_info = stage, done, total

        action = analyze_usage if self.mode == 'usage' else find_duplicates
        self.run_job(lambda: action(self.snapshot, self.cancel, progress), self.accept_result, self.failed, self.finished)

    def poll(self):
        if self.progress_info and not self.cancel.is_set():
            stage, done, total = self.progress_info
            self.status.setText(f'{stage} · {done:,} / {total:,}')
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(done)

    def stop(self):
        self.cancel.set()
        self.status.setText('正在停止，等待当前读取结束…')
        self.stop_button.setEnabled(False)

    def accept_result(self, result):
        if self.closed:
            return
        self.result = result
        state = '已停止 · 部分结果' if result.cancelled else '分析完成' if result.complete else '分析完成 · 部分结果'
        if self.mode == 'usage':
            values = [format_size(value) for value in (result.logical_bytes, result.allocated_bytes, result.slack_bytes, result.saved_bytes)]
            note = f'{state} · 独立文件 {result.unique_files:,} · 硬链接别名 {result.hardlinks:,} · 跳过 {result.skipped:,}'
            self.model.beginResetModel()
            self.model.rows = list(result.directories.items())
            self.model.endResetModel()
            intervals = ('<4 KB', '4 KB～1 MB', '1～10 MB', '10～100 MB', '≥100 MB')
            text = '文件分布：' + ' · '.join(f'{name}：{count:,}' for name, count in zip(intervals, result.buckets))
            if result.volume:
                volume = result.volume
                text += f'\n所在分区：已用 {format_size(volume.used)} / {format_size(volume.total)}（{volume.used / volume.total:.1%}）· 可用 {format_size(volume.free)}'
            text += '\n仅统计已核验文件，不含目录及文件系统元数据；跨目录硬链接归入首次核验路径，只计一次。'
        else:
            values = [str(len(result.groups)), str(sum(len(group) for group in result.groups)), format_size(result.redundant_bytes), str(result.hardlinks)]
            note = f'{state} · 已检查候选 {result.checked:,} · 跳过 {result.skipped:,}'
            self.model.beginResetModel()
            self.model.groups = result.groups
            self.model.endResetModel()
            for row in range(min(3, len(result.groups))):
                self.tree.expand(self.model.index(row, 0))
            text = '零字节文件不参与，硬链接别名不算独立副本。'
            text += '\n' + ('未发现重复内容。' if not result.groups and result.complete else '识别结果仅供核对，不会自动删除；冗余逻辑大小不代表实际可释放空间。')
        for metric, value in zip(self.metrics, values):
            metric.value.setText(value)
        self.status.setText(note)
        self.status.setToolTip('\n'.join(result.errors))
        self.detail.setText(text)

    def failed(self, error):
        if not self.closed:
            self.status.setText(f'分析失败：{error}')

    def finished(self):
        self.busy = False
        self.timer.stop()
        if not self.closed:
            self.progress.hide()
            self.stop_button.setEnabled(False)
        else:
            self.deleteLater()

    def closeEvent(self, event):
        self.closed = True
        self.cancel.set()
        self.timer.stop()
        super().closeEvent(event)
        if not self.busy:
            self.deleteLater()

    def reject(self):
        self.closed = True
        self.cancel.set()
        self.timer.stop()
        super().reject()
        if not self.busy:
            self.deleteLater()
