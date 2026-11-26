from dataclasses import asdict, dataclass, replace
from pathlib import Path
from string import Template

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from darksort.settings import DEFAULT_THEME


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    menu: str
    surface: str
    inset: str
    stripe: str
    raised: str
    border: str
    strong: str
    text: str
    muted: str
    dim: str
    accent: str
    on_accent: str
    selection: str
    dark: bool = True

    def tint(self, color: str, amount: float = 0.18) -> QColor:
        base, hue = QColor(self.surface), QColor(color)
        return QColor.fromRgbF(
            *(
                start + (end - start) * amount
                for start, end in zip(base.getRgbF()[:3], hue.getRgbF()[:3], strict=True)
            )
        )


FOUNDRY = Theme(
    name="铁炉深色",
    background="#171f2a",
    menu="#222b37",
    surface="#1c2530",
    inset="#171f29",
    stripe="#202a36",
    raised="#26323f",
    border="#2c3947",
    strong="#405062",
    text="#e7edf4",
    muted="#a1adbd",
    dim="#77879b",
    accent="#d39047",
    on_accent="#151c24",
    selection="#403931",
)
THEMES = {
    DEFAULT_THEME: Theme(
        name="夜幕蓝",
        background="#101624",
        menu="#141d2d",
        surface="#192337",
        inset="#121b2b",
        stripe="#1d293f",
        raised="#25354e",
        border="#2b3b53",
        strong="#3a506d",
        text="#e8effa",
        muted="#a5b7cf",
        dim="#879cb7",
        accent="#72d9c7",
        on_accent="#0c2828",
        selection="#21413f",
    ),
    "foundry": FOUNDRY,
    "ember": replace(
        FOUNDRY,
        name="余烬深色",
        background="#221b1e",
        menu="#30262a",
        surface="#291f22",
        inset="#21181c",
        stripe="#302327",
        raised="#3b2b30",
        border="#423238",
        strong="#594047",
        text="#f0e6e5",
        muted="#bca6ac",
        dim="#987d85",
        accent="#e08466",
        on_accent="#261b1b",
        selection="#492f2e",
    ),
    "ice": replace(
        FOUNDRY,
        name="寒霜深色",
        background="#132029",
        menu="#1c2d3a",
        surface="#182733",
        inset="#12212b",
        stripe="#1d303d",
        raised="#243a49",
        border="#2b4050",
        strong="#446174",
        text="#e4f2f6",
        muted="#a0b9c7",
        dim="#7794a5",
        accent="#81cce1",
        on_accent="#10242d",
        selection="#263f4b",
    ),
    "paper": Theme(
        name="羊皮纸浅色",
        background="#edf0f1",
        menu="#e1e6e9",
        surface="#f7f8f8",
        inset="#edf1f2",
        stripe="#edf1f3",
        raised="#e4e9ec",
        border="#d0d8df",
        strong="#aab8c5",
        text="#263647",
        muted="#4b6177",
        dim="#627383",
        accent="#9b5628",
        on_accent="#ffffff",
        selection="#eddfd1",
        dark=False,
    ),
}

