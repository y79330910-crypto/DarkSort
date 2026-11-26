from __future__ import annotations

import json

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QWidget,
)

from darksort.locks import snap_boundary
from darksort.ui.appearance import read_artwork
from darksort.ui.theme import current_theme, paint_icon, problem_color

MIME = "application/x-darksort-mod-selection"
ROLE = Qt.ItemDataRole.UserRole
ROW_HEIGHT = 29
RELATION_GUTTER = 23
EDGE_COLORS = {
    "above": ("#7cb9f0", "#286aa4"),
    "requires": ("#75caaa", "#257755"),
}
GROUP_COLORS = ("#83b7ec", "#b7a3e9", "#e6b77c", "#7bcfac")
KIND_COLORS = {
    "font": "#77bad9",
    "translation": "#aca6ea",
    "patch": "#dca264",
    "expansion": "#68c89a",
    "region": "#e08982",
    "hero": "#77a9e3",
    "skin": "#d893c1",
    "texture": "#a6bc78",
}


def column_rects(rect: QRect, active: bool) -> dict[str, QRect]:
    left = rect.left() + 8 + (RELATION_GUTTER if active else 0)
    right = rect.right() - 6
    row = {}
    widths = (
        (
            ("status", 26),
            ("version", 64 if rect.width() >= 510 else 0),
            ("category", 91 if rect.width() >= 386 else 0),
        )
        if active
        else (("status", 20),)
    )
    for name, width in widths:
        row[name] = QRect(right - width + 1, rect.top(), width, rect.height())
        right -= width
    if active:
        row["rank"] = QRect(left, rect.top(), 37, rect.height())
        left += 37
    row["art"] = QRect(left, rect.top(), 21, rect.height())
    left += 25
    row["source"] = QRect(left, rect.top(), 22, rect.height())
    left += 23
    row["name"] = QRect(left, rect.top(), max(0, right - left + 1), rect.height())
    return row


def draw_label(painter, rect, text, *, align=Qt.AlignmentFlag.AlignLeft):
    if rect.width() > 0:
        elided = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, elided)


class ColumnHeader(QWidget):
    def __init__(self, list_widget):
        super().__init__()
        self.list = list_widget
        self.setFixedHeight(23)
        self.setAccessibleName("计划顺序列" if list_widget.side == "profile" else "可用 Mod 列")
        if list_widget.side == "profile":
            self.setToolTip("蓝线表示上下顺序，绿线表示启用依赖，紧贴行的紫色链扣表示联锁。")
        list_widget.verticalScrollBar().rangeChanged.connect(lambda *_: self.update())

    def paintEvent(self, _event):
        painter = QPainter(self)
        theme = current_theme()
        painter.fillRect(self.rect(), QColor(theme.inset))
        painter.setPen(QColor(theme.border))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        active = self.list.side == "profile"
        columns = column_rects(QRect(0, 0, self.list.viewport().width(), self.height()), active)
        font = self.font()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(theme.dim))
        if active:
            paint_icon(painter, QRect(6, 5, 12, 12), "link", QColor(theme.dim))
            draw_label(painter, columns["rank"], "#", align=Qt.AlignmentFlag.AlignHCenter)
            draw_label(painter, columns["category"].adjusted(3, 0, -4, 0), "分类")
            draw_label(painter, columns["version"], "版本")
        draw_label(painter, columns["source"], "来源")
        draw_label(painter, columns["name"], "MOD 名称")
        painter.end()


class ModDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.thumbnails = {}

    def sizeHint(self, option, index):
        return QSize(0, 27 if "group" in (index.data(ROLE) or {}) else ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index):
        data = index.data(ROLE) or {}
        painter.save()
        rect = option.rect
        theme = current_theme()
        if "group" in data:
            self.paint_group(painter, rect, data, option.font)
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovering = bool(option.state & QStyle.StateFlag.State_MouseOver)
        locked_selection = data.get("value") in self.view.lock_selection
        lock = data.get("lock")
        matched = data.get("value") in self.view.search_marks
        current_match = data.get("value") == self.view.current_match
        linked = data.get("value") in self.view.relation_marks
        pointed = data.get("value") == self.view.detail_mark
        temporary = data.get("temporary")
        relation_color = "#79ceea" if theme.dark else "#206d86"
        search_color = "#efbd79" if theme.dark else "#8c5019"
        lock_color = "#baaaed" if theme.dark else "#604694"
        lock_selected = lock and (selected or locked_selection)
        pulse = self.view.lock_strength if lock and lock.members[0] == self.view.pulsing_lock else 0
        mark = data.get("mark_color")
        inset = RELATION_GUTTER if self.view.side == "profile" else 0
        content = rect.adjusted(inset, 0, 0, 0)
        painter.fillRect(rect, QColor(theme.surface))
        if selected or hovering or linked or matched or pointed or temporary or lock or mark or index.row() % 2 == 0:
            fill = QColor(theme.stripe)
            if lock_selected or pulse:
                fill = theme.tint(lock_color, 0.23 + pulse * 0.19)
            elif selected:
                fill = QColor(theme.selection)
            elif hovering:
                fill = QColor(theme.raised)
            elif current_match or matched or temporary:
                fill = theme.tint(search_color, 0.11)
            elif linked or pointed:
                fill = theme.tint(relation_color, 0.11)
            elif mark:
                fill = theme.tint(mark, 0.12)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            outline = search_color if current_match or temporary else relation_color if pointed else None
            if outline:
                painter.setPen(QPen(QColor(outline), 1))
            elif lock_selected or pulse:
                painter.setPen(QPen(theme.tint(lock_color, 0.55 + pulse * 0.25), 1))
            elif selected:
                painter.setPen(QPen(theme.tint(theme.accent, 0.45), 1))
            else:
                painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(content.adjusted(4, 1, -4, -1), 6, 6)
        if lock:
            self.paint_lock(painter, content, index, lock, QColor(lock_color), pulse)
        elif selected:
            painter.fillRect(QRect(content.x() + 4, rect.y() + 5, 2, rect.height() - 10), QColor(theme.accent))
        group_colors = data.get("group_colors", ())
        top, height = rect.y() + 5, rect.height() - 10
        count = min(len(group_colors), height)
        for index_color, color in enumerate(group_colors[:count]):
            start, end = (index_color * height // count), ((index_color + 1) * height // count)
            painter.fillRect(QRect(rect.right() - 4, top + start, 3, end - start), QColor(color))
        if mark:
            x = rect.right() - (8 if group_colors else 4)
            painter.fillRect(QRect(x, top, 2, height), QColor(mark))
        columns = column_rects(rect, data.get("number") is not None)
        mono = QFont("Consolas")
        mono.setPixelSize(11)
        painter.setFont(mono)
        painter.setPen(QColor(theme.dim))
        if "rank" in columns:
            if matched:
                painter.setPen(QColor(search_color))
            draw_label(
                painter, columns["rank"], str(data["number"]), align=Qt.AlignmentFlag.AlignHCenter
            )
            painter.setPen(QColor(theme.dim))
            version = data.get("version", "")
            draw_label(painter, columns["version"], version if version != "未知" else "·")

        source = "workshop" if data.get("source") == "workshop" else "folder"
        hue = "#78b0e5" if source == "workshop" else "#6dc99d"
        if not theme.dark:
            hue = QColor(hue).darker(152).name()
        artwork = QRect(columns["art"].x(), rect.center().y() - 9, 20, 20)
        self.paint_thumbnail(painter, artwork, data.get("preview"))
        src = QRect(columns["source"].x() + 1, rect.center().y() - 6, 13, 13)
        paint_icon(painter, src, source, QColor(hue))

        painter.setFont(option.font)
        painter.setPen(QColor(search_color if matched or temporary or data.get("missing") else theme.text))
        name = f"临时 · {data['name']}" if temporary else data.get("name", "")
        draw_label(painter, columns["name"].adjusted(0, 0, -5, 0), name)
        if "category" in columns:
            self.paint_category(painter, columns["category"], data)
        point = columns["status"].center()
        problem = data.get("problem")
        if problem:
            color = problem_color(problem, theme)
            badge = QRect(point.x() - 10, point.y() - 9, 20, 19)
            painter.setPen(QPen(theme.tint(color, 0.44)))
            painter.setBrush(theme.tint(color, 0.15))
            painter.drawRoundedRect(badge, 5, 5)
            glyph = "alert" if problem == "error" else "warning"
            paint_icon(painter, badge.adjusted(3, 2, -3, -2), glyph, QColor(color))
        elif data.get("warning"):
            paint_icon(painter, QRect(point.x() - 6, point.y() - 6, 12, 12), "warning",
                       QColor(problem_color("warning", theme)))
        elif linked:
            paint_icon(painter, QRect(point.x() - 7, point.y() - 7, 14, 14), "link", QColor(relation_color))
        painter.restore()

    @staticmethod
    def paint_lock(painter, rect, index, lock, color, pulse):
        y = rect.center().y()
        x = rect.left() + 8
        previous = index.siblingAtRow(index.row() - 1).data(ROLE) or {}
        following = index.siblingAtRow(index.row() + 1).data(ROLE) or {}
        start = rect.top() if previous.get("lock") == lock else y
        stop = rect.bottom() + 1 if following.get("lock") == lock else y
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if pulse:
            glow = QColor(color)
            glow.setAlphaF(pulse * 0.32)
            painter.setPen(QPen(glow, 6))
            painter.drawLine(x, start, x, stop)
        painter.setPen(QPen(color, 1.6))
        painter.drawLine(x, start, x, stop)
        glyph = QRect(x - 7, y - 6, 14, 13)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(current_theme().tint(color.name(), 0.15))
        painter.drawRoundedRect(glyph, 3, 3)
        paint_icon(painter, glyph, "link", color)

    def paint_thumbnail(self, painter, rect, path):
        theme = current_theme()
        if path and path not in self.thumbnails:
            if len(self.thumbnails) >= 128:
                self.thumbnails.pop(next(iter(self.thumbnails)))
            self.thumbnails[path] = read_artwork(path, QSize(40, 40))
        image = self.thumbnails.get(path)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(theme.border))
        painter.setBrush(QColor(theme.inset))
        painter.drawRoundedRect(rect, 4, 4)
        if image and not image.isNull():
            painter.save()
            mask = QPainterPath()
            mask.addRoundedRect(rect.x() + 1, rect.y() + 1, rect.width() - 2, rect.height() - 2, 3, 3)
            painter.setClipPath(mask)
            fit = image.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(rect.center().x() - fit.width() // 2, rect.center().y() - fit.height() // 2, fit)
            painter.restore()
        else:
            paint_icon(painter, rect.adjusted(5, 5, -5, -5), "brand", QColor(theme.dim))

    def paint_group(self, painter, rect, data, font):
        theme = current_theme()
        color = QColor(GROUP_COLORS[data.get("group_color", 0)])
        if not theme.dark:
            color = color.darker(165)
        painter.fillRect(rect, QColor(theme.surface))
        rect = rect.adjusted(RELATION_GUTTER, 0, 0, 0)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.tint(color.name(), 0.27), 1))
        painter.setBrush(theme.tint(color.name(), 0.16))
        painter.drawRoundedRect(rect.adjusted(4, 2, -4, -1), 5, 5)
        x, y = rect.left() + 14, rect.center().y()
        painter.setPen(QPen(color, 1.2))
        if data["collapsed"]:
            painter.drawLine(x - 2, y - 4, x + 2, y)
            painter.drawLine(x + 2, y, x - 2, y + 4)
        else:
            painter.drawLine(x - 4, y - 2, x, y + 2)
            painter.drawLine(x, y + 2, x + 4, y - 2)
        font = QFont(font)
        font.setBold(True)
        painter.setFont(font)
        draw_label(painter, rect.adjusted(24, 0, -10, 0), data["name"])

    def paint_category(self, painter, rect, data):
        label = data.get("classification", "")
        if not label or not rect.width():
            return
        theme = current_theme()
        key = data.get("category", "")
        if key not in KIND_COLORS:
            painter.setPen(QColor(theme.dim))
            draw_label(painter, rect.adjusted(3, 0, -5, 0), label)
            return
        hue = QColor(KIND_COLORS[key])
        if not theme.dark:
            hue = hue.darker(155)
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        width = min(rect.width() - 8, painter.fontMetrics().horizontalAdvance(label) + 12)
        chip = QRect(rect.left() + 2, rect.center().y() - 7, width, 15)
        painter.setPen(QPen(theme.tint(hue.name(), 0.40), 1))
        painter.setBrush(theme.tint(hue.name(), 0.13))
        painter.drawRoundedRect(chip, 4, 4)
        painter.setPen(hue)
        draw_label(painter, chip.adjusted(3, 0, -3, 0), label, align=Qt.AlignmentFlag.AlignHCenter)


