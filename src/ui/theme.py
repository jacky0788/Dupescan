# 深色主題：配色、字級與全域樣式（Fusion 樣式＋深色調色盤＋StyleSheet）。
#
# 版面上的文字角色用 QLabel 的 "role" 屬性指定（title／heading／muted／faint／error／warning／success），
# 卡片與提示框用 QFrame 的 "card"／"banner" 屬性；見 set_role()。
from pathlib import Path

from PyQt6.QtCore import QDir
from PyQt6.QtGui import QColor, QFont, QIcon, QPalette

# ── 配色 ─────────────────────────────────────────────────────────────────
BG        = "#121418"   # 視窗底色
SURFACE   = "#1a1d23"   # 卡片、清單、輸入框
SURFACE_2 = "#22262e"   # 按鈕、滑過
SURFACE_3 = "#2a2f38"   # 按下、選取
BORDER    = "#2c313a"
BORDER_2  = "#3a404b"   # 滑過時的邊框
TEXT      = "#e7e9ee"
TEXT_2    = "#a3a9b5"   # 次要文字
TEXT_3    = "#6d7380"   # 提示文字
ACCENT    = "#5b8def"
ACCENT_2  = "#74a0f5"   # 滑過
ACCENT_BG = "#1c2a45"   # 選取的晶片、選取列
DANGER    = "#e5534b"
DANGER_2  = "#ee6d66"
SUCCESS   = "#46c08a"
WARNING   = "#e3b341"

# 結果清單的「保留／移除／保護」膠囊：(底色, 文字)
PILL_KEEP      = ("#1f3b2e", "#7ee2ae")
PILL_REMOVE    = ("#43211f", "#ffa69e")
PILL_PROTECTED = ("#1e2c45", "#9cc0ff")

FONT_FAMILIES = ["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI", "PingFang TC",
                 "Noto Sans CJK TC", "sans-serif"]
ICON_DIR = Path(__file__).resolve().parent / "icons"
FONT_SIZES = {"small": ("小", 12), "normal": ("標準", 13), "large": ("大", 15), "xlarge": ("特大", 17)}
DEFAULT_FONT = "normal"


def font_px(key: str) -> int:
    return FONT_SIZES.get(key, FONT_SIZES[DEFAULT_FONT])[1]


