# 深色主題：配色常數與全域 Qt StyleSheet（主視窗與各對話框共用）

# ── 現代化深色主題配色 ────────────────────────────────────────────────────────
BG        = "#0f1117"   # 視窗最底層背景
PANEL     = "#161922"   # 面板 / 輸入框背景
SURFACE   = "#1e222e"   # 卡片表面、按鈕一般狀態
SURFACE_HI= "#272c3a"   # 表面 hover / 次要強調
BORDER    = "#2a2f3c"   # 邊框
BORDER_HI = "#3a4150"   # 強調邊框
TEXT      = "#e4e7ee"   # 主文字
SUBTEXT   = "#9aa3b2"   # 次要文字
FAINT     = "#6b7280"   # 更淡的提示文字
ACCENT    = "#6c8cff"   # 主強調（靛藍）
ACCENT_HI = "#88a2ff"   # 主強調 hover
GREEN     = "#4ade80"   # 成功 / SSD
DANGER    = "#f87171"   # 危險 / 刪除
DANGER_HI = "#fb8a8a"
ORANGE    = "#fbbf24"   # 路徑 / 警示

# 全域 Qt StyleSheet
STYLE = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QToolTip {{
    background-color: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 4px 8px;
}}
QLabel {{ background: transparent; }}

QPushButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background-color: {SURFACE_HI}; border-color: {BORDER_HI}; }}
QPushButton:pressed {{ background-color: {ACCENT}; color: {BG}; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {FAINT}; background-color: {PANEL}; }}

QPushButton#primary {{
    background-color: {ACCENT};
    color: #0b0d14;
    font-weight: 600;
    border: none;
}}
QPushButton#primary:hover {{ background-color: {ACCENT_HI}; }}
QPushButton#primary:pressed {{ background-color: #5a78e0; }}

QPushButton#danger {{
    background-color: {DANGER};
    color: #1a0d0d;
    font-weight: 600;
    border: none;
}}
QPushButton#danger:hover {{ background-color: {DANGER_HI}; }}
QPushButton#danger:disabled {{ background-color: {PANEL}; color: {FAINT}; }}

QLineEdit, QSpinBox, QComboBox {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #0b0d14;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {BORDER_HI}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: none; border-left: 4px solid transparent;
    border-right: 4px solid transparent; border-top: 5px solid {SUBTEXT}; margin-right: 8px; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 8px;
    color: {TEXT};
    padding: 4px;
    selection-background-color: {SURFACE_HI};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none;
    background: {SURFACE}; }}
QSpinBox::up-button {{ border-top-right-radius: 7px; }}
QSpinBox::down-button {{ border-bottom-right-radius: 7px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {SURFACE_HI}; }}

QProgressBar {{
    background-color: {PANEL};
    border: none;
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
    height: 10px;
    font-size: 11px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 6px;
}}

QTreeWidget {{
    background-color: {PANEL};
    alternate-background-color: #13161e;
    border: 1px solid {BORDER};
    border-radius: 12px;
    outline: none;
    padding: 4px;
}}
QTreeWidget::item {{
    padding: 5px 4px;
    border-radius: 6px;
}}
QTreeWidget::item:hover {{ background-color: {SURFACE}; }}
QTreeWidget::item:selected {{ background-color: {SURFACE_HI}; color: {TEXT}; }}
QHeaderView::section {{
    background-color: {PANEL};
    color: {SUBTEXT};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 12px;
}}
QTreeWidget::indicator, QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1.5px solid {BORDER_HI};
    border-radius: 5px;
    background: {PANEL};
}}
QTreeWidget::indicator:checked, QCheckBox::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
    image: url(none);
}}
QTreeWidget::indicator:indeterminate {{ background: {SURFACE_HI}; border-color: {ACCENT}; }}
QTreeWidget::branch {{ background: transparent; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}

QStatusBar {{
    background-color: {PANEL};
    border-top: 1px solid {BORDER};
    color: {SUBTEXT};
}}
QStatusBar::item {{ border: none; }}

QCheckBox {{ spacing: 6px; color: {TEXT}; }}
QCheckBox:disabled {{ color: {FAINT}; }}
QCheckBox::indicator:disabled {{ background: {SURFACE}; border-color: {BORDER}; }}

QMenu {{
    background-color: {PANEL};
    border: 1px solid {BORDER_HI};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{ padding: 6px 24px 6px 14px; border-radius: 6px; color: {TEXT}; }}
QMenu::item:selected {{ background-color: {ACCENT}; color: #0b0d14; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::item:disabled {{ color: {FAINT}; }}

QTableWidget {{
    background-color: {PANEL};
    alternate-background-color: #13161e;
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: {BORDER};
    selection-background-color: {SURFACE_HI};
    selection-color: {TEXT};
}}
QRadioButton {{ spacing: 6px; color: {TEXT}; }}
QRadioButton::indicator {{
    width: 14px; height: 14px; border-radius: 8px;
    border: 1.5px solid {BORDER_HI}; background: {PANEL};
}}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QFrame#warnbox {{
    background: #1d1a12; border: 1px solid #5c4a1a; border-radius: 10px;
}}
"""
