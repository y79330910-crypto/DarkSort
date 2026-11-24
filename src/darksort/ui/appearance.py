from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from darksort.settings import DEFAULT_THEME
from darksort.ui.theme import THEMES, current_theme, paint_icon


def read_artwork(path: str, bounds: QSize) -> QPixmap:
    reader = QImageReader(path)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 64_000_000:
        return QPixmap()
    if size.width() > bounds.width() or size.height() > bounds.height():
        reader.setScaledSize(size.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio))
    reader.setAutoTransform(True)
    return QPixmap.fromImage(reader.read())


def show_artwork(parent, path: str, name: str) -> None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"预览封面 · {name}")
    layout = QVBoxLayout(dialog)
    screen = parent.screen().availableGeometry().size()
    bounds = QSize(min(1000, screen.width() - 100), min(720, screen.height() - 170))
    artwork = read_artwork(path, bounds)
    image = QLabel("无法读取这张封面。" if artwork.isNull() else "")
    if not artwork.isNull():
        artwork = artwork.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        image.setPixmap(artwork)
    image.setObjectName("artworkViewer")
    image.setAlignment(Qt.AlignmentFlag.AlignCenter)
    image.setMinimumSize(320, 190)
    layout.addWidget(image, 1)
    buttons = QDialogButtonBox()
    buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    dialog.exec()


class TagChips(QWidget):
    def __init__(self):
        super().__init__()
        self.tags = ()
        self.colors = ()
        self.setFixedHeight(22)

    def set_tags(self, tags, colors=()):
        self.tags = tuple(tags)
        self.colors = tuple(colors)
        self.setToolTip(" · ".join(tags))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        theme = current_theme()
        default = "#8ac2ad" if theme.dark else "#366951"
        font = self.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left = 0
        for index, tag in enumerate(self.tags):
            hue = QColor(self.colors[index] if index < len(self.colors) else default)
            if index < len(self.colors) and not theme.dark:
                hue = hue.darker(166)
            width = min(105, painter.fontMetrics().horizontalAdvance(tag) + 17)
            if left + width > self.width():
                painter.setPen(QColor(theme.dim))
                painter.drawText(QRect(left, 0, 18, 21), Qt.AlignmentFlag.AlignCenter, "…")
                break
            chip = QRect(left, 2, width, 18)
            painter.setPen(QPen(theme.tint(hue.name(), 0.32), 1))
            painter.setBrush(theme.tint(hue.name(), 0.12))
            painter.drawRoundedRect(chip, 4, 4)
            painter.setPen(hue)
            text = painter.fontMetrics().elidedText(tag, Qt.TextElideMode.ElideRight, width - 14)
            painter.drawText(chip.adjusted(7, 0, -7, 0), Qt.AlignmentFlag.AlignVCenter, text)
            left += width + 5
        if not self.tags:
            painter.setPen(QColor(theme.dim))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, "—")
        painter.end()


class ModArtwork(QAbstractButton):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(155)
        self.setMaximumHeight(190)
        self.setAccessibleName("查看 Mod 封面")
        self.setToolTip("点击查看大图")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.images = {}
        self.path = None

    def set_art(self, path):
        if path and path not in self.images:
            if len(self.images) >= 24:
                self.images.pop(next(iter(self.images)))
            self.images[path] = read_artwork(path, QSize(768, 480))
        self.path = path
        self.setEnabled(path in self.images and not self.images[path].isNull())
        self.update()

    def clear_cache(self):
        self.images.clear()
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        theme = current_theme()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(theme.accent if self.isEnabled() and (self.underMouse() or self.hasFocus()) else theme.border))
        painter.setBrush(QColor(theme.inset))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        image = self.images.get(self.path) if self.path else None
        if image and not image.isNull():
            fit = image.scaled(
                self.size() - QSize(16, 16),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                (self.width() - fit.width()) // 2, (self.height() - fit.height()) // 2, fit
            )
            control = QRect(self.width() - 30, self.height() - 30, 22, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.raised))
            painter.drawRoundedRect(control, 5, 5)
            paint_icon(painter, control.adjusted(4, 4, -4, -4), "preview", QColor(theme.text))
        else:
            center = self.rect().center()
            paint_icon(
                painter, QRect(center.x() - 15, center.y() - 15, 30, 30), "brand", QColor(theme.dim)
            )
        painter.end()


class ThemeTile(QAbstractButton):
    def __init__(self, key: str):
        super().__init__()
        self.theme = THEMES[key]
        self.setText(self.theme.name)
        self.setCheckable(True)
        self.setMinimumSize(152, 120)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = current_theme()
        tile = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(
            QPen(
                QColor(active.accent if self.isChecked() else active.strong),
                2 if self.isChecked() else 1,
            )
        )
        painter.setBrush(QColor(active.surface))
        painter.drawRoundedRect(tile, 6, 6)
        mini = tile.adjusted(10, 10, -10, -34)
        colors = self.theme
        painter.fillRect(mini, QColor(colors.background))
        painter.fillRect(QRect(mini.x(), mini.y(), mini.width(), 10), QColor(colors.menu))
        painter.fillRect(QRect(mini.right() - 26, mini.y() + 3, 21, 4), QColor(colors.accent))
        body = mini.adjusted(5, 14, -5, -5)
        x = body.x()
        for portion in (0.19, 0.19, 0.36, 0.26):
            width = max(8, int(body.width() * portion) - 3)
            painter.fillRect(QRect(x, body.y(), width, body.height()), QColor(colors.surface))
            for row in range(3):
                painter.fillRect(
                    QRect(x + 3, body.y() + 7 + row * 8, max(2, width - 6), 4),
                    QColor(colors.stripe),
                )
            x += width + 3
        painter.setPen(QColor(active.text))
        painter.drawText(
            tile.adjusted(11, tile.height() - 29, -24, -4),
            Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )
        painter.setPen(QPen(QColor(active.accent if self.isChecked() else active.dim), 1.3))
        center = tile.bottomRight()
        painter.setBrush(QColor(active.accent) if self.isChecked() else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRect(center.x() - 17, center.y() - 21, 9, 9))
        painter.end()


class AppearanceDialog(QDialog):
    theme_changed = Signal(str)

    def __init__(self, selected: str, parent=None):
        super().__init__(parent)
        self.key = selected if selected in THEMES else DEFAULT_THEME
        self.setWindowTitle("外观 · DarkSort")
        self.resize(850, 257)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(11)
        title = QLabel("界面主题")
        title.setObjectName("section")
        layout.addWidget(title)
        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.group = QButtonGroup(self)
        for index, key in enumerate(THEMES):
            tile = ThemeTile(key)
            self.group.addButton(tile)
            tile.setChecked(key == self.key)
            tile.clicked.connect(lambda _checked, key=key: self.choose(key))
            tiles.addWidget(tile, 0, index)
        layout.addLayout(tiles)
        buttons = QDialogButtonBox()
        save = buttons.addButton("使用此主题", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose(self, key: str):
        self.key = key
        self.theme_changed.emit(key)
        for tile in self.group.buttons():
            tile.update()
