"""直接索引扫描记录，Qt 对象数量不随文件数量增长。"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import os

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QFont

from ..core.batch import record_key
from ..core.cleanup import RISKS
from .widgets import format_size


def _key(value):
    return record_key(value) if hasattr(value, 'path') else os.path.normcase(os.path.abspath(value))


@dataclass
class RecordGroup:
    key: str
    name: str
    records: object
    risk: str = 'medium'
    reason: str = ''
    category: str = ''
    recommended: bool = False
    keep_one: bool = False


class RecordModel(QAbstractItemModel):
    checkedChanged = Signal()
    HEADERS = ('清理项 / 文件路径', '逻辑大小', '文件数', '风险', '推荐与影响', '修改时间', 'AI')
    CACHE_LIMIT = 4096

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups = []
        self.checked = {}
        self.checked_counts = {}
        self.flat = False
        self._keys = []
        self._flat = []
        self._allowed = {}
        self._blocked = set()
        self._locations = {}
        self._selectable = []
        self._group_values = []
        self._display_cache = OrderedDict()
        self._font = self._group_font = None

    def set_groups(self, groups, flat=False, blocked=(), allowed=None):
        previous = self.checked
        self.beginResetModel()
        self.groups, self.flat = list(groups), flat
        self._blocked = {_key(value) for value in blocked}
        self._keys, self._flat, self._locations = [], [], {}
        visible = {}
        for number, group in enumerate(self.groups):
            group.records = tuple(group.records)
            keys = tuple(record_key(record) for record in group.records)
            self._keys.append(keys)
            visible.update(zip(keys, group.records))
            for row, key in enumerate(keys):
                self._locations[key] = (number, row)
                if flat:
                    self._flat.append(self._locations[key])
            if group.risk == 'high':
                self._blocked.update(keys)
        if allowed is None:
            self._allowed = visible
        else:
            self._allowed = {}
            values = allowed.values() if isinstance(allowed, dict) else allowed
            for value in values:
                key = _key(value)
                candidate = value if hasattr(value, 'path') else visible.get(key)
                if candidate is not None:
                    self._allowed[key] = candidate
        self._selectable = [tuple(key for key in keys if self._is_allowed(key)) for keys in self._keys]
        self._group_values = [
            (group.name, format_size(sum(record.size for record in group.records)), str(len(group.records)),
             RISKS.get(group.risk, group.risk), self._impact(group), '') for group in self.groups]
        self._display_cache.clear()
        self.checked = self._validated(previous.values())
        self._count_checked()
        self.endResetModel()
        if self.checked != previous:
            self.checkedChanged.emit()

    @staticmethod
    def _impact(group):
        return ('每组至少保留一份' if group.keep_one else '仅查看' if group.risk == 'high'
                else '推荐清理' if group.recommended else '确认用途后选择')

    def _is_allowed(self, key):
        return key in self._allowed and key not in self._blocked

    def _validated(self, records):
        checked, counts = {}, {}
        for record in records:
            key = record_key(record)
            current = self._allowed.get(key)
            if not self._is_allowed(key) or current.fingerprint != record.fingerprint:
                continue
            location = self._locations.get(key)
            if key in checked:
                continue
            if location:
                number = location[0]
                group = self.groups[number]
                if group.keep_one and counts.get(number, 0) >= len(self._keys[number]) - 1:
                    continue
                counts[number] = counts.get(number, 0) + 1
            checked[key] = current
        return checked

    def _count_checked(self):
        self.checked_counts = {group.key: sum(key in self.checked for key in keys)
                               for group, keys in zip(self.groups, self._keys)}

    def set_fonts(self, font, weight):
        self._font, self._group_font = QFont(font), QFont(font)
        self._group_font.setWeight(QFont.Weight(weight))
        self.layoutChanged.emit()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self._flat) if self.flat else len(self.groups)
        if self.flat or parent.column() or parent.internalId():
            return 0
        return len(self.groups[parent.row()].records) if parent.row() < len(self.groups) else 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= len(self.HEADERS) or row >= self.rowCount(parent):
            return QModelIndex()
        return self.createIndex(row, column, 0 if not parent.isValid() else parent.row() + 1)

    def parent(self, index):
        if not index.isValid() or self.flat or not index.internalId():
            return QModelIndex()
        return self.createIndex(index.internalId() - 1, 0, 0)

    def location(self, index):
        if self.flat:
            return self._flat[index.row()]
        return (index.internalId() - 1, index.row()) if index.internalId() else (index.row(), None)

    def record_at(self, index):
        if not index.isValid():
            return None
        group, row = self.location(index)
        return self.groups[group].records[row] if row is not None else None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if index.column() == 6:
            if role == Qt.ItemDataRole.DisplayRole:
                return '✦'
            if role in (Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleTextRole):
                return 'AI 解读：向 DeepSeek 发送此项的路径、大小和风险摘要，不上传文件内容。'
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
            return None
        number, row = self.location(index)
        group = self.groups[number]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            if not self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable:
                return None
            if row is not None:
                return Qt.CheckState.Checked if self._keys[number][row] in self.checked else Qt.CheckState.Unchecked
            count = self.checked_counts[group.key]
            target = min(len(self._selectable[number]), len(group.records) - int(group.keep_one))
            return (Qt.CheckState.Checked if count == target else Qt.CheckState.PartiallyChecked if count
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.FontRole:
            return self._group_font if row is None else self._font
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (1, 2):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if row is None:
            return self._group_values[number][index.column()] if role == Qt.ItemDataRole.DisplayRole else group.reason
        cache_key = (number, row)
        cached = self._display_cache.get(cache_key)
        if cached is None:
            record = group.records[row]
            path = str(record.path)
            try:
                stamp = datetime.fromtimestamp(record.modified_ns / 1e9).strftime('%Y-%m-%d %H:%M:%S')
            except (OSError, OverflowError, ValueError):
                stamp = '不可用'
            cached = ((path, format_size(record.size), '1', RISKS.get(group.risk, group.risk),
                       self._impact(group), stamp), path + '\n' + group.reason)
            self._display_cache[cache_key] = cached
            if len(self._display_cache) > self.CACHE_LIMIT:
                self._display_cache.popitem(last=False)
        else:
            self._display_cache.move_to_end(cache_key)
        return cached[0][index.column()] if role == Qt.ItemDataRole.DisplayRole else cached[1]

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        number, row = self.location(index)
        selectable = (bool(self._selectable[number]) and
                      (not self.groups[number].keep_one or len(self.groups[number].records) > 1)) if row is None else self._is_allowed(self._keys[number][row])
        if index.column() == 0 and selectable:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or not self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable:
            return False
        selected = value == Qt.CheckState.Checked or value == Qt.CheckState.Checked.value
        number, row = self.location(index)
        group = self.groups[number]
        keys = self._selectable[number] if row is None else (self._keys[number][row],)
        count = self.checked_counts[group.key]
        limit = len(group.records) - int(group.keep_one)
        if selected and row is not None and keys[0] not in self.checked and count >= limit:
            return False
        # 全组选副本时优先保留第一份；只读文件也可以充当保留副本。
        if selected and row is None and group.keep_one and len(keys) == len(group.records):
            keys = tuple(key for key in keys if key != self._keys[number][0] or key in self.checked)
        changed = False
        for key in keys:
            if selected and key not in self.checked and count < limit:
                self.checked[key] = self._allowed[key]
                count += 1
                changed = True
            elif not selected and key in self.checked:
                del self.checked[key]
                count -= 1
                changed = True
        if changed:
            self.checked_counts[group.key] = count
            self._notify(number, row)
            self.checkedChanged.emit()
        return True

    def _notify(self, number=None, row=None):
        roles = [Qt.ItemDataRole.CheckStateRole]
        if self.flat:
            if self.rowCount():
                self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, 0), roles)
            return
        numbers = range(len(self.groups)) if number is None else (number,)
        for current in numbers:
            parent = self.index(current, 0)
            self.dataChanged.emit(parent, parent, roles)
            total = self.rowCount(parent)
            if total:
                first, last = (row, row) if row is not None else (0, total - 1)
                self.dataChanged.emit(self.index(first, 0, parent), self.index(last, 0, parent), roles)

    def replace_checked(self, records):
        checked = self._validated(records)
        if checked == self.checked:
            return
        self.checked = checked
        self._count_checked()
        self._notify()
        self.checkedChanged.emit()

    def clear_selection(self):
        self.replace_checked(())

    def selected_records(self):
        return list(self.checked.values())
