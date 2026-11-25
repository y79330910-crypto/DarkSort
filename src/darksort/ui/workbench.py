from __future__ import annotations

from PySide6.QtCore import QRect, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from darksort.settings import MARK_COLORS
from darksort.ui.appearance import ModArtwork, TagChips
from darksort.ui.conflict_dialog import ConflictPanel
from darksort.ui.group_panel import GroupShelf
from darksort.ui.mod_list import ROLE, ColumnHeader, ModList
from darksort.ui.mod_text_batch import ModTextBatchPanel
from darksort.ui.search import ListSearchBar
from darksort.ui.theme import current_theme, icon, paint_icon, problem_color
from darksort.ui.workspace_dock import WorkspaceDock


def label(text: str, name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, *, primary=False, name="", glyph=""):
    widget = QPushButton(text)
    widget.setObjectName(name or ("primary" if primary else "quiet"))
    if glyph:
        widget.setIcon(icon(glyph))
        widget.setProperty("glyph", glyph)
    widget.clicked.connect(callback)
    return widget


def frame(name: str, *, vertical=False, margins=(0, 0, 0, 0), spacing=0):
    widget = QFrame()
    widget.setObjectName(name)
    layout = QVBoxLayout(widget) if vertical else QHBoxLayout(widget)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return widget, layout