def stylesheet(px: int = 13) -> str:
    """全域 StyleSheet；px 是內文字級（設定裡的文字大小）。"""
    title, heading, small = px + 9, px + 3, max(px - 1, 11)
    families = f'"{FONT_FAMILIES[0]}"'
    return f"""
QWidget {{ color: {TEXT}; font-family: {families}; font-size: {px}px; }}
QMainWindow, QDialog, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QToolTip {{ background: {SURFACE_3}; color: {TEXT}; border: 1px solid {BORDER_2}; border-radius: 6px;
            padding: 5px 8px; }}

QLabel {{ background: transparent; }}
QLabel[role="title"]   {{ font-size: {title}px; font-weight: 700; }}
QLabel[role="heading"] {{ font-size: {heading}px; font-weight: 600; }}
QLabel[role="section"] {{ color: {TEXT_2}; font-weight: 600; }}
QLabel[role="muted"]   {{ color: {TEXT_2}; }}
QLabel[role="faint"]   {{ color: {TEXT_3}; font-size: {small}px; }}
QLabel[role="error"]   {{ color: {DANGER_2}; }}
QLabel[role="warning"] {{ color: {WARNING}; }}
QLabel[role="success"] {{ color: {SUCCESS}; }}
QLabel[role="brand"]   {{ font-size: {heading}px; font-weight: 700; color: {TEXT}; }}

QFrame[card="true"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame[banner="warning"] {{ background: #2a2414; border: 1px solid #5c4a1a; border-radius: 10px; }}
QFrame[banner="info"]    {{ background: #172238; border: 1px solid #2b4372; border-radius: 10px; }}
QFrame[banner="error"]   {{ background: #2c1716; border: 1px solid #6b2a26; border-radius: 10px; }}
QFrame#toast {{ background: {SURFACE_3}; border: 1px solid {BORDER_2}; border-radius: 10px; }}
QFrame#bottomBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; }}
QFrame#topBar {{ background: {BG}; border-bottom: 1px solid {BORDER}; }}
QFrame[divider="true"] {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px;
               padding: 7px 16px; }}
QPushButton:hover {{ background: {SURFACE_3}; border-color: {BORDER_2}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {TEXT_3}; background: {SURFACE}; border-color: {BORDER}; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton#primary {{ background: {ACCENT}; color: #ffffff; border: none; font-weight: 600; padding: 8px 20px; }}
QPushButton#primary:hover {{ background: {ACCENT_2}; }}
QPushButton#primary:disabled {{ background: {SURFACE_2}; color: {TEXT_3}; }}
QPushButton#danger {{ background: {DANGER}; color: #ffffff; border: none; font-weight: 600; padding: 8px 20px; }}
QPushButton#danger:hover {{ background: {DANGER_2}; }}
QPushButton#danger:disabled {{ background: {SURFACE_2}; color: {TEXT_3}; }}
QPushButton[big="true"] {{ font-size: {heading}px; padding: 10px 32px; border-radius: 10px; }}
QPushButton#link {{ background: transparent; border: none; color: {ACCENT_2}; padding: 2px 4px; }}
QPushButton#link:hover {{ color: {TEXT}; text-decoration: underline; }}
QPushButton#icon {{ background: transparent; border: none; border-radius: 6px; padding: 4px 8px;
                    color: {TEXT_2}; font-size: {heading}px; }}
QPushButton#icon:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QPushButton#chip {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 13px; padding: 4px 14px;
                    min-height: 18px; color: {TEXT_2}; }}
QPushButton#chip:hover {{ border-color: {BORDER_2}; color: {TEXT}; }}
QPushButton#chip:checked {{ background: {ACCENT_BG}; border-color: {ACCENT}; color: #cfe0ff; }}
QPushButton#expander {{ background: transparent; border: none; color: {TEXT_2}; padding: 4px 0;
                        text-align: left; font-weight: 600; }}
QPushButton#expander:hover {{ color: {TEXT}; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 10px;
    selection-background-color: {ACCENT}; selection-color: #ffffff; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QPlainTextEdit:hover {{ border-color: {BORDER_2}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url(icons:chevron-down.svg); width: 12px; height: 12px; margin-right: 10px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER_2}; border-radius: 8px;
                               padding: 4px; outline: none; selection-background-color: {ACCENT_BG};
                               selection-color: {TEXT}; }}

QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_3}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid #4a515e; border-radius: 4px;
                        background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(icons:check.svg); }}
QCheckBox::indicator:disabled {{ background: {SURFACE_2}; border-color: {BORDER}; }}
QCheckBox::indicator:checked:disabled {{ image: url(icons:check-disabled.svg); }}
QRadioButton::indicator {{ width: 16px; height: 16px; border: 1px solid #4a515e; border-radius: 9px;
                           background: {SURFACE}; }}
QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QRadioButton::indicator:checked {{ border-color: {ACCENT}; background: qradialgradient(cx:0.5, cy:0.5,
    radius:0.5, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.32 #ffffff, stop:0.42 {ACCENT}, stop:1 {ACCENT}); }}

QProgressBar {{ background: {SURFACE_2}; border: none; border-radius: 4px; max-height: 8px; min-height: 8px;
                text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QTreeView, QListWidget, QTableWidget {{ background: {SURFACE}; alternate-background-color: #1d2027;
    border: 1px solid {BORDER}; border-radius: 10px; outline: none; }}
QTreeView::item, QListWidget::item {{ padding: 4px 2px; border: none; }}
QTreeView::item:hover, QListWidget::item:hover {{ background: {SURFACE_2}; }}
QTreeView::item:selected, QListWidget::item:selected, QTableWidget::item:selected {{
    background: {ACCENT_BG}; color: {TEXT}; }}
QTreeView::branch {{ background: transparent; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {SURFACE}; color: {TEXT_2}; border: none; border-bottom: 1px solid {BORDER};
                        padding: 7px 8px; font-weight: 600; }}
QTableWidget {{ gridline-color: {BORDER}; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_2}; border-radius: 4px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_3}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_2}; border-radius: 4px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_3}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER_2}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 28px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_BG}; }}
QMenu::item:disabled {{ color: {TEXT_3}; }}
QMenu::separator {{ height: 1px; background: {BORDER_2}; margin: 5px 8px; }}

QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {TEXT_2}; }}
QFrame#warnbox {{ background: #2a2414; border: 1px solid #5c4a1a; border-radius: 10px; }}
"""


def palette() -> QPalette:
    """Fusion 用的深色調色盤（核取方塊、捲軸等沒在 StyleSheet 指定的部分會用到）。"""
    pal = QPalette()
    role, group = QPalette.ColorRole, QPalette.ColorGroup
    for r, color in [(role.Window, BG), (role.WindowText, TEXT), (role.Base, SURFACE),
                     (role.AlternateBase, "#1d2027"), (role.Text, TEXT), (role.Button, SURFACE_2),
                     (role.ButtonText, TEXT), (role.BrightText, "#ffffff"), (role.Highlight, ACCENT),
                     (role.HighlightedText, "#ffffff"), (role.ToolTipBase, SURFACE_3),
                     (role.ToolTipText, TEXT), (role.PlaceholderText, TEXT_3), (role.Link, ACCENT_2),
                     (role.Light, BORDER_2), (role.Midlight, BORDER), (role.Mid, SURFACE_3),
                     (role.Dark, "#0c0d10"), (role.Shadow, "#000000")]:
        pal.setColor(r, QColor(color))
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, QColor(TEXT_3))
    return pal


def icon(name: str) -> QIcon:
    """src/ui/icons 裡的圖示（不含副檔名）。"""
    return QIcon(str(ICON_DIR / f"{name}.svg"))


def apply(app, px: int = 13):
    """把主題套到整個應用程式（啟動時與變更文字大小時呼叫）。"""
    QDir.setSearchPaths("icons", [str(ICON_DIR)])
    app.setStyle("Fusion")
    app.setPalette(palette())
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(px)
    app.setFont(font)
    app.setStyleSheet(stylesheet(px))


def set_role(widget, role: str):
    """設定 QLabel 的文字角色（title、muted…），已經顯示的元件會立刻更新樣式。"""
    widget.setProperty("role", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


# ── 舊介面相容（新介面接手後移除）──────────────────────────────────────────
PANEL, SURFACE_HI, BORDER_HI = SURFACE, SURFACE_3, BORDER_2
SUBTEXT, FAINT, ACCENT_HI = TEXT_2, TEXT_3, ACCENT_2
GREEN, DANGER_HI, ORANGE = SUCCESS, DANGER_2, WARNING
STYLE = stylesheet(13)
