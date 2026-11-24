from __future__ import annotations

from PySide6.QtCore import QEvent, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from darksort.classification import Category
from darksort.search import SearchRecord, search_text
from darksort.ui.theme import icon

ROLE = Qt.ItemDataRole.UserRole
RESULT_LIMIT = 250
FACETS = {"source": "来源", "category": "分类", "tag": "标签", "status": "状态"}
BROWSE_ORDERS = (("name", "名称 ↑"), ("name_desc", "名称 ↓"), ("source", "来源"), ("category", "分类"))


def plain_label(text: str, object_name: str = "muted") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(object_name)
    return widget


def search_icon(button, glyph, name):
    button.setIcon(icon(glyph))
    button.setProperty("glyph", glyph)
    button.setAccessibleName(name)
    button.setToolTip(name)


class ListSearchBar(QFrame):
    filters_changed = Signal()
    locate_changed = Signal()
    step = Signal(int)

    def __init__(self, side: str):
        super().__init__(objectName="paneFilters")
        self.side = side
        self.mode = "locate"
        self.texts = {"locate": "", "filter": ""}
        self.filter_query = ""
        self.conditions: dict[str, str] = {}
        self.order = "name"
        self.options = {
            "source": [("workshop", "创意工坊"), ("local", "本地")],
            "category": [(category.value, category.label) for category in Category] + [("other", "待归类")],
            "tag": [],
        }
        if side == "profile":
            self.options["category"].append(("unresolved", "待匹配"))
            self.options["status"] = [("missing", "待匹配"), ("warning", "有提示"), ("conflict", "文件问题"), ("lock", "联锁")]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 6, 7, 6)
        layout.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.mode_buttons = {}
        self.modes = QButtonGroup(self)
        for mode, title in (("locate", "定位"), ("filter", "筛选")):
            toggle = QToolButton(objectName="searchMode")
            toggle.setText(title)
            toggle.setCheckable(True)
            toggle.setChecked(mode == self.mode)
            toggle.setToolTip("保留当前列表，依次查看命中" if mode == "locate" else "缩小当前列的显示范围")
            toggle.clicked.connect(lambda _checked, mode=mode: self.set_mode(mode))
            self.modes.addButton(toggle)
            self.mode_buttons[mode] = toggle
            top.addWidget(toggle)
        self.query = QLineEdit(objectName="listSearch")
        self.query.setAccessibleName("搜索可用 Mod" if side == "library" else "搜索计划顺序")
        self.query.setPlaceholderText("名称、标签或 ID")
        self.query.setClearButtonEnabled(True)
        self.query.setMinimumWidth(72)
        self.query.textChanged.connect(self.change_query)
        self.query.returnPressed.connect(self.advance)
        previous = QShortcut(self.query)
        previous.setKeys([QKeySequence("Shift+Return"), QKeySequence("Shift+Enter")])
        previous.setContext(Qt.ShortcutContext.WidgetShortcut)
        previous.activated.connect(lambda: self.step.emit(-1) if self.mode == "locate" else None)
        top.addWidget(self.query, 1)
        layout.addLayout(top)

        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.advanced = QToolButton(objectName="searchOptions")
        self.advanced.setText("条件")
        search_icon(self.advanced, "down", "筛选与排序条件" if side == "library" else "筛选条件")
        self.advanced.setIconSize(QSize(12, 12))
        self.advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = QMenu(self.advanced)
        self.menu.aboutToShow.connect(self.build_menu)
        self.advanced.setMenu(self.menu)
        actions.addWidget(self.advanced)
        self.inline_chip = QHBoxLayout()
        actions.addLayout(self.inline_chip)
        actions.addStretch(1)
        self.summary = plain_label("", "smallCaps")
        actions.addWidget(self.summary)
        self.count = plain_label("", "countBadge")
        actions.addWidget(self.count)
        self.steps = []
        for glyph, title, offset in (("previous", "上一个命中", -1), ("next", "下一个命中", 1)):
            item = QToolButton(objectName="searchStep")
            search_icon(item, glyph, title)
            item.clicked.connect(lambda _checked, step=offset: self.step.emit(step))
            item.setFixedWidth(22)
            self.steps.append(item)
            actions.addWidget(item)
        layout.addLayout(actions)
        self.badges = QWidget()
        self.badge_layout = QGridLayout(self.badges)
        self.badge_layout.setContentsMargins(0, 0, 0, 0)
        self.badge_layout.setSpacing(3)
        self.badge_layout.setColumnStretch(2, 1)
        self.badges.hide()
        layout.addWidget(self.badges)
        self.filter_delay = QTimer(self)
        self.filter_delay.setSingleShot(True)
        self.filter_delay.setInterval(120)
        self.filter_delay.timeout.connect(self.notify_filters)
        self.update_count(0, 0, 0)

    @property
    def filtering(self):
        return bool(self.filter_query or self.conditions)

    def set_mode(self, mode):
        if self.mode == mode:
            return
        if self.filter_delay.isActive():
            self.notify_filters()
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        with QSignalBlocker(self.query):
            self.query.setText(self.texts[mode])
        self.update_chips()
        self.locate_changed.emit()

    def change_query(self, text):
        self.texts[self.mode] = text
        if self.mode == "locate":
            self.locate_changed.emit()
        else:
            self.filter_delay.start()

    def advance(self):
        if self.mode == "locate":
            self.step.emit(1)
        elif self.filter_delay.isActive():
            self.notify_filters()

    def set_condition(self, facet, value):
        if value:
            self.conditions[facet] = value
        else:
            self.conditions.pop(facet, None)
        self.notify_filters()

    def notify_filters(self):
        self.filter_delay.stop()
        self.filter_query = search_text(self.texts["filter"])
        self.update_chips()
        self.filters_changed.emit()

    def clear_condition(self, facet):
        if facet == "text":
            self.texts["filter"] = ""
            if self.mode == "filter":
                with QSignalBlocker(self.query):
                    self.query.clear()
            self.notify_filters()
        else:
            self.set_condition(facet, "")

    def clear_filters(self):
        self.clear_view(filters_only=True)
        self.notify_filters()

    def clear_view(self, *, filters_only=False):
        self.filter_delay.stop()
        self.conditions.clear()
        self.filter_query = ""
        self.texts["filter"] = ""
        if not filters_only:
            self.texts["locate"] = ""
        with QSignalBlocker(self.query):
            self.query.setText(self.texts[self.mode])
        self.update_chips()

    def update_options(self, tags):
        self.options["tag"] = tags
        if self.conditions.get("tag") not in {key for key, _ in tags}:
            self.conditions.pop("tag", None)
        self.update_chips()

    def update_chips(self):
        for layout in (self.inline_chip, self.badge_layout):
            while layout.count():
                item = layout.takeAt(0)
                item.widget().hide()
                item.widget().deleteLater()
        active = [(facet, dict(self.options[facet]).get(key, key)) for facet, key in self.conditions.items()]
        if self.texts["filter"].strip():
            active.insert(0, ("text", self.texts["filter"].strip()))
        inline = int(bool(active) and self.mode == "filter")
        for index, (facet, title) in enumerate(active):
            chip = QToolButton(objectName="searchChip")
            short = chip.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 63)
            chip.setText(f"{short} ×")
            chip.setAccessibleName(f"清除筛选{title}")
            chip.setToolTip(f"清除筛选 {title}")
            chip.clicked.connect(lambda _checked, facet=facet: self.clear_condition(facet))
            if index < inline:
                self.inline_chip.addWidget(chip)
            else:
                grid_index = index - inline
                row, column = divmod(grid_index, 2)
                self.badge_layout.addWidget(chip, row, column, Qt.AlignmentFlag.AlignLeft)
        self.badges.setVisible(len(active) > inline)

    def build_menu(self):
        self.menu.clear()
        for facet in FACETS:
            if facet not in self.options:
                continue
            menu = self.menu.addMenu(FACETS[facet])
            selected = self.conditions.get(facet, "")
            self.add_choices(menu, [("", "全部"), *self.options[facet]], selected,
                             lambda value, facet=facet: self.set_condition(facet, value))
        if self.side == "library":
            self.add_choices(self.menu.addMenu("浏览排序"), BROWSE_ORDERS, self.order, self.set_order)
        self.menu.addSeparator()
        self.menu.addAction("清除本列筛选", self.clear_filters).setEnabled(self.filtering)

    @staticmethod
    def add_choices(menu, options, selected, choose):
        group = QActionGroup(menu)
        for key, text in options:
            action = menu.addAction(text)
            group.addAction(action)
            action.setCheckable(True)
            action.setChecked(key == selected)
            action.triggered.connect(lambda _checked, key=key: choose(key))

    def set_order(self, order):
        if self.order != order:
            self.order = order
            self.filters_changed.emit()

    def update_count(self, hits, position, hidden):
        locating = self.mode == "locate" and bool(self.texts["locate"].strip())
        self.count.setVisible(locating)
        roomy = self.side == "profile" or not self.inline_chip.count()
        self.summary.setVisible(roomy and bool(self.summary.text()) and not locating)
        self.count.setText(f"{position}/{hits}" if position else f"{hits} 命中")
        self.count.setToolTip(f"{hits} 项命中 · {hidden} 项被本列筛选隐藏")
        for button in self.steps:
            button.setVisible(locating)
            button.setEnabled(bool(hits))