STYLE = Template("""
QWidget { color: $text; font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 12px; }
QMainWindow, QDialog { background: $background; }
QMenuBar { background: $menu; padding: 5px 8px; border-bottom: 1px solid $border; }
QMenuBar::item { background: transparent; color: $muted; padding: 6px 10px; border-radius: 5px; }
QMenuBar::item:selected, QMenuBar::item:pressed { background: $raised; color: $text; }
QMenu { background: $surface; border: 1px solid $strong; padding: 4px; }
QMenu::item { padding: 5px 22px 5px 10px; }
QMenu::item:selected { background: $selection; }
QMenu::separator { height: 1px; background: $border; margin: 3px 5px; }
QMenu::item:disabled { color: $dim; }
QLabel { background: transparent; }
QLabel#muted { color: $muted; }
QLabel#wordmark { color: $text; font-size: 16px; font-weight: 740; }
QLabel#panelTitle { font-size: 14px; font-weight: 650; }
QLabel#section { font-size: 14px; font-weight: 650; }
QLabel#problemBadge {
    font-weight: 650; font-size: 11px; padding: 3px 6px; border-radius: 4px;
    color: $muted; background: $raised;
}
QLabel#problemBadge[level="warning"] { color: $problem_yellow; background: $warning_tint; }
QLabel#problemBadge[level="error"] { color: $problem_red; background: $error_tint; }
QFrame#problemCard { background: $inset; border: 1px solid $border; border-radius: 8px; }
QLabel#detailTitle { font-size: 19px; font-weight: 700; }
QLabel#badge, QLabel#countBadge, QLabel#priorityPill {
    color: $accent; padding: 3px 7px; background: $accent_tint; border-radius: 5px;
    font-weight: 600;
}
QLabel#countBadge, QLabel#priorityPill { font-size: 11px; }
QLabel#priorityPill { color: $gold; background: $gold_tint; }
QLabel#smallCaps { color: $dim; font-size: 11px; font-weight: 600; }
QLabel#factLabel { color: $muted; }
QFrame#toolbar {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 $raised, stop:0.53 $surface, stop:1 $surface);
    border: 1px solid $border; border-radius: 10px;
}
QFrame#panel { background: $surface; border: 1px solid $border; border-radius: 10px; }
QFrame#paneHeader {
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 $raised, stop:1 $surface);
    border-bottom: 1px solid $border; border-top-left-radius: 9px; border-top-right-radius: 9px;
}
QFrame#paneFooter {
    background: $inset; border-top: 1px solid $border;
    border-bottom-left-radius: 9px; border-bottom-right-radius: 9px;
}
QFrame#paneFilters, QFrame#dockBody { background: $surface; }
QFrame#relationNavigation { background: $inset; border-bottom: 1px solid $border; }
QFrame#factsCard { background: $inset; border: 1px solid $border; border-radius: 8px; }
QScrollArea#detailScroll, QScrollArea#detailScroll > QWidget { background: $surface; }
QFrame#rule { color: $border; background: $border; border: 0; min-width: 1px; }
QPushButton, QToolButton {
    background: $raised; border: 1px solid $strong; border-radius: 6px;
    padding: 4px 9px; min-height: 17px;
}
QPushButton:hover, QToolButton:hover { background: $stripe; border-color: $accent; }
QPushButton:pressed, QToolButton:pressed { background: $inset; }
QPushButton:disabled, QToolButton:disabled { color: $dim; background: $inset; border-color: $border; }
QPushButton#primary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 650; }
QPushButton#primary:hover { background: $accent_hover; }
QPushButton#primary:disabled { color: $dim; background: $inset; border-color: $border; }
QPushButton#applySave { color: $gold; background: $gold_tint; border-color: $gold_border; font-weight: 650; }
QPushButton#applySave:hover { border-color: $gold; }
QPushButton#sortNow, QPushButton#saveProfile:enabled, QPushButton#assistantButton {
    color: $accent; background: $accent_tint; border-color: $strong; font-weight: 650;
}
QPushButton#sortNow:hover, QPushButton#saveProfile:enabled:hover, QPushButton#assistantButton:hover { border-color: $accent; }
QPushButton#assistantMode:checked {
    color: $accent; background: $accent_tint; border-color: $accent;
}
QLabel#assistantContext {
    color: $accent; background: $accent_tint; padding: 4px 8px; border-radius: 5px;
}
QPushButton#quiet, QToolButton#quiet { background: transparent; border-color: $border; }
QPushButton#quiet:hover, QToolButton#quiet:hover { background: $raised; border-color: $strong; }
QPushButton:focus, QToolButton:focus { border-color: $accent; }
QPushButton#warningFilter, QPushButton#errorFilter, QPushButton#clearProblemFilter {
    padding: 2px 6px; font-size: 11px; font-weight: 600;
}
QPushButton#warningFilter { color: $problem_yellow; background: $warning_tint; }
QPushButton#errorFilter { color: $problem_red; background: $error_tint; }
QPushButton#warningFilter:checked, QPushButton#warningFilter:enabled:hover, QPushButton#warningFilter:focus {
    border-color: $problem_yellow;
}
QPushButton#errorFilter:checked, QPushButton#errorFilter:enabled:hover, QPushButton#errorFilter:focus {
    border-color: $problem_red;
}
QPushButton#warningFilter:disabled, QPushButton#errorFilter:disabled {
    color: $dim; background: $inset; border-color: $border;
}
QToolButton#searchMode, QToolButton#searchOptions, QToolButton#searchStep, QToolButton#searchChip {
    padding: 2px 5px; min-height: 16px; background: $inset; border-color: $border;
}
QToolButton#searchMode:checked, QPushButton#globalSearch {
    color: $accent; background: $accent_tint; border-color: $strong; font-weight: 650;
}
QToolButton#searchMode:hover, QToolButton#searchOptions:hover, QToolButton#searchStep:enabled:hover,
QToolButton#searchChip:hover, QPushButton#globalSearch:hover { border-color: $accent; }
QToolButton#searchMode:focus, QToolButton#searchOptions:focus, QToolButton#searchStep:focus,
QToolButton#searchChip:focus, QPushButton#globalSearch:focus { border-color: $accent; }
QToolButton#searchStep { padding: 1px; }
QToolButton#searchChip { color: $accent; background: $accent_tint; font-size: 11px; }
QToolButton::menu-indicator { subcontrol-position: right center; }
QToolButton#sortOptions::menu-indicator, QToolButton#searchOptions::menu-indicator { image: none; width: 0; }
QToolButton#groupControl { padding: 1px; min-height: 18px; background: transparent; border-color: transparent; }
QToolButton#groupControl:hover, QToolButton#groupControl:focus { background: $raised; border-color: $strong; }
QToolButton#groupControl:checked { background: $accent_tint; border-color: $accent; }
QToolButton#groupControl::menu-indicator { image: none; width: 0; }
QTreeWidget#groupShelf { background: $surface; border: none; outline: none; }
QTreeWidget#groupShelf::item { min-height: 24px; border-bottom: 1px solid $border; }
QTreeWidget#groupShelf::item:hover { background: $stripe; }
QTreeWidget#groupShelf::item:selected { background: $selection; color: $text; }
QTreeWidget#featureGuide { background: $surface; outline: none; }
QTreeWidget#featureGuide::item { min-height: 24px; border-bottom: 1px solid $border; }
QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser {
    background: $inset; color: $text; border: 1px solid $border; border-radius: 6px;
    padding: 3px 6px; selection-background-color: $selection;
}
QLineEdit:focus, QComboBox:focus { border-color: $accent; }
QComboBox { min-height: 17px; padding-right: 18px; }
QComboBox#workspaceCombo::down-arrow { image: none; width: 0; }
QComboBox#workspaceCombo::drop-down { border: none; width: 17px; subcontrol-origin: padding; }
QComboBox QAbstractItemView { background: $surface; selection-background-color: $selection; }
QPlainTextEdit#description { background: $surface; border: 0; padding: 1px 1px; }
QTextBrowser#assistantChat { background: $inset; padding: 7px 10px; }
QPlainTextEdit#assistantInput { background: $surface; padding: 6px; }
QListWidget, QTableWidget { background: $surface; border: none; outline: none; alternate-background-color: $stripe; }
QListWidget::item { padding: 3px 6px; }
QListWidget#relationList::item, QListWidget#groupList::item,
QListWidget#stagingList::item, QListWidget#snapshotList::item, QListWidget#globalResults::item {
    padding: 7px 8px; border-bottom: 1px solid $border;
}
QListWidget#relationList::item:hover, QListWidget#groupList::item:hover,
QListWidget#globalResults::item:hover { background: $stripe; }
QListWidget#stagingList::item:focus { background: $accent_tint; }
QListWidget#problemTargets::item {
    min-height: 19px; padding: 3px 7px; border-bottom: 1px solid $border;
}
QListWidget#problemTargets::item:hover { background: $raised; }
QTableWidget::item { padding: 0 4px; }
QListWidget::item:selected, QTableWidget::item:selected { background: $selection; color: $text; }
QHeaderView::section { background: $inset; color: $muted; border: 0; border-bottom: 1px solid $border; padding: 4px 7px; }
QTableCornerButton::section { background: $inset; border: 0; }
QScrollBar:vertical { background: $surface; width: 8px; margin: 2px 1px; }
QScrollBar:horizontal { background: $surface; height: 8px; margin: 1px 2px; }
QScrollBar::handle { background: $strong; border-radius: 3px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QStatusBar { color: $muted; background: $menu; border-top: 1px solid $border; }
QStatusBar::item { border: 0; }
QProgressBar { background: $inset; border: 0; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: $accent; }
QSplitter::handle { background: $background; }
QSplitter::handle:hover { background: $strong; }
QSplitter#workbenchPanes::handle:horizontal {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 $background, stop: 0.42 $background, stop: 0.43 $border,
        stop: 0.57 $border, stop: 0.58 $background, stop: 1 $background);
}
QSplitter#workbenchPanes::handle:horizontal:hover { background: $strong; }
QTabWidget::pane { border: 1px solid $border; background: $surface; top: -1px; border-radius: 7px; }
QTabBar::base { border: 0; background: $background; }
QTabBar::tab { min-height: 17px; padding: 4px 12px; background: $background; color: $muted;
               border: 1px solid transparent; border-top-width: 2px;
               border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: $surface; color: $text; border-top-color: $accent;
                        border-left-color: $border; border-right-color: $border; }
QTabBar::tab:hover { color: $text; }
QTabWidget#utilityTabs::pane { border: 0; top: 0; }
QTabWidget#utilityTabs QTabBar::tab {
    background: $surface; padding: 4px 6px; border: 0; border-bottom: 2px solid transparent;
}
QTabWidget#utilityTabs QTabBar::tab:selected { background: $accent_tint; border-bottom-color: $accent; color: $accent; }
QCheckBox::indicator {
    width: 11px; height: 11px; background: $inset; border: 1px solid $dim; border-radius: 2px;
}
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
QToolTip { background: $raised; color: $text; border: 1px solid $strong; padding: 4px; }
""")


