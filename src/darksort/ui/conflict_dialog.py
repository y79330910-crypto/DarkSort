from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
)

from darksort.conflicts import ConflictReport, analyze_conflicts
from darksort.ui.mod_list import ROLE
from darksort.ui.theme import current_theme, icon


class ConflictWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)

    def __init__(self, entries, parent):
        super().__init__(parent)
        self.entries = list(entries)

    def run(self):
        try:
            result = analyze_conflicts(
                self.entries, progress=self.progress.emit,
                cancelled=self.isInterruptionRequested,
            )
        except (OSError, ValueError) as exc:
            result = ConflictReport(issues=(f"分析失败 · {exc}",), failed=True)
        self.completed.emit(result)


def plain_label(text="", name=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setObjectName(name)
    return label


class ConflictPanel(QFrame):
    jump = Signal(str)
    change_ignored = Signal(object, bool)
    ask_ai = Signal(object)
    add_order_rule = Signal(str, str)

    def __init__(self, recheck, parent=None):
        super().__init__(parent)
        self.setObjectName("dockBody")
        self.report = None
        self.problems = ()
        self.entries = {}
        self.entry = None
        self.level = ""
        self.ignored = frozenset()
        self.show_ignored = False
        self.visible_problems = []
        self.position = 0
        self.current_problem = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 9, 9, 8)
        layout.setSpacing(7)
        self.status = plain_label("等待检查文件。", "muted")
        layout.addWidget(self.status)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.overview = QPushButton("返回概览")
        self.overview.setObjectName("quiet")
        self.overview.clicked.connect(lambda: self.inspect())
        self.overview.hide()
        self.review = QPushButton("待核实")
        self.review.setObjectName("quiet")
        self.review.clicked.connect(lambda: self.inspect(level="review"))
        self.ignored_button = QPushButton("已忽略")
        self.ignored_button.setObjectName("quiet")
        self.ignored_button.clicked.connect(lambda: self.inspect(ignored=True))
        more = QPushButton("⋯")
        more.setObjectName("quiet")
        more.setFixedWidth(38)
        more.setAccessibleName("更多冲突操作")
        menu = QMenu(more)
        self.raw = menu.addAction("完整文件明细…", self.show_report)
        self.recheck = menu.addAction("重新检查文件", recheck)
        self.raw.setEnabled(False)
        more.setMenu(menu)
        for control in (self.overview, self.review, self.ignored_button):
            toolbar.addWidget(control)
        toolbar.addStretch()
        toolbar.addWidget(more)
        layout.addLayout(toolbar)

        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("detailScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QFrame(objectName="problemCard")
        detail = QVBoxLayout(body)
        detail.setContentsMargins(9, 9, 9, 7)
        detail.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.badge = plain_label("", "problemBadge")
        top.addWidget(self.badge)
        top.addStretch()
        self.previous = QPushButton(icon("previous"), "")
        self.next = QPushButton(icon("next"), "")
        self.page = plain_label("", "smallCaps")
        for control, name, direction in (
            (self.previous, "上一条提醒", -1), (self.next, "下一条提醒", 1),
        ):
            control.setObjectName("quiet")
            control.setFixedWidth(25)
            control.setAccessibleName(name)
            control.setToolTip(name)
            control.clicked.connect(lambda _checked, direction=direction: self.step(direction))
        top.addWidget(self.previous)
        top.addWidget(self.page)
        top.addWidget(self.next)
        detail.addLayout(top)
        self.title = plain_label("点 Mod 行尾的提示", "section")
        self.message = plain_label()
        self.targets = QListWidget()
        self.targets.setObjectName("problemTargets")
        self.targets.setAccessibleName("点击定位相关 Mod")
        self.targets.setUniformItemSizes(True)
        self.targets.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.targets.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.targets.itemClicked.connect(lambda item: self.jump.emit(item.data(ROLE)))
        self.targets.itemActivated.connect(lambda item: self.jump.emit(item.data(ROLE)))
        self.advice = plain_label("", "muted")
        self.rule = QPushButton("添加排序规则…", objectName="quiet")
        self.rule.clicked.connect(self.create_order_rule)
        self.files = QPushButton("▸ 文件与判定依据")
        self.files.setObjectName("quiet")
        self.files.setCheckable(True)
        self.technical = QPlainTextEdit()
        self.technical.setReadOnly(True)
        self.technical.setFixedHeight(83)
        self.files.toggled.connect(self.toggle_details)
        detail.addWidget(self.title)
        detail.addWidget(self.message)
        detail.addWidget(self.targets)
        detail.addWidget(self.advice)
        detail.addWidget(self.rule, 0, Qt.AlignmentFlag.AlignLeft)
        detail.addWidget(self.files, 0, Qt.AlignmentFlag.AlignLeft)
        detail.addWidget(self.technical)
        detail.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.ignore = QPushButton("忽略…")
        self.ignore.setObjectName("quiet")
        self.ignore.setToolTip("收起提醒，之后可在「已忽略」恢复。")
        self.ignore_menu = QMenu(self.ignore)
        self.ignore_menu.aboutToShow.connect(self.fill_ignore_menu)
        self.ignore.setMenu(self.ignore_menu)
        self.ask = QPushButton("问 AI")
        self.ask.setObjectName("quiet")
        self.ask.clicked.connect(self.ask_current)
        actions.addWidget(self.ignore)
        actions.addStretch()
        actions.addWidget(self.ask)
        layout.addLayout(actions)
        self.render()

    def set_state(self, problems, entries, report, status, ignored, *, checking=False):
        self.report = report
        self.status.setText(status)
        self.raw.setEnabled(report is not None)
        self.recheck.setEnabled(bool(entries) and not checking)
        if self.problems == problems and self.entries == entries and self.ignored == ignored:
            return
        self.problems = problems
        self.entries = entries
        self.ignored = ignored
        if self.entry and self.entry not in self.entries:
            self.entry, self.level = None, ""
        self.render()

    def inspect(self, *, entry=None, level="", ignored=False):
        self.show_ignored = ignored
        self.entry, self.level = entry, level
        self.render(reset=True)

    def render(self, *, reset=False):
        selected = self.current_problem.key if self.current_problem and not reset else None
        self.visible_problems = [
            problem for problem in self.problems
            if (problem.key in self.ignored) == self.show_ignored
            and (not self.entry or any(identity == self.entry for identity, _ in problem.targets))
            and (problem.level == self.level if self.level else (
                problem.level != "review" or self.show_ignored
            ))
        ]
        fallback = 0 if reset else min(self.position, max(0, len(self.visible_problems) - 1))
        self.position = next((i for i, problem in enumerate(self.visible_problems) if problem.key == selected), fallback)
        review = sum(p.level == "review" and p.key not in self.ignored for p in self.problems)
        ignored_count = sum(p.key in self.ignored for p in self.problems)
        scoped = bool(self.entry or self.level or self.show_ignored)
        self.overview.setVisible(scoped)
        self.review.setVisible(review > 0 and not scoped)
        self.review.setText(f"待核实 {review}")
        self.ignored_button.setVisible(bool(self.ignored) and not self.show_ignored)
        self.ignored_button.setText(f"已忽略 {ignored_count}" if ignored_count else "已忽略")
        self.step(0)

    def step(self, delta):
        total = len(self.visible_problems)
        self.position = max(0, min(self.position + delta, total - 1))
        self.previous.setVisible(total > 1)
        self.next.setVisible(total > 1)
        self.page.setVisible(total > 1)
        self.page.setText(f"{self.position + 1}/{total}")
        self.previous.setEnabled(self.position > 0)
        self.next.setEnabled(self.position + 1 < total)
        self.show_problem(self.visible_problems[self.position] if total else None)

    def show_problem(self, problem):
        self.current_problem = problem
        self.badge.setVisible(bool(problem))
        if problem:
            name = {"warning": "⚠ 警告", "error": "！需处理", "review": "待核实"}[problem.level]
            self.badge.setText(f"已忽略 · {name}" if self.show_ignored else name)
            self.badge.setProperty("level", problem.level)
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)
            self.title.setText(problem.title)
        elif self.show_ignored:
            self.title.setText("没有仍在出现的已忽略提醒")
        elif self.entry or self.level:
            self.title.setText("这里没有要显示的提醒")
        else:
            self.title.setText("点 Mod 行尾的黄标或红标")
        self.message.setText(problem.message if problem else "")
        self.message.setVisible(bool(problem))
        self.targets.clear()
        if problem:
            for identity, role in problem.targets:
                if identity in self.entries:
                    number, name = self.entries[identity]
                    text = f"{role}  #{number:02d} {name}"
                    item = QListWidgetItem(icon("next"), text)
                    item.setData(ROLE, identity)
                    item.setToolTip(f"点击定位 · {name}")
                    if role == "生效":
                        item.setForeground(QColor(current_theme().accent))
                    self.targets.addItem(item)
        self.targets.setVisible(bool(self.targets.count()))
        visible_rows = min(3, self.targets.count())
        self.targets.setFixedHeight(2 + sum(self.targets.sizeHintForRow(i) for i in range(visible_rows)))
        self.advice.setText(problem.advice if problem else "")
        self.advice.setVisible(bool(problem))
        self.rule.setVisible(bool(problem and problem.order_pair))
        self.files.setChecked(False)
        self.files.setVisible(bool(problem and problem.details))
        self.technical.setPlainText(problem.details if problem else "")
        self.toggle_details(False)
        self.ask.setVisible(bool(problem))
        self.ignore.setText("恢复…" if self.show_ignored else "忽略…")
        self.ignore.setEnabled(bool(self.ignored) if self.show_ignored else bool(problem or any(
            p.level != "review" and p.key not in self.ignored for p in self.problems
        )))

    def toggle_details(self, showing):
        self.files.setText("▾ 文件与判定依据" if showing else "▸ 文件与判定依据")
        self.technical.setVisible(showing)

    def fill_ignore_menu(self):
        self.ignore_menu.clear()
        current = self.current_problem
        if self.show_ignored:
            if current:
                self.ignore_menu.addAction("恢复这条", lambda: self.change_ignored.emit((current.key,), False))
            self.ignore_menu.addAction(
                "恢复本方案全部提醒", lambda: self.change_ignored.emit(tuple(self.ignored), False),
            )
            return
        if current:
            self.ignore_menu.addAction("忽略这条", lambda: self.change_ignored.emit((current.key,), True))
        for level, label in (("warning", "黄色警告"), ("error", "红色提醒")):
            keys = tuple(p.key for p in self.problems if p.level == level and p.key not in self.ignored)
            if keys:
                self.ignore_menu.addAction(
                    f"忽略当前所有{label}（{len(keys)}）",
                    lambda _checked=False, keys=keys: self.change_ignored.emit(keys, True),
                )

    def ask_current(self):
        if self.current_problem:
            self.ask_ai.emit(self.current_problem)

    def create_order_rule(self):
        if self.current_problem and self.current_problem.order_pair:
            self.add_order_rule.emit(*self.current_problem.order_pair)

    def show_report(self):
        if self.report is not None:
            ConflictDialog(self.report, self).exec()


