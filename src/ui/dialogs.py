"""Explicit cleanup confirmation dialogs."""
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QHeaderView, QTableWidgetItem, QVBoxLayout
from .widgets import button, label, size_text, table


class PreviewDialog(QDialog):
    def __init__(self, plan, parent):
        super().__init__(parent)
        self.setWindowTitle('确认清理预览')
        self.resize(850, 470)
        box = QVBoxLayout(self)
        box.setContentsMargins(22, 22, 22, 22)
        box.addWidget(label('确认移入Windows回收站', 'section'))
        notice = label('以下文件由你手动选择。移入回收站通常不会立即释放磁盘空间；执行前会再次核验文件。', 'muted')
        notice.setWordWrap(True)
        box.addWidget(notice)
        items = table(['完整路径', '逻辑大小', '修改时间'])
        items.setRowCount(len(plan.records))
        for row, record in enumerate(plan.records):
            items.setItem(row, 0, QTableWidgetItem(str(record.path)))
            items.setItem(row, 1, QTableWidgetItem(size_text(record.size)))
            items.setItem(row, 2, QTableWidgetItem(datetime.fromtimestamp(record.modified_ns / 1e9).strftime('%m-%d %H:%M:%S')))
        items.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        items.setColumnWidth(1, 105)
        items.setColumnWidth(2, 140)
        box.addWidget(items)
        box.addWidget(label(f'已选择 {len(plan.records)} 个文件 · 合计逻辑大小 {size_text(sum(r.size for r in plan.records))}'))
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = button('取消', self.reject)
        cancel.setDefault(True)
        actions.addWidget(cancel)
        confirm = button('确认移入回收站', self.accept)
        confirm.setObjectName('danger')
        actions.addWidget(confirm)
        box.addLayout(actions)
