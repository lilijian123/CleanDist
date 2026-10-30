"""按真实逻辑大小分配面积的矩形树图，仅在数据或尺寸变化时布局。"""
from bisect import bisect_left
from dataclasses import dataclass
import hashlib
from itertools import islice

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QToolTip, QVBoxLayout, QWidget

from ..core.analysis import DirectoryEntry
from .theme import color
from .widgets import button, format_size, label


def layout_tiles(weights, area):
    """按权重平衡切分长边，面积守恒，不固定斐波那契比例。"""
    rectangles = [QRectF() for _ in weights]
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + max(0, weight))

    def split(start, end, rect):
        total = prefix[end] - prefix[start]
        if total <= 0 or rect.isEmpty():
            return
        if end - start == 1:
            rectangles[start] = rect
            return
        middle = bisect_left(prefix, prefix[start] + total / 2, start + 1, end)
        middle = min(end - 1, max(start + 1, middle))
        if middle > start + 1 and abs(prefix[middle - 1] - prefix[start] - total / 2) < abs(prefix[middle] - prefix[start] - total / 2):
            middle -= 1
        share = (prefix[middle] - prefix[start]) / total
        if rect.width() >= rect.height():
            width = rect.width() * share
            first = QRectF(rect.x(), rect.y(), width, rect.height())
            second = QRectF(rect.x() + width, rect.y(), rect.width() - width, rect.height())
        else:
            height = rect.height() * share
            first = QRectF(rect.x(), rect.y(), rect.width(), height)
            second = QRectF(rect.x(), rect.y() + height, rect.width(), rect.height() - height)
        split(start, middle, first)
        split(middle, end, second)

    if weights:
        split(0, len(weights), QRectF(area))
    return rectangles


@dataclass
class OtherEntry:
    name: str
    size: int
    relative: str = ''
    count: int = 0


