"""可复用的 Qt 控件、格式化工具与内存图表。"""
from collections import OrderedDict
from functools import lru_cache
from math import ceil

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QLabel, QLineEdit, QPushButton, QTableWidget,
    QStyledItemDelegate, QStyleOptionViewItem, QTableWidgetItem, QVBoxLayout, QWidget,
)
from .theme import color


def format_size(value, signed=False):
    if value is None:
        return '不可用'
    sign = ('+' if value > 0 else '−' if value < 0 else '') if signed else ''
    value = abs(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{sign}{value:.1f} {unit}' if unit != 'B' else f'{sign}{int(value)} B'
        value /= 1024


def label(text='', name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    return item


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName('primary')
    item.clicked.connect(callback)
    return item


def table(headers):
    item = QTableWidget(0, len(headers))
    item.setHorizontalHeaderLabels(headers)
    item.setAlternatingRowColors(True)
    item.setShowGrid(False)
    item.verticalHeader().hide()
    item.verticalHeader().setDefaultSectionSize(36)
    item.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    item.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    enable_pixel_scroll(item)
    item.horizontalHeader().setStretchLastSection(True)
    item.horizontalHeader().setHighlightSections(False)
    return item


def enable_pixel_scroll(view):
    # 使用原生像素滚动，避免整行跳动，并保留触控板的细粒度位移。
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.verticalScrollBar().setSingleStep(20)
    view.horizontalScrollBar().setSingleStep(20)


@lru_cache(maxsize=2)
def number_font(light=False):
    font = QFont(QApplication.font())
    font.setFeature(QFont.Tag.fromString('tnum'), 1)
    font.setWeight(QFont.Weight(400 if light else 360))
    return font


class FocusEdit(QLineEdit):
    """用两层内侧阴影表达焦点，不改变输入框尺寸或添加外轮廓。"""
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.hasFocus() and not self.isReadOnly():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for inset, width, shade in ((1.5, 3, color(self, 'focus_outer')), (3.5, 1, color(self, 'focus_inner'))):
                painter.setPen(QPen(QColor(shade), width))
                painter.drawRoundedRect(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), 6, 6)


class NumberItem(QTableWidgetItem):
    def __init__(self, text, number=None):
        super().__init__(text)
        self.setFont(number_font())
        self.setData(Qt.ItemDataRole.UserRole + 1, number if number is not None else float('-inf'))
        self.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole + 1) < other.data(Qt.ItemDataRole.UserRole + 1)


class CachedDelegate(QStyledItemDelegate):
    """复用近期单元格的绘制结果，避免滚动时重复排版与样式绘制。"""
    LIMIT = 256

    def __init__(self, view):
        super().__init__(view)
        self.cache = OrderedDict()
        self.view = view
        self.offset = (view.horizontalScrollBar().value(), view.verticalScrollBar().value())
        self.fast_scroll = False
        view.verticalScrollBar().valueChanged.connect(self.scrolled)
        view.horizontalScrollBar().valueChanged.connect(self.scrolled)
        model = view.model()
        for signal in (model.modelReset, model.layoutChanged, model.dataChanged,
                       model.rowsInserted, model.rowsRemoved, model.rowsMoved):
            signal.connect(lambda *_: self.cache.clear())

    def scrolled(self, *_):
        current = (self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value())
        self.fast_scroll = (abs(current[0] - self.offset[0]) >= self.view.viewport().width() / 2
                            or abs(current[1] - self.offset[1]) >= self.view.viewport().height() / 2)
        self.offset = current

    def paint(self, painter, option, index):
        if option.rect.isEmpty():
            return
        # 大幅跳转几乎没有可复用区域，直接绘制避免冷缓存的图像分配开销。
        if self.fast_scroll:
            self.draw(painter, option, index)
            return
        ratio = painter.device().devicePixelRatioF()
        key = (index.internalId(), index.row(), index.column(), option.rect.size().width(),
               option.rect.size().height(), ratio, option.state.value, option.features.value,
               option.palette.cacheKey(), option.font.key(), option.direction.value,
               option.textElideMode.value, option.decorationSize.width(), option.decorationSize.height())
        pixmap = self.cache.get(key)
        if pixmap is None:
            pixmap = QPixmap(ceil(option.rect.width() * ratio), ceil(option.rect.height() * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            cached = QStyleOptionViewItem(option)
            cached.rect.moveTo(0, 0)
            canvas = QPainter(pixmap)
            try:
                self.draw(canvas, cached, index)
            finally:
                canvas.end()
            self.cache[key] = pixmap
            # 缓存只覆盖近期可见区域，不随扫描结果总量增长。
            if len(self.cache) > self.LIMIT:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        painter.drawPixmap(option.rect.topLeft(), pixmap)

    def draw(self, painter, option, index):
        super().paint(painter, option, index)


class Metric(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName('card')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.addWidget(label(title, 'muted'))
        self.value = label('—', 'metric')
        self.value.setFont(number_font())
        layout.addWidget(self.value)


class TrendChart(QWidget):
    def __init__(self):
        super().__init__()
        self.points = ()
        self.setMinimumHeight(145)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(85, 12, max(10, self.width() - 110), max(10, self.height() - 40))
        painter.setPen(QPen(QColor(color(self, 'grid')), 1))
        for fraction in (0, 0.5, 1):
            y = area.top() + area.height() * fraction
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        if not self.points:
            painter.setPen(QColor(color(self, 'muted')))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, '选择进程查看趋势')
            return
        ceiling = max(max(value for _, value in self.points) * 1.15, 1024 * 1024)
        first, last = self.points[0][0], self.points[-1][0]
        span = max(last - first, 1)
        painter.setPen(QColor(color(self, 'muted')))
        for fraction in (0, 0.5, 1):
            text_rect = QRectF(0, area.top() + area.height() * fraction - 9, 76, 20)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight, format_size(ceiling * (1 - fraction)))
        painter.drawText(QPointF(area.left(), self.height() - 8), f'{last - first:.0f}秒前')
        painter.drawText(QPointF(area.right() - 30, self.height() - 8), '现在')
        points = [QPointF(area.left() + (timestamp - first) / span * area.width(),
                          area.bottom() - value / ceiling * area.height()) for timestamp, value in self.points]
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(points[-1].x(), area.bottom()))
        fill.lineTo(QPointF(points[0].x(), area.bottom()))
        fill.closeSubpath()
        gradient = QLinearGradient(area.topLeft(), area.bottomLeft())
        shade = QColor(color(self, 'accent'))
        shade.setAlpha(45)
        gradient.setColorAt(0, shade)
        shade.setAlpha(2)
        gradient.setColorAt(1, shade)
        painter.fillPath(fill, gradient)
        painter.setPen(QPen(QColor(color(self, 'accent')), 2))
        painter.drawPath(path)
        painter.setBrush(QColor(color(self, 'accent')))
        painter.drawEllipse(points[-1], 3, 3)