class WorkspaceCombo(QComboBox):
    def __init__(self):
        super().__init__()
        self.setObjectName("workspaceCombo")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        box = QRect(self.width() - 16, self.height() // 2 - 6, 12, 12)
        paint_icon(painter, box, "down", QColor(current_theme().muted))
        painter.end()


def build_workbench(window):
    create_menus(window)
    central = QWidget()
    window.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setSpacing(9)
    layout.setContentsMargins(12, 9, 12, 6)
    layout.addWidget(create_toolbar(window))

    window.workspace_split = QSplitter(Qt.Orientation.Vertical)
    window.workspace_split.setHandleWidth(8)
    window.panes = QSplitter(Qt.Orientation.Horizontal)
    window.panes.setHandleWidth(8)
    window.panes.setChildrenCollapsible(False)
    window.detail_panel = create_details(window)
    window.panes.addWidget(window.detail_panel)
    window.panes.addWidget(create_library(window))
    window.panes.addWidget(create_plan(window))
    window.utility = UtilityPane(window)
    window.panes.addWidget(window.utility)
    window.panes.setSizes([246, 283, 650, 282])
    window.panes.setStretchFactor(2, 1)
    window.workspace_split.addWidget(window.panes)
    window.dock = WorkspaceDock(window)
    window.workspace_split.addWidget(window.dock)
    window.workspace_split.setCollapsible(0, False)
    window.workspace_split.setSizes([625, 162])
    window.workspace_split.setStretchFactor(0, 1)
    layout.addWidget(window.workspace_split, 1)
    window.progress_bar = QProgressBar()
    window.progress_bar.setTextVisible(False)
    window.progress_bar.hide()
    layout.addWidget(window.progress_bar)
    window.mod_text_batch = ModTextBatchPanel(window)
    layout.addWidget(window.mod_text_batch)

    status = window.statusBar()
    status.setSizeGripEnabled(False)
    window.scan_summary = label("等待扫描", "muted")
    window.dirty_label = label("已保存", "muted")
    status.addPermanentWidget(window.scan_summary)
    status.addPermanentWidget(window.dirty_label)


def create_menus(window):
    menu = window.menuBar()
    menu.setNativeMenuBar(False)
    window.brand, wordmark = frame("brand", margins=(5, 0, 15, 0), spacing=7)
    window.brand_icon = label("")
    window.brand_icon.setPixmap(icon("brand", current_theme().accent, 22).pixmap(22, 22))
    wordmark.addWidget(window.brand_icon)
    wordmark.addWidget(label("DarkSort", "wordmark"))
    menu.setCornerWidget(window.brand, Qt.Corner.TopLeftCorner)
    file = menu.addMenu("文件")
    file.addAction("新建方案…", window.new_profile)
    file.addAction("复制当前方案…", window.duplicate_profile)
    file.addSeparator()
    file.addAction("导入方案…", window.import_profile)
    file.addAction("导出方案…", window.export_profile)
    file.addSeparator()
    file.addAction("生成分享包…", window.export_share_package)
    file.addAction("导入分享包…", window.import_share_package)
    file.addSeparator()
    file.addAction("保存方案", window.save_profile)
    file.addAction("带方案开新档…", lambda: window.open_new_campaign())
    file.addAction("退出", window.close)

    edit = menu.addMenu("编辑")
    window.undo_action = window.undo_stack.createUndoAction(window, "撤销")
    window.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
    edit.addAction(window.undo_action)
    window.redo_action = window.undo_stack.createRedoAction(window, "重做")
    window.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
    edit.addAction(window.redo_action)
    edit.addSeparator()
    edit.addAction("搜全部…", window.search_all)
    edit.addAction("加入所选", window.add_selected)
    edit.addAction("加入所有可见 Mod", window.add_visible)
    edit.addAction("暂存所选可用 Mod", window.stage_available)
    edit.addAction("移除所选", window.remove_selected)
    edit.addAction("暂存计划所选", window.stage_plan)
    edit.addAction("上移", lambda: window.nudge(-1))
    edit.addAction("下移", lambda: window.nudge(1))
    edit.addSeparator()
    edit.addAction("新增或编辑分组…", window.edit_group)
    edit.addAction("联锁所选…", window.lock_selected)
    edit.addAction("解除所选联锁", window.unlock_selected)
    edit.addAction("手动定位…", window.relink_selected)

    view = menu.addMenu("视图")
    view.addAction("界面主题…", window.edit_appearance)
    view.addAction("显示或隐藏 Mod 详情", window.toggle_details)
    view.addAction("显示或隐藏工作区", window.toggle_utility)
    view.addAction("显示或隐藏诊断面板", window.toggle_dock)

    tools = menu.addMenu("工具")
    window.path_button = tools.addAction("扫描路径…", window.edit_paths)
    window.refresh_button = tools.addAction("强制刷新", lambda: window.start_scan(refresh=True))
    tools.addAction("用户规则…", window.edit_rules)
    tools.addSeparator()
    window.issues_button = tools.addAction("扫描提示", window.show_issues)
    window.issues_button.setEnabled(False)
    window.conflict_button = tools.addAction("冲突排查", window.show_conflicts)
    tools.addAction("方案历史…", window.show_history)
    tools.addAction("打开数据目录", window.open_data_folder)

    assistant = menu.addMenu("AI")
    assistant.addAction("打开 AI 助手", lambda: window.open_ai_assistant())
    assistant.addAction("接入设置与提示词…", window.configure_ai)
    assistant.addAction("AI 排序…", window.ai_sort_profile)
    assistant.addAction("AI 整理日志…", window.organize_logs)
    assistant.addSeparator()
    window.batch_text_action = assistant.addAction("批量生成别名与翻译", lambda: window.mod_text_batch.start())
    window.batch_text_progress_action = assistant.addAction("查看批量翻译进度", lambda: window.mod_text_batch.show())
    window.batch_text_progress_action.setEnabled(False)

    help_ = menu.addMenu("帮助")
    help_.addAction(
        "键盘快捷键",
        lambda: window.show_text(
            "键盘快捷键",
            "Ctrl+S   保存方案\nCtrl+Z   撤销\nCtrl+Y / Ctrl+Shift+Z   重做\n"
            "Ctrl+F   搜全部\nF5          扫描 Mod\n"
            "Ctrl+A    全选列表\nCtrl / Shift   多选\nShift+F10    打开 Mod 菜单",
        ),
    )
    window.path_summary = label("DD1 · 等待扫描", "muted")
    window.path_summary.setContentsMargins(7, 0, 6, 0)
    menu.setCornerWidget(window.path_summary, Qt.Corner.TopRightCorner)


def create_toolbar(window):
    toolbar, lane = frame("toolbar", margins=(10, 8, 10, 8), spacing=7)
    window.profile_combo = WorkspaceCombo()
    window.profile_combo.setAccessibleName("当前方案")
    window.profile_combo.setToolTip("切换方案")
    window.profile_combo.setMinimumWidth(108)
    window.profile_combo.setMaximumWidth(176)
    window.profile_combo.currentIndexChanged.connect(window.switch_profile)
    lane.addWidget(window.profile_combo)
    window.scan_button = button("扫描", window.start_scan, glyph="refresh")
    lane.addWidget(window.scan_button)
    lane.addWidget(QFrame(objectName="rule"))

    search_all = button("搜全部   Ctrl+F", window.search_all, name="globalSearch", glyph="search")
    search_all.setAccessibleName("搜全部")
    search_all.setToolTip("在可用、计划和暂存中查找 Mod")
    lane.addWidget(search_all)
    lane.addStretch(1)
    lane.addWidget(button("智能排序", lambda: window.sort_profile(), glyph="sort", name="sortNow"))
    options = QToolButton()
    options.setIcon(icon("down"))
    options.setProperty("glyph", "down")
    options.setObjectName("sortOptions")
    options.setFixedWidth(26)
    options.setAccessibleName("排序选项")
    options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    order_actions = QMenu(options)
    order_actions.addAction("仅按自定义规则", lambda: window.sort_profile(rules_only=True))
    order_actions.addAction("AI 排序…", window.ai_sort_profile)
    order_actions.addAction("编辑用户规则…", window.edit_rules)
    options.setMenu(order_actions)
    lane.addWidget(options)
    lane.addWidget(button("AI 助手", lambda: window.open_ai_assistant(), name="assistantButton"))
    window.save_button = button("保存方案", window.save_profile, name="saveProfile")
    lane.addWidget(window.save_button)
    open_campaign = button("带方案开新档…", lambda: window.open_new_campaign(), name="newCampaign")
    open_campaign.setToolTip("用当前计划启动游戏，新档第一次进入时预载整套 Mod")
    lane.addWidget(open_campaign)
    lane.addWidget(button("应用到存档…", window.manage_saves, name="applySave"))
    return toolbar


def create_library(window):
    panel, layout = frame("panel", vertical=True, margins=(1, 1, 1, 1))
    panel.setMinimumWidth(199)
    header, band = frame("paneHeader", margins=(11, 7, 10, 7), spacing=6)
    window.library_title = label("可用 Mod", "panelTitle")
    band.addWidget(window.library_title, 1)
    window.library_count = label("0", "countBadge")
    band.addWidget(window.library_count)
    layout.addWidget(header)
    window.library_search = create_column_search(window, "library")
    layout.addWidget(window.library_search)
    window.library_list = ModList("library")
    window.library_list.itemSelectionChanged.connect(lambda: window.show_selected_detail("library"))
    window.library_list.itemClicked.connect(lambda item: window.show_selected_detail("library", item))
    window.library_list.itemDoubleClicked.connect(lambda _: window.add_selected())
    window.library_list.dropped.connect(window.handle_drop)
    window.library_list.menu_requested.connect(window.open_mod_menu)
    layout.addWidget(ColumnHeader(window.library_list))
    layout.addWidget(window.library_list, 1)
    footer, actions = frame("paneFooter", margins=(7, 6, 7, 6), spacing=5)
    actions.addWidget(button("加入 →", window.add_selected, primary=True))
    actions.addWidget(button("暂存", window.stage_available))
    actions.addStretch()
    window.library_selection = label("", "smallCaps")
    actions.addWidget(window.library_selection)
    layout.addWidget(footer)
    return panel


def create_plan(window):
    panel, layout = frame("panel", vertical=True, margins=(1, 1, 1, 1))
    panel.setMinimumWidth(321)
    header, band = frame("paneHeader", margins=(11, 7, 10, 7), spacing=7)
    window.selected_title = label("计划顺序", "panelTitle")
    band.addWidget(window.selected_title)
    window.plan_count = label("0", "countBadge")
    band.addWidget(window.plan_count)
    band.addStretch(1)
    window.problem_buttons = {}
    for level, name, glyph in (("warning", "警告", "warning"), ("error", "需处理", "alert")):
        control = button(f"{name} 0", lambda _checked, level=level: window.filter_problems(level),
                         name=f"{level}Filter", glyph=glyph)
        control.setIcon(icon(glyph, problem_color(level)))
        control.setProperty("problemLevel", level)
        control.setCheckable(True)
        control.setToolTip(f"只看带「{name}」图标的 Mod。")
        window.problem_buttons[level] = control
        band.addWidget(control)
    window.priority_label = label("↑ 顶部优先", "priorityPill")
    window.priority_label.setToolTip("同一个文件可由多个 Mod 提供时，已经确认的覆盖路径按顶部优先。")
    band.addWidget(window.priority_label)
    window.clear_problem_button = button("全部", lambda: window.filter_problems(), name="clearProblemFilter")
    window.clear_problem_button.hide()
    band.addWidget(window.clear_problem_button)
    layout.addWidget(header)
    window.profile_search = create_column_search(window, "profile")
    layout.addWidget(window.profile_search)
    window.profile_list = ModList("profile")
    window.profile_list.itemSelectionChanged.connect(lambda: window.show_selected_detail("profile"))
    window.profile_list.itemClicked.connect(window.toggle_group)
    window.profile_list.dropped.connect(window.handle_drop)
    window.profile_list.relationship_selected.connect(window.reveal_plan_relation)
    window.profile_list.problem_selected.connect(window.inspect_problem)
    window.profile_list.menu_requested.connect(window.open_mod_menu)
    window.profile_list.setToolTip("Ctrl 或 Shift 多选，右键打开菜单。点行尾黄标或红标可查原因。")
    layout.addWidget(ColumnHeader(window.profile_list))
    layout.addWidget(window.profile_list, 1)
    footer, actions = frame("paneFooter", margins=(7, 6, 7, 6), spacing=5)
    remove = button("← 移出", window.remove_selected)
    remove.setToolTip("移出所选项。联锁成员会一起移入暂存篮。")
    actions.addWidget(remove)
    actions.addWidget(button("↑", lambda: window.nudge(-1)))
    actions.addWidget(button("↓", lambda: window.nudge(1)))
    add_group = button("＋ 分组", window.group_selected)
    add_group.setToolTip("把选中的 Mod 组成一个可重复启停的组")
    actions.addWidget(add_group)
    window.locate_button = button("匹配…", window.relink_selected)
    window.locate_button.setToolTip("为计划条目匹配本机安装")
    actions.addWidget(window.locate_button)
    actions.addStretch()
    window.profile_selection = label("", "smallCaps")
    actions.addWidget(window.profile_selection)
    layout.addWidget(footer)
    return panel


def create_column_search(window, side):
    bar = ListSearchBar(side)
    bar.filters_changed.connect(lambda: window.search_filters_changed(side))
    bar.locate_changed.connect(lambda: window.refresh_search_hits(side))
    bar.step.connect(lambda offset: window.locate_next(side, offset))
    return bar


def create_details(window):
    panel, layout = frame("panel", vertical=True, margins=(1, 1, 1, 1))
    panel.setMinimumWidth(205)
    panel.setMaximumWidth(380)
    header, band = frame("paneHeader", margins=(11, 7, 10, 7))
    band.addWidget(label("MOD 详情", "panelTitle"))
    band.addStretch()
    window.detail_generation_button = button("AI 生成", lambda: window.edit_mod_text(generate=True))
    window.detail_generation_button.setToolTip("生成显示别名和中文简介")
    band.addWidget(window.detail_generation_button)
    layout.addWidget(header)
    body = QWidget()
    details = QVBoxLayout(body)
    details.setContentsMargins(11, 11, 11, 10)
    details.setSpacing(7)
    window.detail_art = ModArtwork()
    window.detail_art.clicked.connect(window.preview_cover)
    details.addWidget(window.detail_art)
    window.detail_title = label("选择一个 Mod", "detailTitle")
    window.detail_title.setWordWrap(True)
    details.addWidget(window.detail_title)
    window.detail_original = label("", "muted")
    window.detail_original.setWordWrap(True)
    window.detail_original.hide()
    details.addWidget(window.detail_original)
    card = QFrame(objectName="factsCard")
    facts = QGridLayout(card)
    facts.setContentsMargins(9, 8, 9, 8)
    facts.setHorizontalSpacing(11)
    facts.setVerticalSpacing(3)
    facts.setColumnStretch(1, 1)
    window.detail_facts = {}
    for row, name in enumerate(("来源", "分类", "版本", "Workshop ID")):
        facts.addWidget(label(name, "factLabel"), row, 0)
        value = label("—")
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        facts.addWidget(value, row, 1)
        window.detail_facts[name] = value
    details.addWidget(card)
    window.shop_links = QWidget()
    shop_row = QHBoxLayout(window.shop_links)
    shop_row.setContentsMargins(0, 0, 0, 0)
    shop_row.setSpacing(5)
    window.workshop_button = QToolButton(objectName="quiet", text="打开工坊")
    window.workshop_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    window.workshop_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    window.workshop_button.setToolTip("在 Steam 或浏览器里打开工坊页面。点箭头可复制链接或直接用浏览器。")
    workshop_menu = QMenu(window.workshop_button)
    window.add_workshop_menu_actions(workshop_menu)
    window.workshop_button.setMenu(workshop_menu)
    window.workshop_button.clicked.connect(window.open_workshop)
    shop_row.addWidget(window.workshop_button)
    shop_row.addWidget(button("复制 ID", window.copy_workshop_id))
    shop_row.addStretch(1)
    window.shop_links.hide()
    details.addWidget(window.shop_links)
    tags_line = QHBoxLayout()
    tags_line.addWidget(label("标签", "smallCaps"))
    tags_line.addStretch()
    window.tags_button = button("＋ 标签", window.edit_tags)
    window.tags_button.setEnabled(False)
    tags_line.addWidget(window.tags_button)
    details.addLayout(tags_line)
    window.detail_tags = TagChips()
    details.addWidget(window.detail_tags)
    group_line = QHBoxLayout()
    group_line.addWidget(label("分组", "smallCaps"))
    group_line.addStretch()
    window.detail_group_button = button("＋ 分组", lambda: window.open_detail_group_menu())
    group_line.addWidget(window.detail_group_button)
    details.addLayout(group_line)
    window.detail_groups = TagChips()
    details.addWidget(window.detail_groups)
    details.addWidget(label("颜色标记", "smallCaps"))
    colors = QHBoxLayout()
    colors.setSpacing(1)
    window.detail_colors = {}
    for title, color in (*MARK_COLORS, ("清除标记", "")):
        control = QToolButton(objectName="groupControl")
        control.setIcon(icon("dot" if color else "close", color or None))
        control.setIconSize(QSize(15, 15))
        control.setFixedSize(18, 23)
        control.setCheckable(bool(color))
        control.setToolTip(title)
        control.setAccessibleName(title)
        control.clicked.connect(lambda _checked=False, color=color: window.mark_detail(color))
        window.detail_colors[color] = control
        colors.addWidget(control)
    colors.addStretch()
    details.addLayout(colors)
    description = QHBoxLayout()
    description.setSpacing(4)
    description.addWidget(label("简介", "smallCaps"))
    description.addStretch()
    window.detail_alias_button = button("别名…", lambda: window.edit_mod_text())
    description.addWidget(window.detail_alias_button)
    details.addLayout(description)
    window.detail_description = QPlainTextEdit()
    window.detail_description.setObjectName("description")
    window.detail_description.setReadOnly(True)
    window.detail_description.setPlaceholderText("选择条目后查看简介")
    window.detail_description.setMinimumHeight(72)
    details.addWidget(window.detail_description, 1)
    window.detail_warning = label("", "badge")
    window.detail_warning.setWordWrap(True)
    window.detail_warning.hide()
    details.addWidget(window.detail_warning)
    footer, row = frame("paneFooter", margins=(7, 6, 7, 6), spacing=5)
    window.open_folder_button = button("打开目录", window.open_mod_folder, glyph="folder")
    window.open_folder_button.setEnabled(False)
    row.addWidget(window.open_folder_button)
    window.classification_button = button("分类…", window.edit_classification)
    window.classification_button.setEnabled(False)
    row.addWidget(window.classification_button)
    row.addStretch()
    scroll = QScrollArea()
    scroll.setObjectName("detailScroll")
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(body)
    layout.addWidget(scroll, 1)
    layout.addWidget(footer)
    return panel


class UtilityPane(QFrame):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("panel")
        self.setMinimumWidth(209)
        self.setMaximumWidth(390)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        header, band = frame("paneHeader", margins=(11, 7, 10, 7))
        band.addWidget(label("工作区", "panelTitle"))
        outer.addWidget(header)
        self.tabs = QTabWidget(objectName="utilityTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(True)
        outer.addWidget(self.tabs, 1)

        self.relation_page, relation_layout = self.add_page("关联")
        self.conflict_panel = ConflictPanel(window.recheck_conflicts)
        self.conflict_panel.jump.connect(window.jump_to_problem_mod)
        self.conflict_panel.change_ignored.connect(window.change_ignored_problems)
        self.conflict_panel.ask_ai.connect(window.ask_about_problem)
        self.conflict_panel.add_order_rule.connect(window.edit_conflict_rule)
        self.tabs.addTab(self.conflict_panel, "冲突")
        self.subject = label("", "section")
        self.subject.setWordWrap(True)
        relation_layout.addWidget(self.subject)
        self.relation_hint = label("选择一个 Mod 查看关联。", "muted")
        self.relation_hint.setWordWrap(True)
        relation_layout.addWidget(self.relation_hint)
        self.relation_navigation, navigation = frame("relationNavigation", margins=(7, 5, 7, 5), spacing=4)
        navigation.addWidget(button("← 返回起点", window.return_to_relation_origin))
        navigation.addWidget(button("查看此项关联", window.follow_current_detail))
        self.relation_navigation.hide()
        outer.insertWidget(1, self.relation_navigation)
        self.relation_legend = label("蓝线 · 优先顺序　绿线 · 启用依赖", "smallCaps")
        self.relation_legend.setWordWrap(True)
        self.relation_legend.hide()
        relation_layout.addWidget(self.relation_legend)
        self.relations = self.add_list(relation_layout, "relationList", "关联 Mod")
        self.relations.itemActivated.connect(lambda item: window.reveal_relation(item.data(ROLE)))
        self.relations.itemClicked.connect(lambda item: window.reveal_relation(item.data(ROLE)))
        relation_layout.addWidget(button("编辑排序规则…", window.edit_rules, glyph="link"))

        self.groups_page, group_layout = self.add_page("分组")
        self.group_shelf = GroupShelf(window)
        group_layout.addWidget(self.group_shelf, 1)
        self.legacy_group_button = QPushButton("计划标题与联锁", objectName="quiet", checkable=True)
        group_layout.addWidget(self.legacy_group_button)
        self.groups = self.add_list(group_layout, "groupList", "计划标题与联锁")
        self.groups.setMaximumHeight(115)
        self.groups.hide()
        self.legacy_group_button.toggled.connect(self.groups.setVisible)
        self.groups.itemActivated.connect(lambda item: window.reveal_entry(item.data(ROLE)))
        self.groups.itemClicked.connect(lambda item: window.reveal_entry(item.data(ROLE)))

        self.stage_page, stage_layout = self.add_page("暂存")
        self.stage_hint = label("在可用列表选择 Mod 后点「暂存」。", "muted")
        self.stage_hint.setWordWrap(True)
        stage_layout.addWidget(self.stage_hint)
        self.staging = self.add_list(stage_layout, "stagingList", "暂存篮")
        self.staging.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.staging.itemSelectionChanged.connect(self.update_stage_buttons)
        self.staging.itemClicked.connect(window.click_staged_item)
        stage_actions = QHBoxLayout()
        stage_actions.setSpacing(4)
        self.stage_activate = button("加入计划", window.add_staged, primary=True)
        self.stage_activate.setToolTip("将所选项按暂存篮顺序加入计划末尾")
        self.stage_release = button("移出", window.release_staged)
        self.stage_release.setToolTip("从暂存篮移出，已安装的 Mod 回到可用列表")
        self.stage_unlock = button("解锁", lambda: window.unlock_selected(staged=True))
        for control in (self.stage_activate, self.stage_release, self.stage_unlock):
            stage_actions.addWidget(control)
        stage_layout.addLayout(stage_actions)

        self.history_page, history_layout = self.add_page("历史")
        self.snapshots = self.add_list(history_layout, "snapshotList", "历史快照")
        self.snapshots.itemActivated.connect(
            lambda item: window.show_history(snapshot_id=item.data(ROLE))
        )
        history_layout.addWidget(button("对比快照…", lambda: window.show_history(
            snapshot_id=self.snapshots.currentItem().data(ROLE) if self.snapshots.currentItem() else None
        )))

    def add_page(self, name):
        page, layout = frame("dockBody", vertical=True, margins=(9, 9, 9, 8), spacing=7)
        self.tabs.addTab(page, name)
        return page, layout

    @staticmethod
    def add_list(layout, name, accessible_name):
        rows = QListWidget(objectName=name)
        rows.setAccessibleName(accessible_name)
        rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        layout.addWidget(rows, 1)
        return rows

    def set_relations(self, subject, relations, current="", returning=False, *, show_legend=False):
        scroll = self.relations.verticalScrollBar()
        scroll_value = scroll.value() if self.subject.text() == subject else 0
        self.subject.setText(subject)
        self.subject.setVisible(bool(subject))
        self.relations.clear()
        if returning:
            hint = f"当前查看 · {current}"
        elif not subject:
            hint = "选择一个 Mod 查看关联。"
        else:
            hint = "点击关联项，在列表中定位。" if relations else "没有已确认的配套关系或用户规则。"
        self.relation_hint.setText(hint)
        self.relation_navigation.setVisible(returning)
        self.relation_legend.setVisible(show_legend)
        self.tabs.setTabText(0, f"关联 {len(relations)}" if relations else "关联")
        for relation in relations:
            item = QListWidgetItem(f"{relation.name}\n{relation.kind}\n{relation.location}")
            item.setToolTip(f"{relation.name}\n{relation.kind} · {relation.location}\n{relation.evidence}")
            item.setData(ROLE, relation)
            if relation.target or relation.choices:
                item.setIcon(icon("link", current_theme().accent))
            self.relations.addItem(item)
        scroll.setValue(scroll_value)

    def set_groups(self, profile_id, saved, groups, locks):
        self.group_shelf.set_groups(profile_id, saved)
        self.groups.clear()
        count = len(saved)
        self.tabs.setTabText(self.tabs.indexOf(self.groups_page), f"分组 {count}" if count else "分组")
        legacy = len(groups) + len(locks)
        self.legacy_group_button.setText(f"计划标题与联锁 · {legacy}")
        self.legacy_group_button.setVisible(bool(legacy))
        self.groups.setVisible(bool(legacy) and self.legacy_group_button.isChecked())
        for index in sorted(groups.keys() | locks.keys()):
            if index in groups:
                group, end = groups[index]
                item = QListWidgetItem(f"{group.title}\n{end - index} 项 · 从 #{index + 1} 开始")
                item.setData(ROLE, index)
                self.groups.addItem(item)
            if lock := locks.get(index):
                item = QListWidgetItem(f"{lock.title}\n联锁 {len(lock.members)} 项 · 从 #{index + 1} 开始")
                item.setIcon(icon("lock", current_theme().accent))
                item.setData(ROLE, index)
                self.groups.addItem(item)

    def set_staged(self, rows, locks):
        selected = {member for item in self.staging.selectedItems() for member in item.data(ROLE)}
        scroll = self.staging.verticalScrollBar()
        scroll_value = scroll.value()
        name = "暂存"
        self.tabs.setTabText(self.tabs.indexOf(self.stage_page), f"{name} {len(rows)}" if rows else name)
        self.stage_hint.setText(
            "多选后成批加入计划末尾。" if rows else "在可用列表选择 Mod 后点「暂存」。"
        )
        with QSignalBlocker(self.staging):
            self.staging.clear()
            self.fill_staged(rows, locks, selected)
            scroll.setValue(scroll_value)
        self.update_stage_buttons()

    def fill_staged(self, rows, locks, selected):
        index = 0
        while index < len(rows):
            lock = locks.get(index)
            if lock:
                chunk = rows[index:index + len(lock.members)]
                missing = sum(row.mod is None for row in chunk)
                subtitle = f"联锁 · {len(chunk)} 项" + (f" · {missing} 待匹配" if missing else "")
                item = QListWidgetItem(f"{lock.title}\n{subtitle}")
                item.setIcon(icon("lock", current_theme().accent))
                item.setData(ROLE, lock.members)
            else:
                chunk = rows[index:index + 1]
                row = chunk[0]
                title = row.mod.name if row.mod else row.entry.name
                item = QListWidgetItem(f"{title}\n{row.entry.source.label if row.mod else '待匹配'}")
                item.setData(ROLE, (row.entry.id,))
            item.setToolTip("\n".join(row.entry.name + (f" · {row.problem}" if row.problem else "") for row in chunk))
            self.staging.addItem(item)
            item.setSelected(bool(selected.intersection(item.data(ROLE))))
            index += len(chunk)

    def update_stage_buttons(self):
        selected = self.staging.selectedItems()
        locked = any(len(item.data(ROLE)) > 1 for item in selected)
        self.stage_activate.setEnabled(bool(selected))
        self.stage_release.setEnabled(bool(selected) and not locked)
        self.stage_unlock.setEnabled(locked)

    def set_history(self, snapshots):
        self.snapshots.clear()
        self.tabs.setTabText(
            self.tabs.indexOf(self.history_page), f"历史 {len(snapshots)}" if snapshots else "历史"
        )
        for snapshot in snapshots:
            time = snapshot.taken.astimezone().strftime("%m-%d %H:%M")
            item = QListWidgetItem(f"{snapshot.name or snapshot.reason}\n{time} · {len(snapshot.profile.entries)} 个 Mod")
            item.setData(ROLE, snapshot.id)
            self.snapshots.addItem(item)
