from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QTabWidget,
    QVBoxLayout,
)

from darksort.ui.mod_list import ROLE
from darksort.ui.theme import icon

MENU_SCOPES = {"library": "可用 Mod", "profile": "计划顺序", "group": "分组成员"}
DEFAULT_ITEMS = {
    "library": ("add", "group", "edit_text", "folder", "workshop"),
    "profile": ("remove", "problem", "group", "edit_text", "folder", "workshop", "relink"),
    "group": ("locate", "match_member", "add", "remove", "edit_text", "folder", "workshop"),
}


@dataclass(frozen=True)
class MenuItemInfo:
    title: str
    section: str
    scopes: tuple[str, ...] = ()

    def applies_to(self, scope):
        return not self.scopes or scope in self.scopes


PLAN = "计划与分组"
MOD_INFO = "模组信息"
WORKSHOP = "创意工坊"
FILES = "文件与扫描"
MENU_ITEMS = {
    "add": MenuItemInfo("加入计划", PLAN, ("library", "group")),
    "add_lock": MenuItemInfo("加入计划并联锁", PLAN, ("library", "group")),
    "add_heading": MenuItemInfo("加入计划并设标题…", PLAN, ("library", "group")),
    "remove": MenuItemInfo("移出计划", PLAN, ("profile", "group")),
    "locate": MenuItemInfo("在列表定位", PLAN, ("group",)),
    "group": MenuItemInfo("分组管理", PLAN),
    "match_member": MenuItemInfo("手动匹配本机 Mod…", PLAN, ("group",)),
    "remove_member": MenuItemInfo("从此分组移出", PLAN, ("group",)),
    "heading": MenuItemInfo("添加或编辑分组标题…", PLAN, ("profile", "group")),
    "rule": MenuItemInfo("添加排序规则…", PLAN),
    "requires": MenuItemInfo("设置前置…", PLAN),
    "lock": MenuItemInfo("联锁选中项", PLAN, ("profile", "group")),
    "unlock": MenuItemInfo("解除联锁", PLAN, ("profile", "group")),
    "relink": MenuItemInfo("匹配此 Mod…", PLAN, ("profile", "group")),
    "match_pending": MenuItemInfo("匹配待定位条目…", PLAN, ("library", "group")),
    "relations": MenuItemInfo("查看此 Mod 的关联", PLAN),
    "problem": MenuItemInfo("看看这个 Mod 有什么问题", PLAN, ("profile", "group")),
    "edit_text": MenuItemInfo("编辑别名、简介与备注…", MOD_INFO),
    "tags": MenuItemInfo("编辑标签…", MOD_INFO),
    "color": MenuItemInfo("标记颜色", MOD_INFO),
    "type": MenuItemInfo("修改类型…", MOD_INFO),
    "workshop": MenuItemInfo("打开创意工坊", WORKSHOP),
    "workshop_browser": MenuItemInfo("在浏览器打开工坊", WORKSHOP),
    "workshop_copy": MenuItemInfo("复制工坊链接", WORKSHOP),
    "workshop_link": MenuItemInfo("关联创意工坊…", WORKSHOP),
    "unsubscribe": MenuItemInfo("在 Steam 取消订阅…", WORKSHOP),
    "folder": MenuItemInfo("打开 Mod 文件夹", FILES),
    "scan_notice": MenuItemInfo("查看扫描提示", FILES),
    "replace_files": MenuItemInfo("转入游戏文件替换…", FILES),
    "exclude": MenuItemInfo("从扫描中排除", FILES),
    "uninstall": MenuItemInfo("卸载本地 Mod…", FILES),
}


@dataclass(frozen=True)
class MenuCommand:
    run: Callable | None = None
    submenu: Callable[[QMenu], None] | None = None
    title: str = ""
    enabled: bool = True
    glyph: str = ""

    def add_to(self, menu, key):
        title = self.title or MENU_ITEMS[key].title
        if self.submenu:
            self.submenu(menu.addMenu(title))
        else:
            action = menu.addAction(title, self.run)
            action.setEnabled(self.enabled)
            if self.glyph:
                action.setIcon(icon(self.glyph))


def quick_items(scope, settings):
    return [
        key for key in settings.mod_menu_items.get(scope, DEFAULT_ITEMS[scope])
        if key in MENU_ITEMS and MENU_ITEMS[key].applies_to(scope)
    ]


def popup_mod_menu(parent, point, scope, commands, settings, configure, *, mode=None):
    mode = mode or settings.mod_menu_mode
    point = QPoint(point)
    menu = QMenu(parent)
    menu.setObjectName("modContextMenu")
    menu.setProperty("menuMode", mode)
    menu.aboutToHide.connect(menu.deleteLater)
    if mode == "full":
        sections = {}
        for key, info in MENU_ITEMS.items():
            if key not in commands:
                continue
            if info.section not in sections:
                sections[info.section] = menu.addMenu(info.section)
            commands[key].add_to(sections[info.section], key)
    else:
        for key in quick_items(scope, settings):
            if key in commands and commands[key].enabled:
                commands[key].add_to(menu, key)

    if menu.actions():
        menu.addSeparator()

    def switch():
        next_mode = "quick" if mode == "full" else "full"
        # Wait for Qt to release the first popup and its grab before opening the next one.
        menu.destroyed.connect(lambda: QTimer.singleShot(
            0, parent, lambda: popup_mod_menu(parent, point, scope, commands, settings, configure, mode=next_mode),
        ))
        menu.hide()

    menu.addAction("返回常用选项" if mode == "full" else "显示详细选项", switch)
    if mode == "full":
        menu.addAction("自定义常用选项…", lambda: configure(scope))
    menu.popup(point)


class ModMenuDialog(QDialog):
    def __init__(self, settings, parent=None, *, scope="library"):
        super().__init__(parent)
        self.setWindowTitle("右键菜单设置")
        self.resize(540, 565)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("常用选项", "quick")
        self.mode.addItem("详细选项", "full")
        self.mode.setCurrentIndex(self.mode.findData(settings.mod_menu_mode))
        form.addRow("右键默认打开", self.mode)
        layout.addLayout(form)

        hint = QLabel("勾选常用动作，拖动可调整顺序。菜单只显示当前条目可用的常用动作。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        self.lists = {}
        for side, title in MENU_SCOPES.items():
            choices = QListWidget()
            choices.setAccessibleName(f"{title}的常用右键动作")
            choices.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            choices.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.lists[side] = choices
            self.tabs.addTab(choices, title)
            self.fill(side, quick_items(side, settings))
        self.tabs.setCurrentWidget(self.lists[scope])
        layout.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox()
        buttons.addButton("恢复本栏默认", QDialogButtonBox.ButtonRole.ResetRole).clicked.connect(
            self.restore_tab,
        )
        buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole).setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def fill(self, side, checked):
        choices = self.lists[side]
        choices.clear()
        remaining = (key for key, info in MENU_ITEMS.items() if info.applies_to(side))
        for key in dict.fromkeys((*checked, *remaining)):
            info = MENU_ITEMS[key]
            item = QListWidgetItem(info.title)
            item.setData(ROLE, key)
            item.setToolTip(info.section)
            item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsDropEnabled)
            item.setCheckState(Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked)
            choices.addItem(item)

    def restore_tab(self):
        side = tuple(MENU_SCOPES)[self.tabs.currentIndex()]
        self.fill(side, DEFAULT_ITEMS[side])

    def result_items(self):
        return {
            side: [
                choices.item(i).data(ROLE) for i in range(choices.count())
                if choices.item(i).checkState() == Qt.CheckState.Checked
            ]
            for side, choices in self.lists.items()
        }