class Treemap(QWidget):
    directory_clicked = Signal(str)
    file_clicked = Signal(object)
    LIMIT = 180

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.relative = ''
        self.entries = []
        self.rectangles = []
        self.previews = {}
        self.hovered = -1
        self.setMinimumHeight(180)
        self.setMouseTracking(True)

    def set_directory(self, analysis, relative=''):
        self.analysis, self.relative = analysis, relative
        entries = [entry for entry in analysis.children.get(relative, ()) if entry.size > 0] if analysis else []
        self.entries = entries[:self.LIMIT]
        if len(entries) > self.LIMIT:
            tail = entries[self.LIMIT:]
            self.entries.append(OtherEntry(f'其他 {len(tail):,} 项', sum(entry.size for entry in tail)))
        self.hovered = -1
        self.previews = {}
        for entry in self.entries:
            if isinstance(entry, DirectoryEntry):
                children = list(islice((item for item in analysis.children.get(entry.relative, ()) if item.size > 0), 16))
                remaining = entry.size - sum(item.size for item in children)
                if remaining > 0:
                    children.append(OtherEntry('其他', remaining))
                self.previews[entry.relative] = children
        self.arrange()

    def arrange(self):
        self.rectangles = layout_tiles([entry.size for entry in self.entries], QRectF(self.rect()))
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()

    def shade(self, entry, child=False):
        seed = hashlib.blake2b(entry.relative.encode('utf-8'), digest_size=2).digest()
        hue = (95 + int.from_bytes(seed, 'little') % 135) / 360
        light = color(self, 'muted') == '#74746d'
        return QColor.fromHslF(hue, .16 if light else .20, (.84 if light else .28) + (.025 if child else 0))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.entries:
            painter.setPen(QColor(color(self, 'muted')))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '暂无可绘制的文件数据')
            return
        total = sum(entry.size for entry in self.entries)
        for i, (entry, area) in enumerate(zip(self.entries, self.rectangles)):
            rect = area.adjusted(2, 2, -2, -2)
            if rect.width() < 1 or rect.height() < 1:
                continue
            shade = self.shade(entry)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shade.lighter(112) if i == self.hovered else shade)
            painter.drawRoundedRect(rect, 5, 5)
            # 子目录内轻量显示下一层分布，交互仍以当前层的父块为准。
            if isinstance(entry, DirectoryEntry) and rect.width() > 100 and rect.height() > 90:
                children = self.previews.get(entry.relative, ())
                if children:
                    inner = rect.adjusted(5, 50, -5, -5)
                    for child, tile in zip(children, layout_tiles([item.size for item in children], inner)):
                        tile = tile.adjusted(1, 1, -1, -1)
                        if tile.width() > 0 and tile.height() > 0:
                            painter.setBrush(self.shade(child, True))
                            painter.drawRoundedRect(tile, 3, 3)
            painter.setPen(QColor('#26352a' if color(self, 'muted') == '#74746d' else '#eef1e9'))
            if rect.width() > 65 and rect.height() > 30:
                text = painter.fontMetrics().elidedText(entry.name, Qt.TextElideMode.ElideMiddle, max(1, int(rect.width() - 20)))
                painter.drawText(rect.adjusted(10, 7, -10, -5), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, text)
                if rect.height() > 52:
                    painter.drawText(rect.adjusted(10, 28, -10, -5), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                                     f'{format_size(entry.size)} · {entry.size / total:.1%}')

    def hit(self, point):
        return next((i for i, rect in enumerate(self.rectangles) if rect.contains(point)), -1)

    def mouseMoveEvent(self, event):
        hovered = self.hit(event.position())
        if hovered != self.hovered:
            self.hovered = hovered
            if hovered >= 0:
                entry = self.entries[hovered]
                action = '点击进入目录' if isinstance(entry, DirectoryEntry) else '在列表中查看其余明细' if isinstance(entry, OtherEntry) else '点击查看完整路径'
                QToolTip.showText(event.globalPosition().toPoint(), f'{entry.name}\n{format_size(entry.size)}\n{action}', self)
                self.setCursor(Qt.CursorShape.PointingHandCursor if not isinstance(entry, OtherEntry) else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, event):
        self.hovered = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.hit(event.position())
            if index >= 0:
                entry = self.entries[index]
                if isinstance(entry, DirectoryEntry):
                    self.directory_clicked.emit(entry.relative)
                elif not isinstance(entry, OtherEntry):
                    self.file_clicked.emit(entry.record)
        super().mousePressEvent(event)


class MapPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self.history = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.back_button = button('返回上层', self.go_back)
        self.back_button.setEnabled(False)
        controls.addWidget(self.back_button)
        self.path_label = label('', 'muted')
        controls.addWidget(self.path_label, 1)
        layout.addLayout(controls)
        self.map = Treemap()
        self.map.directory_clicked.connect(self.open_directory)
        layout.addWidget(self.map, 1)

    def set_analysis(self, analysis):
        same_root = self.analysis is not None and analysis is not None and self.analysis.snapshot.root == analysis.snapshot.root
        relative = self.map.relative if same_root and self.map.relative in analysis.children else ''
        if not relative:
            self.history.clear()
        self.analysis = analysis
        self.show_directory(relative)

    def show_directory(self, relative):
        self.map.set_directory(self.analysis, relative)
        text = '扫描根目录'
        if self.analysis is not None:
            snapshot = self.analysis.snapshot
            root = snapshot.root
            if hasattr(snapshot, 'snapshots'):
                if relative:
                    index, _, relative = relative.removeprefix('\0root/').partition('/')
                    root = snapshot.snapshots[int(index)].root
                else:
                    root = None
                    text = '所有位置'
            if root is not None:
                small = relative.startswith('\0small/')
                text = str(root / relative.removeprefix('\0small/'))
                if small:
                    text += ' · 小文件（小于1 MB）'
        self.path_label.setText(text)
        self.path_label.setToolTip(text)
        self.back_button.setEnabled(bool(self.history))

    def open_directory(self, relative):
        if self.analysis is None or relative not in self.analysis.children or relative == self.map.relative:
            return
        self.history.append(self.map.relative)
        self.show_directory(relative)

    def go_back(self):
        if self.history:
            self.show_directory(self.history.pop())
