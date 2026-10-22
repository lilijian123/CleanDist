"""可复用的 Qt 控件、格式化工具与内存图表。"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from .theme import MUTED, TEAL


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


class NumberItem(QTableWidgetItem):
    def __init__(self, text, number=None):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole + 1, number if number is not None else float('-inf'))
        self.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole + 1) < other.data(Qt.ItemDataRole.UserRole + 1)


class Metric(QFrame):
    def __init__(self, title):
        super().__init__()
        self.setObjectName('card')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 13, 18, 13)
        layout.addWidget(label(title, 'muted'))
        self.value = label('—', 'metric')
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
        painter.setPen(QPen(QColor('#2b3a50'), 1))
        for fraction in (0, 0.5, 1):
            y = area.top() + area.height() * fraction
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        if not self.points:
            painter.setPen(QColor(MUTED))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, '选择进程查看趋势')
            return
        ceiling = max(max(value for _, value in self.points) * 1.15, 1024 * 1024)
        first, last = self.points[0][0], self.points[-1][0]
        span = max(last - first, 1)
        painter.setPen(QColor(MUTED))
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
        gradient.setColorAt(0, QColor(83, 223, 191, 75))
        gradient.setColorAt(1, QColor(83, 223, 191, 2))
        painter.fillPath(fill, gradient)
        painter.setPen(QPen(QColor(TEAL), 2))
        painter.drawPath(path)
        painter.setBrush(QColor(TEAL))
        painter.drawEllipse(points[-1], 3, 3)
