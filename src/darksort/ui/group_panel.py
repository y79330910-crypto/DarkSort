from dataclasses import dataclass, replace

from PySide6.QtCore import QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from darksort.models import ModGroup, ProfileEntry
from darksort.search import ModLocation, search_text
from darksort.settings import MARK_COLORS
from darksort.ui.mod_list import ROLE, GroupDragHandle
from darksort.ui.theme import current_theme, icon, problem_color


@dataclass(frozen=True)
class GroupMemberRow:
    entry: ProfileEntry
    name: str
    target: ModLocation | None
    status: str
    tags: tuple[str, ...] = ()


def icon_control(glyph, title, action, widget=None):
    button = widget if widget is not None else QToolButton()
    button.setObjectName("groupControl")
    button.setIcon(icon(glyph))
    button.setIconSize(QSize(15, 15))
    button.setFixedWidth(23)
    button.setToolTip(title)
    button.setAccessibleName(title)
    if action is not None:
        button.clicked.connect(action)
    return button


class GroupShelf(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.data = None
        self.collapsed = set()
        self.search_collapsed = set()
        self.headers = {}
        self.drag_active = False
        self.pending_render = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.search = QLineEdit(placeholderText="搜索分组或 Mod")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索分组或组内 Mod")
        self.search.textChanged.connect(self.filter_groups)
        layout.addWidget(self.search)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        toolbar.addWidget(icon_control("expand", "展开全部分组", lambda: self.expand_all(True)))
        toolbar.addWidget(icon_control("collapse", "折叠全部分组", lambda: self.expand_all(False)))
        toolbar.addStretch()
        templates = QPushButton("载入模板", objectName="quiet")
        templates.setToolTip("从文件载入一个分组模板")
        templates.clicked.connect(window.import_group_template)
        toolbar.addWidget(templates)
        create = QPushButton("新建分组", objectName="quiet")
        create.setIcon(icon("plus"))
        create.clicked.connect(lambda: window.edit_mod_group())
        toolbar.addWidget(create)
        layout.addLayout(toolbar)
        self.empty = QLabel(objectName="muted")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.tree = QTreeWidget(objectName="groupShelf")
        self.tree.setAccessibleName("Mod 分组")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(8)
        self.tree.setRootIsDecorated(False)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.member_menu)
        self.tree.itemClicked.connect(self.show_member)
        self.tree.itemActivated.connect(self.show_member)
        self.tree.itemExpanded.connect(lambda item: self.remember_expansion(item, True))
        self.tree.itemCollapsed.connect(lambda item: self.remember_expansion(item, False))
        layout.addWidget(self.tree, 1)

    def set_groups(self, profile_id, groups):
        data = (profile_id, tuple(groups), current_theme())
        if self.data == data:
            return
        if self.data is None or self.data[0] != profile_id:
            self.collapsed = {group.id for group, _ in data[1][1:]}
            self.search_collapsed.clear()
            with QSignalBlocker(self.search):
                self.search.clear()
        self.data = data
        self.render()

    def expand_all(self, expanded):
        if self.data:
            collapsed = self.collapse_state()
            collapsed.clear()
            if not expanded:
                collapsed.update(group.id for group, _ in self.data[1])
        self.render()

    def filter_groups(self):
        self.search_collapsed.clear()
        self.render()

    def collapse_state(self):
        return self.search_collapsed if self.search.text().strip() else self.collapsed

    def set_drag_active(self, active):
        self.drag_active = active
        if self.pending_render and not active:
            QTimer.singleShot(0, self, self.render)

    def remember_expansion(self, item, expanded):
        data = item.data(0, ROLE)
        if not isinstance(data, str):
            return
        collapsed = self.collapse_state()
        if expanded:
            collapsed.discard(data)
        else:
            collapsed.add(data)
        control = self.headers.get(data)
        if control:
            control.setIcon(icon("down" if expanded else "next"))
            text = "折叠分组" if expanded else "展开分组"
            control.setToolTip(text)
            control.setAccessibleName(text)

    def render(self, *_):
        if self.data is None:
            return
        if self.drag_active:
            self.pending_render = True
            return
        self.pending_render = False
        profile_id, groups, _ = self.data
        query = search_text(self.search.text())
        scroll = self.tree.verticalScrollBar()
        position = scroll.value()
        with QSignalBlocker(self.tree):
            self.headers.clear()
            self.tree.clear()
            for group, rows in groups:
                indexed = list(enumerate(rows, 1))
                if query and query not in search_text(group.title):
                    indexed = [
                        (number, row) for number, row in indexed if all(token in search_text(
                            f"{row.name} {row.entry.name} {row.entry.workshop_id or ''} {' '.join(row.tags)}"
                        ) for token in query.split())
                    ]
                    if not indexed:
                        continue
                item = QTreeWidgetItem(self.tree, [group.title])
                item.setData(0, ROLE, group.id)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setSizeHint(0, QSize(0, 32))
                open_group = group.id not in self.collapse_state()
                header = self.make_header(profile_id, group, rows, item, open_group)
                self.tree.setItemWidget(item, 0, header)
                for number, row in indexed:
                    self.add_member(item, number, row)
                item.setExpanded(open_group)
            scroll.setValue(position)
        shown = self.tree.topLevelItemCount()
        self.empty.setVisible(not shown)
        self.empty.setText(
            "没有找到匹配的分组或组员。" if query
            else "选中 Mod 后右键加入分组，也可以新建分组或载入模板。"
        )

    def make_header(self, profile_id, group, rows, item, expanded):
        theme = current_theme()
        card = QFrame(objectName="modGroupHeader")
        card.setStyleSheet(
            f"QFrame#modGroupHeader {{ background: {theme.tint(group.color, 0.16).name()};"
            f" border: 1px solid {theme.tint(group.color, 0.42).name()};"
            f" border-left: 3px solid {group.color}; border-radius: 6px; }}"
        )
        header = QHBoxLayout(card)
        header.setContentsMargins(3, 2, 3, 2)
        header.setSpacing(2)
        grip = GroupDragHandle(profile_id, group.id, group.title, card)
        icon_control("grip", grip.toolTip(), None, grip)
        grip.setAccessibleName(f"拖动分组 {group.title}")
        grip.setEnabled(bool(rows))
        grip.drag_state_changed.connect(self.set_drag_active)
        header.addWidget(grip)
        toggle = icon_control("down" if expanded else "next", "折叠分组" if expanded else "展开分组",
                              lambda: item.setExpanded(not item.isExpanded()))
        toggle.setEnabled(bool(rows))
        self.headers[group.id] = toggle
        header.addWidget(toggle)
        name = QLabel(group.title, objectName="section")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setToolTip(group.title)
        header.addWidget(name, 1)
        active = sum(bool(row.target and row.target.side == "profile") for row in rows)
        ready = sum(bool(row.target and row.target.side in {"library", "staging"}) for row in rows)
        count = QLabel(f"{active}/{len(rows)}", objectName="smallCaps")
        count.setToolTip(f"{active} 项已加入计划，组内共有 {len(rows)} 项")
        header.addWidget(count)
        removing = bool(active and not ready)
        direction = "library" if removing else "profile"
        activate = icon_control(
            "minus" if removing else "plus", "整组移出计划" if removing else "整组加入计划",
            lambda: self.window.apply_mod_group(group.id, direction),
        )
        activate.setEnabled(bool(active or ready))
        header.addWidget(activate)
        more = icon_control("more", f"管理分组 {group.title}", None)
        menu = QMenu(more)
        menu.addAction("整组加入计划", lambda: self.window.apply_mod_group(group.id, "profile")).setEnabled(bool(active or ready))
        menu.addAction("整组移出计划", lambda: self.window.apply_mod_group(group.id, "library")).setEnabled(bool(active))
        menu.addSeparator()
        menu.addAction("编辑名称、颜色和组员顺序…", lambda: self.window.edit_mod_group(group.id))
        menu.addAction("保存为分组模板…", lambda: self.window.export_group_template(group.id))
        menu.addAction("删除分组…", lambda: self.window.delete_mod_group(group.id))
        more.setMenu(menu)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        header.addWidget(more)
        return card

    @staticmethod
    def add_member(item, number, row):
        child = QTreeWidgetItem(item, [f"{number:02d}  {row.name}"])
        child.setData(0, ROLE, row)
        child.setToolTip(0, f"{row.name}\n{row.entry.name}\n{row.status}" if row.name != row.entry.name
                         else f"{row.name}\n{row.status}")
        theme = current_theme()
        side = row.target.side if row.target else "missing"
        color = {"profile": theme.accent, "staging": problem_color("warning", theme),
                 "library": theme.dim}.get(side, problem_color("error", theme))
        child.setIcon(0, icon("dot", color, 13))

    def show_member(self, item, _column):
        row = item.data(0, ROLE)
        if isinstance(row, GroupMemberRow) and row.target:
            self.window.navigate(row.target)

    def member_menu(self, point):
        item = self.tree.itemAt(point)
        if not item or not item.parent():
            return
        row, identity = item.data(0, ROLE), item.parent().data(0, ROLE)
        menu = QMenu(self)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.addAction("在列表定位", lambda: self.window.navigate(row.target)).setEnabled(bool(row.target))
        if row.target is None:
            menu.addAction("手动匹配本机 Mod…", lambda: self.window.relink_group_member(identity, row.entry.reference))
        menu.addAction("从此分组移出", lambda: self.window.remove_group_member(identity, row.entry.reference))
        menu.popup(self.tree.viewport().mapToGlobal(point))