class GroupDragHandle(QToolButton):
    drag_state_changed = Signal(bool)

    def __init__(self, profile_id, group_id, title, parent=None):
        super().__init__(parent)
        self.payload = {"side": "group", "profile_id": profile_id, "group_id": group_id}
        self.origin = None
        self.result = None
        self.setAccessibleName(f"拖动分组 {title}")
        self.setToolTip("拖进计划整组启用，拖回可用整组停用")
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.origin = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if (self.origin is None or not event.buttons() & Qt.MouseButton.LeftButton
                or (event.position().toPoint() - self.origin).manhattanLength() < QApplication.startDragDistance()):
            super().mouseMoveEvent(event)
            return
        self.origin = self.result = None
        self.setDown(False)
        mime = QMimeData()
        mime.setData(MIME, json.dumps(self.payload).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.parentWidget().grab())
        drag.setHotSpot(QPoint(self.x() + 10, self.y() + 10))
        self.drag_state_changed.emit(True)
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self.drag_state_changed.emit(False)
        if self.result:
            view, payload, boundary = self.result
            QTimer.singleShot(0, view, lambda: view.dropped.emit(view.side, payload, boundary))


class GroupMemberTree(QTreeWidget):
    member_moved = Signal(str, str, str, bool)
    drag_state_changed = Signal(bool)

    def __init__(self):
        super().__init__(objectName="groupShelf")
        self.profile_id = ""
        self.dragged = None
        self.drop_line = None
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(False)

    def startDrag(self, _actions):
        item = self.currentItem()
        if not item or not item.parent():
            return
        group_id = item.parent().data(0, ROLE)
        entry = item.data(0, ROLE).entry
        member_id = entry.id
        self.dragged = (group_id, member_id)
        payload = {"side": "group-member", "profile_id": self.profile_id,
                   "group_id": group_id, "member_id": member_id, "references": [entry.reference]}
        mime = QMimeData()
        mime.setData(MIME, json.dumps(payload).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)))
        self.drag_state_changed.emit(True)
        try:
            drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)
        finally:
            self.dragged = None
            self.drop_line = None
            self.viewport().update()
            self.drag_state_changed.emit(False)

    def dragEnterEvent(self, event):
        if self.dragged and event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def member_at_drop(self, event):
        point = event.position().toPoint()
        item = self.itemAt(point)
        if (self.dragged and event.source() is self and item and item.parent()
                and item.parent().data(0, ROLE) == self.dragged[0]):
            return item, point.y() > self.visualItemRect(item).center().y()
        return None, False

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        item, below = self.member_at_drop(event)
        self.drop_line = None
        if item:
            rect = self.visualItemRect(item)
            self.drop_line = rect.bottom() if below else rect.top()
            event.acceptProposedAction()
        else:
            event.ignore()
        self.viewport().update()

    def dragLeaveEvent(self, event):
        self.drop_line = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        item, below = self.member_at_drop(event)
        self.drop_line = None
        self.viewport().update()
        if not item:
            event.ignore()
            return
        group_id, member_id = self.dragged
        target_id = item.data(0, ROLE).entry.id
        QTimer.singleShot(0, self, lambda: self.member_moved.emit(group_id, member_id, target_id, below))
        event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_line is not None:
            painter = QPainter(self.viewport())
            painter.setPen(QPen(QColor(current_theme().accent), 2))
            painter.drawLine(0, self.drop_line, self.viewport().width(), self.drop_line)
            painter.end()


