"""按清理规则分组，跨目录使用真实绝对路径和扫描指纹维护选择。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QListWidget, QListWidgetItem,
                              QVBoxLayout, QWidget)

from ..core.cleanup import CATEGORIES
from .file_tools import record_tree
from .records import RecordGroup, RecordModel, record_key
from .widgets import FocusEdit, button, format_size, label


class CleanupPage(QWidget):
    checkedChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.analysis = None
        self._updating = False
        self._signatures, self._category_keys = {}, {}
        self._allowed, self._blocked = [], []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = label('', 'muted')
        self.summary.setWordWrap(True)
        self.summary.hide()
        layout.addWidget(self.summary)
        body = QHBoxLayout()
        self.categories = QListWidget()
        self.categories.setObjectName('fileModes')
        self.categories.setMinimumWidth(200)
        self.categories.setMaximumWidth(250)
        self.categories.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.addWidget(self.categories)
        content = QVBoxLayout()
        filters = QHBoxLayout()
        self.risk_filter = QComboBox()
        for title, value in (('全部风险', ''), ('低风险', 'low'), ('中风险', 'medium'), ('高风险', 'high')):
            self.risk_filter.addItem(title, value)
        filters.addWidget(self.risk_filter)
        self.search = FocusEdit()
        self.search.setPlaceholderText('搜索名称或路径')
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.recommend_button = button('选择推荐项', self.select_recommended)
        self.clear_button = button('清空选择', self.clear_selection)
        filters.addWidget(self.recommend_button)
        filters.addWidget(self.clear_button)
        content.addLayout(filters)
        self.model = RecordModel(self)
        self.tree = record_tree(self.model)
        content.addWidget(self.tree, 1)
        self.empty_note = label('', 'muted')
        self.empty_note.setWordWrap(True)
        self.empty_note.hide()
        content.addWidget(self.empty_note)
        body.addLayout(content, 1)
        layout.addLayout(body, 1)
        self.categories.currentRowChanged.connect(self.render)
        self.categories.itemChanged.connect(self.select_category)
        self.risk_filter.currentIndexChanged.connect(self.render)
        self.search.returnPressed.connect(self.render)
        self.search.textChanged.connect(lambda text: self.render() if not text else None)
        self.model.checkedChanged.connect(self.selection_changed)

    def set_analysis(self, analysis):
        previous = self.categories.currentItem()
        category = previous.data(Qt.ItemDataRole.UserRole) if previous else ''
        signatures = {record_key(record): (item.key, item.risk, record.fingerprint)
                      for item in analysis.items for record in item.records} if analysis else {}
        retained = [record for key, record in self.model.checked.items()
                    if key in signatures and signatures[key] == self._signatures.get(key)]
        self.analysis, self._signatures = analysis, signatures
        self._allowed = [record for item in analysis.items if item.risk != 'high' for record in item.records] if analysis else []
        self._blocked = analysis.readonly_records() if analysis else []
        self._category_keys = {}
        self.categories.blockSignals(True)
        self.categories.clear()
        if analysis:
            available = {item.category for item in analysis.items}
            for key in ('', *[key for key in CATEGORIES if key in available]):
                items = [item for item in analysis.items if not key or item.category == key]
                cleanable = [item for item in items if item.risk != 'high']
                keys = {record_key(record) for item in cleanable for record in item.records}
                self._category_keys[key] = keys
                row = QListWidgetItem(('全部类别' if not key else CATEGORIES[key]) + '\n' +
                    format_size(sum(item.size for item in (cleanable or items))) +
                    (f' · {len(items)} 个清理项' if cleanable else ' · 仅查看'))
                row.setData(Qt.ItemDataRole.UserRole, key)
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                row.setToolTip('勾选本类别的可清理文件；高风险项仅查看，不计入可清理大小。')
                if keys:
                    row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    row.setCheckState(Qt.CheckState.Unchecked)
                self.categories.addItem(row)
                if key == category:
                    self.categories.setCurrentItem(row)
            if self.categories.currentRow() < 0:
                self.categories.setCurrentRow(0)
            self.summary.setText(f'{len(analysis.items)} 个清理项 · {len(self._allowed):,} 个文件 · 可清理 {format_size(analysis.cleanable_bytes)}')
        else:
            self.summary.clear()
        self.summary.setVisible(analysis is not None)
        self.categories.blockSignals(False)
        self._updating = True
        self.render()
        self.model.replace_checked(retained)
        self._updating = False
        self.selection_changed()

    def render(self, *_):
        row = self.categories.currentItem()
        category = row.data(Qt.ItemDataRole.UserRole) if row else ''
        risk, query = self.risk_filter.currentData(), self.search.text().strip().casefold()
        groups = []
        if self.analysis:
            for item in self.analysis.items:
                if (category and item.category != category) or (risk and item.risk != risk):
                    continue
                records = [record for record in item.records if not query or query in (item.name + ' ' + str(record.path)).casefold()]
                if records:
                    groups.append(RecordGroup(item.key, item.name, records, item.risk, item.reason,
                                              item.category, item.recommended))
        self.model.set_groups(groups, blocked=self._blocked, allowed=self._allowed)
        self.empty_note.setText('暂无清理项' if self.analysis and not groups else '')
        self.empty_note.setVisible(bool(self.empty_note.text()))

    def selection_changed(self):
        if self._updating:
            return
        checked = self.model.checked.keys()
        self.categories.blockSignals(True)
        for index in range(self.categories.count()):
            row = self.categories.item(index)
            if not row.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                continue
            keys = self._category_keys[row.data(Qt.ItemDataRole.UserRole)]
            count = len(keys & checked)
            row.setCheckState(Qt.CheckState.Checked if count == len(keys) else Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked)
        self.categories.blockSignals(False)
        self.recommend_button.setEnabled(bool(self.analysis and any(item.recommended and item.risk != 'high' for item in self.analysis.items)))
        self.clear_button.setEnabled(bool(checked))
        self.checkedChanged.emit()

    def select_category(self, row):
        if not self.analysis or row.checkState() == Qt.CheckState.PartiallyChecked:
            return
        keys = self._category_keys.get(row.data(Qt.ItemDataRole.UserRole), set())
        checked = dict(self.model.checked)
        for record in self._allowed:
            key = record_key(record)
            if key in keys:
                if row.checkState() == Qt.CheckState.Checked:
                    checked[key] = record
                else:
                    checked.pop(key, None)
        self.model.replace_checked(checked.values())

    def clear_selection(self):
        self.model.clear_selection()

    def select_recommended(self):
        records = (record for item in self.analysis.items if item.recommended and item.risk != 'high' for record in item.records) if self.analysis else ()
        self.model.replace_checked(records)

    def selected_records(self):
        return self.model.selected_records()