class ConflictDialog(QDialog):
    def __init__(self, report, parent=None):
        super().__init__(parent)
        self.result = report
        self.setWindowTitle("文件检查明细")
        self.resize(950, 575)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选路径、Mod 名称或诊断")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.render)
        layout.addWidget(self.search)
        splitter = QSplitter()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self.show_details)
        splitter.addWidget(self.list)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        splitter.addWidget(self.detail)
        splitter.setSizes([485, 435])
        layout.addWidget(splitter)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.render()

    def render(self, *_):
        query = self.search.text().strip().casefold()
        self.list.clear()
        for conflict in self.result.conflicts:
            context = " ".join(provider.mod.name for provider in conflict.providers)
            if query and query not in f"{conflict.path} {conflict.status} {context}".casefold():
                continue
            item = QListWidgetItem(f"{conflict.path}\n{conflict.status} · {len(conflict.providers)} 个提供者")
            item.setData(ROLE, conflict)
            self.list.addItem(item)
        if self.result.issues:
            self.list.addItem(f"分析提示 · {len(self.result.issues)} 条")
        self.list.setCurrentRow(0)
        if not self.list.count():
            self.detail.setPlainText("没有符合条件的文件重叠。")

    def show_details(self, current, _previous):
        if current is None:
            return
        conflict = current.data(ROLE)
        if conflict is None:
            self.detail.setPlainText("\n".join(self.result.issues))
            return
        lines = [conflict.path, conflict.status, ""]
        for provider in conflict.providers:
            manifest = {True: "已列入清单", False: "未列入清单", None: "清单未知"}[provider.listed]
            availability = "文件缺失或不可读" if provider.size is None else f"{provider.size:,} 字节"
            outcome = ""
            if provider == conflict.winner:
                outcome = " · 预计生效"
            elif conflict.winner is not None and provider.listed:
                outcome = " · 预计被覆盖"
            lines.extend([
                f"{provider.rank:02d}  {provider.mod.name}{outcome}",
                f"{manifest} · {availability}", str(provider.path), "",
            ])
        self.detail.setPlainText("\n".join(lines))