class ModList(QListWidget):
    dropped = Signal(str, object, int)
    relationship_selected = Signal(str)
    problem_selected = Signal(str)
    menu_requested = Signal(str, object, QPoint)

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.total_entries = 0
        self.drop_line = None
        self.drop_highlight = False
        self.reorder_blocked = False
        self.search_marks = set()
        self.current_match = None
        self.detail_mark = None
        self.relation_marks = set()
        self.relationship_anchor: str | None = None
        self.relationships: list[tuple[str, int, str]] = []
        self.relationship_rows: dict[str, int] = {}
        self.relationship_hits: list[tuple[QRect, str, str]] = []
        self.pressed_relationship: str | None = None
        self.pressed_problem: str | None = None
        self.toggled_lock = False
        self.lock_selection = set()
        self.pulsing_lock = None
        self.lock_strength = 0.0
        self.lock_animation = QVariantAnimation(self)
        self.lock_animation.setStartValue(1.0)
        self.lock_animation.setEndValue(0.0)
        self.lock_animation.setDuration(520)
        self.lock_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.lock_animation.valueChanged.connect(self.update_lock_pulse)
        self.units = []
        self.setObjectName(f"{side}List")
        self.setAccessibleName("可用 Mod" if side == "library" else "计划顺序")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(False)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformItemSizes(side == "library")
        self.setItemDelegate(ModDelegate(self))

    def flash_lock(self, identity):
        self.pulsing_lock = identity
        if self.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) > 0:
            self.lock_animation.stop()
            self.lock_animation.start()

    def update_lock_pulse(self, value):
        self.lock_strength = float(value)
        self.viewport().update()

    def startDrag(self, supported_actions):
        rows = [item.data(ROLE) for item in sorted(self.selectedItems(), key=self.row)]
        if not rows:
            return
        payload = {
            "side": self.side,
            "values": [row["value"] for row in rows],
            "references": [row["reference"] for row in rows if "reference" in row],
        }
        mime = QMimeData()
        mime.setData(MIME, json.dumps(payload).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if self.accepts_drag(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self.accepts_drag(event):
            self.clear_drop_line()
            event.ignore()
            return
        super().dragMoveEvent(event)
        item = self.itemAt(event.position().toPoint())
        self.drop_line = None
        self.drop_highlight = isinstance(event.source(), (GroupDragHandle, GroupMemberTree))
        if self.side == "profile" and item:
            rect = self.visualItemRect(item)
            below = event.position().y() > rect.center().y()
            raw = self.boundary_at(item, below)
            boundary = snap_boundary(self.units, raw)
            self.drop_line = rect.bottom() if below else rect.top()
            if raw != boundary:
                self.drop_line = self.line_for_boundary(boundary)
        elif self.side == "profile" and self.drop_highlight:
            self.drop_line = self.line_for_boundary(self.total_entries)
        self.viewport().update()
        event.acceptProposedAction()

    def accepts_drag(self, event):
        return (not self.reorder_blocked and event.mimeData().hasFormat(MIME)
                and isinstance(event.source(), (ModList, GroupDragHandle, GroupMemberTree)))

    def clear_drop_line(self):
        if self.drop_line is not None or self.drop_highlight:
            self.drop_line = None
            self.drop_highlight = False
            self.viewport().update()

    def dragLeaveEvent(self, event):
        self.clear_drop_line()
        super().dragLeaveEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.side == "profile":
            self.paint_relationships()
        if self.drop_line is not None or self.drop_highlight:
            painter = QPainter(self.viewport())
            theme = current_theme()
            if self.drop_highlight:
                painter.setPen(QPen(theme.tint(theme.accent, 0.64), 1.5))
                painter.drawRoundedRect(self.viewport().rect().adjusted(1, 1, -2, -2), 6, 6)
            if self.drop_line is not None:
                painter.setPen(QPen(QColor(theme.accent), 2))
                painter.drawLine(2, self.drop_line, self.viewport().width() - 2, self.drop_line)
            painter.end()

    def set_relationships(self, anchor, edges):
        self.relationship_anchor = anchor
        self.relationships = sorted(edges, key=lambda edge: (edge[1], edge[2])) if anchor else []
        self.relationship_rows = {}
        self.relationship_hits.clear()
        if self.relationships:
            entry_ids = {anchor, *(target for target, _, _ in self.relationships)}
            for row in range(self.count()):
                identity = self.item(row).data(ROLE).get("entry_id")
                if identity in entry_ids:
                    self.relationship_rows[identity] = row
        self.viewport().update()

    def paint_relationships(self):
        self.relationship_hits.clear()
        painter = QPainter(self.viewport())
        theme = current_theme()
        height = self.viewport().height()
        painter.fillRect(QRect(0, 0, RELATION_GUTTER, height), QColor(theme.inset))
        anchor_row = self.relationship_rows.get(self.relationship_anchor)
        if anchor_row is None or height <= 26:
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        anchor_y = self.visualItemRect(self.item(anchor_row)).center().y()
        start = max(0, min(height - 1, anchor_y))
        endpoints = {}
        beyond = {"top": {}, "bottom": {}}
        for target, _, kind in self.relationships:
            row = self.relationship_rows.get(target)
            if row is None:
                continue
            rect = self.visualItemRect(self.item(row))
            y = rect.center().y()
            if 0 <= y < height:
                endpoints[target] = (y, self.item(row).data(ROLE)["name"])
                self.draw_relationship(painter, start, y, kind)
            else:
                farther = "top" if y < 0 else "bottom"
                beyond[farther].setdefault(target, (y, set()))[1].add(kind)

        point_x = RELATION_GUTTER - 4
        for target, (y, name) in endpoints.items():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.text))
            painter.drawEllipse(QPoint(point_x, y), 2, 2)
            self.relationship_hits.append((QRect(0, y - 6, RELATION_GUTTER, 13), target, f"定位 · {name}"))
        if 0 <= anchor_y < height:
            painter.setBrush(QColor(theme.inset))
            painter.setPen(QPen(QColor(theme.accent), 1.5))
            painter.drawEllipse(QPoint(point_x, anchor_y), 3, 3)

        for edge, choices in beyond.items():
            if not choices:
                continue
            top = edge == "top"
            y = 8 if top else height - 9
            target = max(choices, key=lambda identity: choices[identity][0]) if top else min(
                choices, key=lambda identity: choices[identity][0]
            )
            for kind in {kind for _, kinds in choices.values() for kind in kinds}:
                self.draw_relationship(painter, start, y, kind)
            pill = QRect(1, y - 7, RELATION_GUTTER - 2, 15)
            painter.setPen(QColor(theme.accent))
            painter.setBrush(QColor(theme.raised))
            painter.drawRoundedRect(pill, 4, 4)
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            count = str(len(choices)) if len(choices) <= 99 else "+"
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, f"{'↑' if top else '↓'}{count}")
            where = "上方" if top else "下方"
            hit = (pill, target, f"{where}有 {len(choices)} 项关联 · 点击定位最近一项")
            self.relationship_hits.insert(0, hit)
        painter.end()

    @staticmethod
    def draw_relationship(painter, start, end, kind):
        if start == end:
            return
        theme = current_theme()
        hue = QColor(EDGE_COLORS[kind][0 if theme.dark else 1])
        lane = 6 if kind == "above" else 12
        tip = RELATION_GUTTER - 4
        turn = min(4, abs(end - start) // 2)
        direction = 1 if end > start else -1
        curve = QPainterPath()
        curve.moveTo(tip, start)
        curve.lineTo(lane + turn, start)
        curve.quadTo(lane, start, lane, start + turn * direction)
        curve.lineTo(lane, end - turn * direction)
        curve.quadTo(lane, end, lane + turn, end)
        curve.lineTo(tip, end)
        painter.setPen(QPen(hue, 1.25))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(curve)

    def relationship_at(self, point):
        return next(((target, tooltip) for rect, target, tooltip in self.relationship_hits if rect.contains(point)), None)

    def problem_at(self, point):
        item = self.itemAt(point)
        data = item.data(ROLE) if item else {}
        if data.get("problem") and column_rects(self.visualItemRect(item), True)["status"].contains(point):
            return data["entry_id"], data["problem"]
        return None

    def contextMenuEvent(self, event):
        keyboard = event.reason() == event.Reason.Keyboard
        item = self.currentItem() if keyboard else self.itemAt(event.pos())
        if item is None:
            event.ignore()
            return
        data = item.data(ROLE)
        point = self.viewport().mapToGlobal(self.visualItemRect(item).center()) if keyboard else event.globalPos()
        if "group" not in data:
            flags = QItemSelectionModel.SelectionFlag
            command = flags.NoUpdate if item.isSelected() or data["value"] in self.lock_selection else flags.ClearAndSelect
            self.setCurrentItem(item, command)
        self.menu_requested.emit(self.side, data, point)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            if hit := self.problem_at(point):
                self.pressed_problem = hit[0]
            elif hit := self.relationship_at(point):
                self.pressed_relationship = hit[0]
            elif event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                item = self.itemAt(point)
                if item and (lock := item.data(ROLE).get("lock")):
                    selected = [other for other in self.selectedItems() if other.data(ROLE).get("lock") == lock]
                    if selected:
                        with QSignalBlocker(self):
                            for other in selected:
                                other.setSelected(False)
                        self.itemSelectionChanged.emit()
                        self.toggled_lock = True
            if self.pressed_problem or self.pressed_relationship or self.toggled_lock:
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.toggled_lock:
            self.toggled_lock = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.pressed_problem:
            pressed, self.pressed_problem = self.pressed_problem, None
            if (hit := self.problem_at(event.position().toPoint())) and hit[0] == pressed:
                self.problem_selected.emit(pressed)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.pressed_relationship:
            pressed = self.pressed_relationship
            self.pressed_relationship = None
            hit = self.relationship_at(event.position().toPoint())
            if hit and hit[0] == pressed:
                self.relationship_selected.emit(pressed)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        cursor = Qt.CursorShape.PointingHandCursor if self.problem_at(point) else Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip and (hit := self.problem_at(event.pos())):
            name = "查看需要处理的问题" if hit[1] == "error" else "查看黄色警告"
            QToolTip.showText(event.globalPos(), name, self.viewport())
            return True
        if event.type() == QEvent.Type.ToolTip and (hit := self.relationship_at(event.pos())):
            QToolTip.showText(event.globalPos(), hit[1], self.viewport())
            return True
        return super().viewportEvent(event)

    def dropEvent(self, event):
        self.clear_drop_line()
        if not self.accepts_drag(event):
            event.ignore()
            return
        try:
            payload = json.loads(bytes(event.mimeData().data(MIME)))
            item = self.itemAt(event.position().toPoint())
            boundary = 0
            if self.side == "profile":
                boundary = self.total_entries
                if item:
                    below = event.position().y() > self.visualItemRect(item).center().y()
                    boundary = snap_boundary(self.units, self.boundary_at(item, below))
            source = event.source()
            if isinstance(source, GroupDragHandle):
                source.result = (self, payload, boundary)
            elif isinstance(source, GroupMemberTree):
                QTimer.singleShot(0, self, lambda: self.dropped.emit(self.side, payload, boundary))
            else:
                self.dropped.emit(self.side, payload, boundary)
            event.acceptProposedAction()
        except (ValueError, KeyError, TypeError):
            event.ignore()

    @staticmethod
    def boundary_at(item, below):
        data = item.data(ROLE)
        return data["value"] + int(below and "group" not in data)

    def line_for_boundary(self, boundary):
        for index in range(self.count()):
            item = self.item(index)
            if item.data(ROLE)["value"] == boundary:
                return self.visualItemRect(item).top()
        if self.count():
            return self.visualItemRect(self.item(self.count() - 1)).bottom()
        return None