def current_theme() -> Theme:
    app = QApplication.instance()
    return THEMES.get(app.property("darksortTheme") if app else "", THEMES[DEFAULT_THEME])


def problem_color(level: str, theme: Theme | None = None) -> str:
    theme = theme or current_theme()
    if level == "error":
        return "#ff8d9a" if theme.dark else "#a4233b"
    return "#f0c466" if theme.dark else "#86590a"


def apply_theme(app: QApplication, key: str = DEFAULT_THEME) -> None:
    key = key if key in THEMES else DEFAULT_THEME
    theme = THEMES[key]
    if app.property("darksortTheme") == key:
        return
    app.setProperty("darksortTheme", key)
    app.setStyle("Fusion")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark if theme.dark else Qt.ColorScheme.Light)
    palette = QPalette()
    role = QPalette.ColorRole
    for color_role, color in (
        (role.Window, theme.background),
        (role.WindowText, theme.text),
        (role.Base, theme.inset),
        (role.AlternateBase, theme.stripe),
        (role.Text, theme.text),
        (role.Button, theme.raised),
        (role.ButtonText, theme.text),
        (role.PlaceholderText, theme.dim),
        (role.Highlight, theme.selection),
        (role.HighlightedText, theme.text),
        (role.Link, theme.accent),
    ):
        palette.setColor(color_role, QColor(color))
    app.setPalette(palette)
    gold = "#e9b573" if theme.dark else "#88511f"
    yellow, red = problem_color("warning", theme), problem_color("error", theme)
    app.setStyleSheet(
        STYLE.substitute(
            asdict(theme),
            accent_hover=QColor(theme.accent).lighter(112).name(),
            accent_tint=theme.tint(theme.accent).name(),
            gold=gold,
            gold_tint=theme.tint(gold, 0.15).name(),
            gold_border=theme.tint(gold, 0.35).name(),
            problem_yellow=yellow,
            problem_red=red,
            warning_tint=theme.tint(yellow, 0.10).name(),
            error_tint=theme.tint(red, 0.10).name(),
        )
    )
    icon_path = Path(__file__).resolve().parents[1] / "assets" / "darksort.ico"
    app.setWindowIcon(QIcon(str(icon_path)))