class GlobalSearchDialog(QDialog):
    def __init__(self, records: list[SearchRecord], parent=None):
        super().__init__(parent)
        self.setWindowTitle("搜全部 · DarkSort")
        self.resize(670, 483)
        self.records = records
        self.target = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 12, 13, 12)
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索名称、标签、分类或 Workshop ID")
        self.query.setAccessibleName("全局搜索 Mod")
        self.query.setClearButtonEnabled(True)
        self.query.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.query.textChanged.connect(self.refresh)
        self.query.installEventFilter(self)
        layout.addWidget(self.query)
        self.summary = plain_label("")
        layout.addWidget(self.summary)
        self.results = QListWidget(objectName="globalResults")
        self.results.setAccessibleName("全局搜索结果")
        self.results.itemActivated.connect(self.choose)
        layout.addWidget(self.results, 1)
        controls = QDialogButtonBox()
        self.jump = controls.addButton("跳转到 Mod", QDialogButtonBox.ButtonRole.AcceptRole)
        self.jump.setObjectName("primary")
        controls.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        controls.accepted.connect(lambda: self.choose(self.results.currentItem()))
        controls.rejected.connect(self.reject)
        self.results.currentItemChanged.connect(lambda item, _: self.jump.setEnabled(item is not None))
        layout.addWidget(controls)
        self.refresh("")
        self.query.setFocus()

    def refresh(self, text):
        query = search_text(text)
        matched = []
        if query:
            matched = [(rank, record) for record in self.records if (rank := record.rank(query)) is not None]
            matched.sort(key=lambda item: item[0])
        self.results.clear()
        for _, record in matched[:RESULT_LIMIT]:
            item = QListWidgetItem(f"{record.title}\n{record.location}")
            item.setData(ROLE, record.target)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)
        more = f" · 显示前 {RESULT_LIMIT} 项" if len(matched) > RESULT_LIMIT else ""
        self.summary.setText(f"{len(matched)} 项{more}" if query else "查找可用库、计划和暂存篮中的 Mod")
        self.jump.setEnabled(bool(self.results.count()))

    def choose(self, item):
        if item is not None:
            self.target = item.data(ROLE)
            self.accept()

    def eventFilter(self, watched, event):
        if watched is self.query and event.type() == QEvent.Type.KeyPress and self.results.count():
            key = event.key()
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                offset = 1 if key == Qt.Key.Key_Down else -1
                self.results.setCurrentRow((self.results.currentRow() + offset) % self.results.count())
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.choose(self.results.currentItem())
                return True
        return super().eventFilter(watched, event)