class ModGroupDialog(QDialog):
    def __init__(self, group: ModGroup, title_for, parent=None):
        super().__init__(parent)
        self.group = group
        self.setWindowTitle("编辑 Mod 分组")
        self.resize(490, 450)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title = QLineEdit(group.title, maxLength=120)
        self.title.setPlaceholderText("例如 机器人系列")
        self.colors = QComboBox()
        for title, color in MARK_COLORS:
            self.colors.addItem(icon("dot", color), title, color)
        if self.colors.findData(group.color) == -1:
            self.colors.addItem(icon("dot", group.color), group.color, group.color)
        self.colors.setCurrentIndex(self.colors.findData(group.color))
        form.addRow("分组名称", self.title)
        form.addRow("分组颜色", self.colors)
        layout.addLayout(form)
        layout.addWidget(QLabel("拖动组员调整整组加入计划的顺序。"))
        self.members = QListWidget()
        self.members.setAccessibleName("分组成员顺序")
        self.members.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.members.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.members.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for member in group.members:
            item = QListWidgetItem(title_for(member.reference, member.name))
            item.setToolTip(member.name)
            item.setData(ROLE, member)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.members.addItem(item)
        layout.addWidget(self.members, 1)
        remove = QPushButton("移出所选组员", objectName="quiet")
        remove.setEnabled(False)
        self.members.itemSelectionChanged.connect(lambda: remove.setEnabled(bool(self.members.selectedItems())))
        remove.clicked.connect(self.remove_selected)
        layout.addWidget(remove, 0, Qt.AlignmentFlag.AlignLeft)
        buttons = QDialogButtonBox()
        save = buttons.addButton("保留分组", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("primary")
        save.setEnabled(bool(group.title))
        self.title.textChanged.connect(lambda text: save.setEnabled(bool(text.strip())))
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def remove_selected(self):
        for item in self.members.selectedItems():
            self.members.takeItem(self.members.row(item))

    def result_group(self):
        return replace(self.group, title=self.title.text().strip(), color=self.colors.currentData(),
                       members=tuple(self.members.item(i).data(ROLE) for i in range(self.members.count())))
