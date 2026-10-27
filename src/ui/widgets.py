"""可复用的 Qt 控件、格式化工具与内存图表。"""
from collections import OrderedDict
from functools import lru_cache
from math import ceil, cos, pi, sin

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QLabel, QLineEdit, QPushButton, QTableWidget,
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


class BrandMark(QWidget):
    """无字母和底块的磁盘标记，原生矢量绘制以适配主题与缩放。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 44)
        self.setAccessibleName('CleanDist 磁盘标志')

    def draw(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shade = QColor(color(self, 'accent'))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(shade, 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(QRectF(6, 6, 32, 32), 45 * 16, 285 * 16)
        painter.drawLine(QPointF(29, 15), QPointF(37, 7))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shade)
        painter.drawEllipse(QPointF(37, 7), 2.7, 2.7)
        painter.drawEllipse(QPointF(22, 22), 2, 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        shade.setAlpha(125)
        painter.setPen(QPen(shade, 1.6))
        painter.drawEllipse(QPointF(22, 22), 7, 7)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.draw(painter)

    def icon(self):
        icon = QIcon()
        for size in (32, 64, 128):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.scale(size / 44, size / 44)
            self.draw(painter)
            painter.end()
            icon.addPixmap(pixmap)
        return icon


def button(text, callback, primary=False):
    item = QPushButton(text)
    if primary:
        item.setObjectName('primary')
    item.clicked.connect(callback)
    return item


class SettingsButton(QPushButton):
    """随主题着色的齿轮图标，无需额外图标依赖。"""
    def __init__(self, callback):
        super().__init__()
        self.setObjectName('settingsButton')
        self.setAccessibleName('设置')
        self.setToolTip('设置')
        self.setFixedSize(40, 40)
        self.setIconSize(QSize(22, 22))
        self.clicked.connect(callback)
        self.refresh_icon()

    def refresh_icon(self):
        shape = QPainterPath()
        for point in range(48):
            radius = (8, 8, 10.5, 10.5, 8, 8)[point % 6]
            angle = point * 2 * pi / 48
            position = QPointF(12 + radius * cos(angle), 12 + radius * sin(angle))
            shape.lineTo(position) if point else shape.moveTo(position)
        shape.closeSubpath()
        shape.addEllipse(QRectF(8.5, 8.5, 7, 7))
        shape.setFillRule(Qt.FillRule.OddEvenFill)
        icon = QIcon()
        for size in (24, 48):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.scale(size / 24, size / 24)
            painter.fillPath(shape, QColor(color(self, 'muted')))
            painter.end()
            icon.addPixmap(pixmap)
        self.setIcon(icon)


class ThemeChoice(QComboBox):
    """明确显示下拉入口，并让箭头跟随当前主题。"""
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color(self, 'muted')), 1.6,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() - 15, self.height() / 2
        path = QPainterPath(QPointF(x - 4, y - 2))
        path.lineTo(x, y + 2)
        path.lineTo(x + 4, y - 2)
        painter.drawPath(path)


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
        self.relative = False
        self.setMinimumHeight(145)

    def plot_points(self):
        if self.relative and self.points:
            baseline = self.points[0][1]
            return tuple((stamp, value - baseline) for stamp, value in self.points)
        return self.points

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
        readings = self.plot_points()
        floor = 0
        ceiling = max(max(value for _, value in readings) * 1.15, 1024 * 1024)
        if self.relative:
            low, high = min(value for _, value in readings), max(value for _, value in readings)
            padding = max((high - low) * .1, 1) if high != low else 1024
            floor, ceiling = min(0, low) - padding, max(0, high) + padding
        first, last = readings[0][0], readings[-1][0]
        span = max(last - first, 1)
        painter.setPen(QColor(color(self, 'muted')))
        for fraction in (0, 0.5, 1):
            text_rect = QRectF(0, area.top() + area.height() * fraction - 9, 76, 20)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignRight,
                             format_size(ceiling - (ceiling - floor) * fraction, self.relative))
        painter.drawText(QPointF(area.left(), self.height() - 8), f'{last - first:.0f}秒前')
        painter.drawText(QPointF(area.right() - 30, self.height() - 8), '现在')
        points = [QPointF(area.left() + (timestamp - first) / span * area.width(),
                          area.bottom() - (value - floor) / (ceiling - floor) * area.height()) for timestamp, value in readings]
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