def icon(name: str, color: str | None = None, size: int = 16) -> QIcon:
    image = QPixmap(size * 2, size * 2)
    image.setDevicePixelRatio(2)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_icon(painter, QRect(0, 0, size, size), name, QColor(color or current_theme().muted))
    painter.end()
    return QIcon(image)


def paint_icon(painter: QPainter, rect: QRect, name: str, color: QColor) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 16, rect.height() / 16)
    painter.setPen(
        QPen(color, 1.3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    )
    painter.setBrush(Qt.BrushStyle.NoBrush)
    match name:
        case "dot":
            painter.setBrush(color)
            painter.drawEllipse(4, 4, 8, 8)
        case "search":
            painter.drawEllipse(3, 3, 7, 7)
            painter.drawLine(9, 9, 13, 13)
        case "refresh":
            painter.drawArc(3, 3, 10, 10, 40 * 16, 275 * 16)
            painter.drawLine(12, 2, 12, 6)
            painter.drawLine(12, 6, 8, 6)
        case "workshop":
            path = QPainterPath()
            path.moveTo(4, 12)
            path.cubicTo(1, 12, 1, 7, 4, 7)
            path.cubicTo(4, 3, 10, 2, 11, 7)
            path.cubicTo(15, 6, 16, 12, 12, 12)
            path.closeSubpath()
            painter.fillPath(path, color)
        case "folder":
            path = QPainterPath()
            path.moveTo(2, 5)
            path.lineTo(6, 5)
            path.lineTo(7.5, 6.5)
            path.lineTo(14, 6.5)
            path.lineTo(14, 13)
            path.lineTo(2, 13)
            path.closeSubpath()
            painter.fillPath(path, color)
        case "sort":
            painter.drawLine(2, 4, 14, 4)
            painter.drawLine(2, 8, 11, 8)
            painter.drawLine(2, 12, 8, 12)
        case "down":
            painter.drawLine(4, 6, 8, 10)
            painter.drawLine(8, 10, 12, 6)
        case "grip" | "more":
            painter.setBrush(color)
            points = ((x, y) for x in (6, 10) for y in (4, 8, 12)) if name == "grip" else ((x, 8) for x in (4, 8, 12))
            for x, y in points:
                painter.drawEllipse(x - 1, y - 1, 2, 2)
        case "plus" | "minus":
            painter.drawLine(3, 8, 13, 8)
            if name == "plus":
                painter.drawLine(8, 3, 8, 13)
        case "expand" | "collapse":
            direction = 1 if name == "expand" else -1
            for y in (5, 10):
                painter.drawLine(4, y - direction, 8, y + 2 * direction)
                painter.drawLine(8, y + 2 * direction, 12, y - direction)
        case "previous" | "next":
            direction = -1 if name == "previous" else 1
            painter.drawLine(8 - 2 * direction, 4, 8 + 2 * direction, 8)
            painter.drawLine(8 + 2 * direction, 8, 8 - 2 * direction, 12)
        case "preview":
            for x, y, dx, dy in ((3, 3, 4, 4), (13, 3, -4, 4), (3, 13, 4, -4), (13, 13, -4, -4)):
                painter.drawLine(x, y, x + dx, y)
                painter.drawLine(x, y, x, y + dy)
        case "link":
            painter.drawRoundedRect(2, 5, 7, 6, 2.5, 2.5)
            painter.drawRoundedRect(7, 5, 7, 6, 2.5, 2.5)
        case "lock":
            painter.drawRoundedRect(3, 7, 10, 7, 1.4, 1.4)
            painter.drawArc(5, 2, 6, 9, 0, 180 * 16)
            painter.drawPoint(8, 10)
        case "warning":
            painter.drawLine(8, 2, 14, 13)
            painter.drawLine(14, 13, 2, 13)
            painter.drawLine(2, 13, 8, 2)
            painter.drawLine(8, 6, 8, 9)
            painter.drawPoint(8, 11)
        case "alert":
            painter.drawEllipse(2, 2, 12, 12)
            painter.drawLine(8, 5, 8, 9)
            painter.drawPoint(8, 11)
        case "close":
            painter.drawLine(4, 4, 12, 12)
            painter.drawLine(4, 12, 12, 4)
        case "brand":
            path = QPainterPath()
            path.moveTo(9, 1.5)
            path.cubicTo(11, 5, 13, 5.5, 12, 9)
            path.cubicTo(10.5, 12, 6, 12.5, 4.5, 9.5)
            path.cubicTo(3, 7, 5, 5, 6, 4)
            path.cubicTo(5.5, 7, 8.5, 7, 9, 1.5)
            painter.fillPath(path, color)
            painter.drawLine(5, 13, 11, 13)
            painter.drawLine(7, 14.5, 9, 14.5)
    painter.restore()
