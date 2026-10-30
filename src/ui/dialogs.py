"""清理操作的明确确认对话框。"""
from datetime import datetime
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QHeaderView, QMessageBox, QPlainTextEdit, QTableWidgetItem, QVBoxLayout
from ..core.cleanup import RISKS
from .widgets import button, label, format_size, table


class PreviewDialog(QDialog):
    def __init__(self, plan, parent):
        super().__init__(parent)
        self.setWindowTitle('清理预览')
        self.resize(850, 470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addWidget(label('确认移入回收站', 'section'))
        notice = label('仅将预览清单中的文件移入回收站，文件夹本身保留。移入回收站不会立即释放磁盘空间。', 'muted')
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.risk_notes = plan.risk_notes
        if self.risk_notes:
            notes = QPlainTextEdit()
            notes.setReadOnly(True)
            notes.setMaximumHeight(110)
            notes.setPlainText('\n'.join(dict.fromkeys(f'{RISKS[risk]} · {name}：{reason}' for _, name, risk, reason in self.risk_notes)))
            layout.addWidget(notes)
        file_table = table(['完整路径', '逻辑大小', '修改时间'])
        file_table.setWordWrap(False)
        file_table.setRowCount(len(plan.records))
        for row, record in enumerate(plan.records):
            file_table.setItem(row, 0, QTableWidgetItem(str(record.path)))
            file_table.setItem(row, 1, QTableWidgetItem(format_size(record.size)))
            file_table.setItem(row, 2, QTableWidgetItem(datetime.fromtimestamp(record.modified_ns / 1e9).strftime('%m-%d %H:%M:%S')))
        file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        file_table.setColumnWidth(1, 105)
        file_table.setColumnWidth(2, 140)
        layout.addWidget(file_table)
        layout.addWidget(label(f'已选择 {len(plan.records)} 个文件 · 合计逻辑大小 {format_size(sum(record.size for record in plan.records))}'))
        actions = QHBoxLayout()
        actions.addStretch()
        cancel_button = button('取消', self.reject)
        cancel_button.setDefault(True)
        actions.addWidget(cancel_button)
        confirm_button = button('确认移入回收站', self.accept)
        confirm_button.setObjectName('danger')
        actions.addWidget(confirm_button)
        layout.addLayout(actions)

    def accept(self):
        if any(risk == 'medium' for _, _, risk, _ in self.risk_notes):
            answer = QMessageBox.question(self, '确认中风险清理',
                '选择中包含需要谨慎处理的内容，可能影响离线安装、重新构建或故障排查。确认已阅读上方影响说明并继续？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().accept()
