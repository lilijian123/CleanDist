"""应用共用的配色与 Qt 样式。"""
TEAL = '#a8c8b5'

MUTED = '#8b8e87'

STYLE = """
QWidget { background: #151515; color: #d6d6d1; font-size: 10pt; font-weight: 360; }
QMainWindow, QDialog { background: #151515; }
QLabel { background: transparent; }
QLabel#title { font-size: 27px; font-weight: 660; color: #e5e5df; }
QLabel#brand { background: #20201e; color: #b8d1bf; font-size: 25px; font-weight: 760; border-radius: 14px; }
QLabel#muted { color: #8b8e87; }
QLabel#pill { color: #a8c8b5; background: #20201e; border: 0; border-radius: 12px; padding: 6px 12px; }
QFrame#card { background: #1a1a19; border: 0; border-radius: 13px; }
QFrame#card QLabel { background: transparent; }
QLabel#metric { font-size: 27px; font-weight: 660; color: #e5e5df; }
QLabel#section { font-size: 16px; font-weight: 610; }
QPushButton { background: #20201e; border: 0; border-radius: 7px; padding: 9px 16px; font-weight: 560; }
QPushButton:hover { background: #272725; }
QPushButton:pressed { background: #111110; }
QPushButton:disabled { color: #666861; background: #1a1a19; }
QPushButton#primary { background: #b5cbb9; color: #1b221c; }
QPushButton#primary:hover { background: #c5d9c9; }
QPushButton#primary:disabled { background: #222923; color: #747e73; }
QPushButton#danger { background: #694244; color: #eedcdb; }
QLineEdit { border: 0; background: #1a1a19; padding: 10px; border-radius: 7px; selection-background-color: #3c493e; }
QLineEdit:focus { background: #171716; }
QLineEdit#filePath { background: transparent; color: #8b8e87; padding: 3px 8px; }
QComboBox { background: #1a1a19; border: 0; padding: 9px; border-radius: 7px; min-width: 90px; }
QComboBox::drop-down { border: 0; width: 23px; }
QComboBox QAbstractItemView { background: #20201e; border: 0; selection-background-color: #2b302a; }
QMenu { background: #20201e; border: 0; padding: 6px; }
QMenu::item { padding: 7px 20px; border-radius: 5px; }
QMenu::item:selected { background: #2b302a; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: transparent; color: #8b8e87; padding: 12px 24px; margin-right: 8px; border: 0; font-size: 14px; }
QTabBar::tab:selected { color: #c6d8c8; background: #1a1a19; border-radius: 8px; }
QTableView, QTreeView { background: #151515; alternate-background-color: #1a1a19; border: 0; border-radius: 8px; selection-background-color: #293029; selection-color: #e1e8de; }
QTableView::item, QTreeView::item { padding: 5px 10px; border: 0; }
QTreeView::item { height: 28px; padding: 3px 10px; }
QTableView::item:selected, QTreeView::item:selected { background: #293029; }
QHeaderView::section { background: #20201e; color: #a4a89d; padding: 10px; border: 0; font-weight: 560; }
QTableCornerButton::section { background: #20201e; border: 0; }
QScrollBar:vertical { background: #151515; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3b3d37; min-height: 36px; border-radius: 5px; }
QScrollBar::handle:hover { background: #55584e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #151515; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #3b3d37; min-width: 36px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page, QAbstractScrollArea::corner { background: transparent; }
QProgressBar { border: 0; border-radius: 3px; background: #20201e; max-height: 6px; }
QProgressBar::chunk { background: #a8c8b5; border-radius: 3px; }
QTextEdit { background: #1a1a19; border: 0; border-radius: 7px; padding: 9px; }
QCheckBox { spacing: 6px; }
QStatusBar { color: #8b8e87; border: 0; }
"""


# 浅色沿用相同布局，只替换颜色和暗色补偿字重。
LIGHT_STYLE = STYLE
for dark, light in {
    '#151515': '#fcfcfb', '#d6d6d1': '#30302d', '#e5e5df': '#242422',
    '#20201e': '#efefec', '#b8d1bf': '#38654e', '#8b8e87': '#74746d',
    '#a8c8b5': '#47785c', '#1a1a19': '#f5f5f3', '#272725': '#e7e7e2',
    '#111110': '#ddddd7', '#666861': '#999991', '#b5cbb9': '#d7e5d9',
    '#1b221c': '#294733', '#c5d9c9': '#c7dccb', '#222923': '#e9ede7',
    '#747e73': '#8b9588', '#694244': '#f1dcdc', '#eedcdb': '#853d42',
    '#3c493e': '#d5e3d6', '#171716': '#f0f0ed', '#2b302a': '#e2e8df',
    '#c6d8c8': '#365c43', '#293029': '#e0e9df', '#e1e8de': '#253d2b',
    '#a4a89d': '#666a60', '#3b3d37': '#cecec7', '#55584e': '#adada4',
}.items():
    LIGHT_STYLE = LIGHT_STYLE.replace(dark, light)
for weight in (760, 660, 610, 560, 360):
    LIGHT_STYLE = LIGHT_STYLE.replace(f'font-weight: {weight};', f'font-weight: {weight + 40};')


def color(widget, name):
    """手绘控件读取所属窗口的主题，避免多个窗口互相污染。"""
    while widget is not None and widget.property('theme') is None:
        widget = widget.parent()
    light = widget is not None and widget.property('theme') == 'light'
    return {
        'accent': ('#a8c8b5', '#47785c'), 'muted': ('#8b8e87', '#74746d'),
        'grid': ('#292b26', '#e5e5df'), 'positive': ('#c9ad8e', '#99632d'),
        'focus_outer': ('#0b0b0a', '#ddddd7'), 'focus_inner': ('#3c4239', '#bfc6b9'),
    }[name][int(light)]
