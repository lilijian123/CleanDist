"""Shared application palette and Qt styles."""
TEAL = '#53dfbf'

MUTED = '#8b9bb1'

STYLE = """
QWidget { background: #0d1421; color: #dce6f4; font-size: 13px; }
QMainWindow, QDialog { background: #0d1421; }
QLabel { background: transparent; }
QLabel#title { font-size: 27px; font-weight: 700; color: #f1f6fc; }
QLabel#brand { background: #203c3c; color: #70f4d4; font-size: 25px; font-weight: 800; border-radius: 14px; }
QLabel#muted { color: #8b9bb1; }
QLabel#pill { color: #69e9c9; background: #18332f; border: 1px solid #265346; border-radius: 12px; padding: 6px 12px; }
QFrame#card { background: #151f30; border: 1px solid #253247; border-radius: 13px; }
QFrame#card QLabel { background: transparent; }
QLabel#metric { font-size: 27px; font-weight: 700; color: #eef5ff; }
QLabel#section { font-size: 16px; font-weight: 650; }
QPushButton { background: #1b293e; border: 1px solid #34445b; border-radius: 7px; padding: 8px 15px; font-weight: 600; }
QPushButton:hover { background: #293e57; border-color: #6a829e; }
QPushButton:disabled { color: #65758b; background: #151e2d; border-color: #273447; }
QPushButton#primary { background: #50dbb7; color: #0a2821; border-color: #50dbb7; }
QPushButton#primary:hover { background: #7aeace; }
QPushButton#primary:disabled { background: #23473f; color: #769e93; border-color: #23473f; }
QPushButton#danger { background: #83444e; border-color: #b66b76; color: #fff1f2; }
QLineEdit { border: 1px solid #32425a; background: #121d2e; padding: 9px; border-radius: 7px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { background: transparent; color: #91a3ba; padding: 12px 24px; margin-right: 8px; border-bottom: 2px solid transparent; font-size: 14px; }
QTabBar::tab:selected { color: #67e9ca; border-bottom: 2px solid #53dfbf; }
QTableWidget { background: #111b2b; alternate-background-color: #152133; border: 1px solid #27364c; border-radius: 8px; gridline-color: #233047; selection-background-color: #214d4a; selection-color: #e5fff6; }
QTableWidget::item { padding: 5px 10px; border: 0; }
QTableWidget::item:selected { background: #214d4a; }
QHeaderView::section { background: #1b283b; color: #a7b9d0; padding: 9px; border: 0; border-bottom: 1px solid #344258; font-weight: 600; }
QTableCornerButton::section { background: #1b283b; border: 0; }
QScrollBar:vertical { background: #131e2e; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4d68; min-height: 24px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar { border: 0; border-radius: 3px; background: #253449; max-height: 6px; }
QProgressBar::chunk { background: #53dfbf; border-radius: 3px; }
QTextEdit { background: #111b2b; border: 1px solid #32425a; border-radius: 7px; padding: 8px; }
QCheckBox { spacing: 6px; }
QStatusBar { color: #8b9bb1; border-top: 1px solid #253247; }
"""
