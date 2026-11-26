from __future__ import annotations

import json
from collections import Counter

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
)

from darksort.history import ProfileHistory, compare_entries
from darksort.models import OrderRule, Profile
from darksort.profiles import MAX_RULES, resolve_entries
from darksort.rule_targets import dynamic_target
from darksort.search import SearchRecord
from darksort.sorting import OrderPreview
from darksort.ui.mod_list import MIME, ROLE, GroupMemberTree, ModList
from darksort.ui.search import GlobalSearchDialog
from darksort.ui.theme import current_theme
from darksort.ui.workspace_dock import data_table, decision_rows, fill_table
from darksort.workshop import workshop_page


def text_view(text: str = "") -> QPlainTextEdit:
    view = QPlainTextEdit(text)
    view.setReadOnly(True)
    return view


def confirmation_dialog(parent, title: str, text: str, action: str = "") -> bool:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(720, 470)
    layout = QVBoxLayout(dialog)
    layout.addWidget(text_view(text))
    buttons = QDialogButtonBox()
    if action:
        accept = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        accept.setObjectName("primary")
    cancel = buttons.addButton("取消" if action else "关闭", QDialogButtonBox.ButtonRole.RejectRole)
    cancel.setDefault(True)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    dialog.deleteLater()
    return accepted


def transfer_matches(mods, sections, scanned: bool, *, show_all=False) -> str:
    summaries, lists = [], []
    for title, entries in sections:
        if not entries:
            continue
        rows = resolve_entries(entries, mods)
        missing = sum(row.mod is None for row in rows)
        summaries.append(f"{title} · {len(rows)} 项 · {len(rows) - missing} 已匹配 · {missing} 待匹配")
        lines = []
        for index, row in enumerate(rows, 1):
            if row.mod and not show_all:
                continue
            reason = "已匹配" if row.mod else row.problem if scanned else "扫描后匹配"
            lines.append(f"#{index:02d}  {row.entry.name}  ·  {reason}")
            if not row.mod and (link := workshop_page(row.entry.workshop_id)):
                lines.append(f"        {link}")
        if lines:
            heading = title if show_all else f"{title} · 待匹配项"
            lists.append("\n".join([heading, *lines]))
    if not summaries:
        return "共 0 项 Mod。"
    if not scanned:
        summaries.append("当前尚未扫描 Mod。载入后可扫描并重新匹配。")
    return "\n\n".join(["\n".join(summaries), *lists])


class SortPreviewDialog(QDialog):
    def __init__(self, preview: OrderPreview, *, rules_only=False, title=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title or ("规则排序预览" if rules_only else "智能排序预览"))
        self.resize(980, 530)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        count = sum(row.before != row.after for row in preview.decisions)
        heading = QLabel(f"{count} 个条目建议移动" if not preview.blockers else "排序规则需要处理")
        heading.setObjectName("section")
        header.addWidget(heading, 1)
        moved = QCheckBox("只看变动")
        moved.setEnabled(bool(count))
        header.addWidget(moved)
        layout.addLayout(header)
        table = data_table(("原位 → 新位", "MOD", "分类", "排序依据"), (96, 270, 106, 0))
        fill_table(table, decision_rows(preview))
        table.setVisible(not preview.blockers)
        layout.addWidget(table, 1)
        moved.toggled.connect(
            lambda checked: fill_table(table, decision_rows(preview, changed_only=checked))
        )
        messages = (*preview.blockers, *preview.notices)
        if messages:
            notices = text_view("\n".join(messages))
            if not preview.blockers:
                notices.setMaximumHeight(94)
            layout.addWidget(notices)
        buttons = QDialogButtonBox()
        if count and not preview.blockers:
            accept = buttons.addButton("采用建议顺序", QDialogButtonBox.ButtonRole.AcceptRole)
            accept.setObjectName("primary")
        cancel = buttons.addButton("取消" if count else "关闭", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class HistoryDialog(QDialog):
    def __init__(self, history: ProfileHistory, profile: Profile, parent=None, *, snapshot_id=None):
        super().__init__(parent)
        self.history = history
        self.profile = profile
        self.restored = None
        self.setWindowTitle(f"方案历史 · {profile.name}")
        self.resize(1070, 570)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)
        heading = QLabel("快照对比")
        heading.setObjectName("section")
        layout.addWidget(heading)
        splitter = QSplitter()
        self.snapshots = QListWidget(objectName="snapshotList")
        self.snapshots.setAccessibleName("选择历史快照")
        self.snapshots.setMinimumWidth(175)
        self.snapshots.currentItemChanged.connect(self.show_snapshot)
        splitter.addWidget(self.snapshots)

        detail = QFrame(objectName="panel")
        compare = QVBoxLayout(detail)
        compare.setContentsMargins(10, 8, 10, 8)
        compare.setSpacing(8)
        controls = QHBoxLayout()
        self.diff_summary = QLabel()
        self.diff_summary.setObjectName("muted")
        controls.addWidget(self.diff_summary, 1)
        self.hide_unchanged = QCheckBox("收起一致和顺延")
        self.hide_unchanged.setChecked(True)
        self.hide_unchanged.toggled.connect(self.render_diff)
        controls.addWidget(self.hide_unchanged)
        compare.addLayout(controls)
        self.diff = data_table(("序号", "当前方案", "变化", "序号", "所选快照"), (47, 0, 87, 47, 0))
        self.diff.setAccessibleName("当前方案与所选快照差异")
        self.diff.verticalHeader().setDefaultSectionSize(28)
        compare.addWidget(self.diff, 1)
        self.diff_notes = QLabel()
        self.diff_notes.setObjectName("muted")
        self.diff_notes.setWordWrap(True)
        compare.addWidget(self.diff_notes)
        splitter.addWidget(detail)
        splitter.setSizes([235, 780])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.errors = QLabel()
        self.errors.setObjectName("badge")
        layout.addWidget(self.errors)
        buttons = QDialogButtonBox()
        create = buttons.addButton("创建命名快照", QDialogButtonBox.ButtonRole.ActionRole)
        create.clicked.connect(self.capture)
        self.restore_button = buttons.addButton("载入为方案草稿", QDialogButtonBox.ButtonRole.ActionRole)
        self.restore_button.setObjectName("primary")
        self.restore_button.clicked.connect(self.restore)
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.changes = []
        self.reload(snapshot_id)

    def reload(self, snapshot_id=None):
        self.snapshots.clear()
        snapshots, errors = self.history.list(self.profile.id)
        selected = 0
        for snapshot in snapshots:
            local_time = snapshot.taken.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            name = snapshot.name or snapshot.reason
            item = QListWidgetItem(f"{name}\n{local_time} · {len(snapshot.profile.entries)} 个 Mod")
            item.setData(ROLE, snapshot)
            self.snapshots.addItem(item)
            if snapshot.id == snapshot_id:
                selected = self.snapshots.count() - 1
        self.restore_button.setEnabled(False)
        if snapshots:
            self.snapshots.setCurrentRow(selected)
        else:
            self.show_snapshot(None, None)
        self.errors.setText(f"{len(errors)} 个快照无法读取")
        self.errors.setToolTip("\n".join(errors))
        self.errors.setVisible(bool(errors))

    def show_snapshot(self, current, _previous):
        self.restore_button.setEnabled(current is not None)
        self.hide_unchanged.setEnabled(current is not None)
        if current is None:
            self.changes = []
            self.diff_summary.setText("该方案还没有快照。")
            self.diff_notes.clear()
            self.render_diff()
            return
        snapshot = current.data(ROLE)
        self.changes = compare_entries(self.profile, snapshot.profile)
        counts = Counter(row.status for row in self.changes)
        self.diff_summary.setText("   ·   ".join((
            f"加入 {counts['加入']}", f"移除 {counts['移除']}",
            f"移位 {counts['移位'] + counts['移位 + 更新']}", f"顺延 {counts['顺延']}",
            f"更新 {counts['更新'] + counts['移位 + 更新']}",
        )))
        notes = []
        if self.profile.groups != snapshot.profile.groups:
            notes.append("分组有变化")
        if self.profile.rules != snapshot.profile.rules:
            notes.append("排序规则有变化")
        if self.profile.locks != snapshot.profile.locks:
            notes.append(f"联锁 {len(self.profile.locks)} → {len(snapshot.profile.locks)} 组有变化")
        notes.append("按相同 Mod 对齐。顺延条目的相对顺序一致。")
        self.diff_notes.setText(" · ".join(notes))
        self.render_diff()

    def render_diff(self, _checked=False):
        changes = [
            row for row in self.changes
            if not self.hide_unchanged.isChecked() or row.status not in {"一致", "顺延"}
        ]
        colors = {
            "加入": "#79ccaa", "移除": "#eda29b", "移位": "#edbc84",
            "移位 + 更新": "#edbc84", "更新": "#b7ade9",
        }
        theme = current_theme()
        fill_table(self.diff, (
            (row.before or "—", row.old_entry.name if row.old_entry else "—", row.status,
             row.after or "—", row.new_entry.name if row.new_entry else "—")
            for row in changes
        ))
        for index, row in enumerate(changes):
            marker = self.diff.item(index, 2)
            marker.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if color := colors.get(row.status):
                hue = QColor(color) if theme.dark else QColor(color).darker(166)
                marker.setForeground(hue)
                marker.setBackground(theme.tint(hue.name(), 0.15))

    def capture(self):
        name, ok = QInputDialog.getText(self, "创建快照", "快照名称")
        if not ok or not name.strip():
            return
        try:
            snapshot = self.history.capture(self.profile, "手动创建", name=name[:120])
            self.reload(snapshot.id)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "快照未创建", str(exc))

    def restore(self):
        item = self.snapshots.currentItem()
        if item is None:
            return
        self.restored = item.data(ROLE).profile
        self.accept()


class RuleDialog(QDialog):
    save_requested = Signal()

    def __init__(
        self, profile: Profile, targets: dict[str, SearchRecord], parent=None,
        *, first="", second="", prerequisite=False,
    ):
        super().__init__(parent, Qt.WindowType.Tool)
        self.profile_id = profile.id
        self.profile_name = profile.name
        self.original_rules = tuple(profile.rules)
        self.selected: list[tuple[str, ...]] = [(), ()]
        self.target_buttons = []
        self.drop_highlight = None
        self.creating = bool(first)
        layout = QVBoxLayout(self)
        subtitle = QLabel("「在上方」可以选择 Mod、分组、标签或标记颜色。「依赖」可以选择两个具体 Mod。")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.drag_hint = QLabel(objectName="muted", wordWrap=True)
        layout.addWidget(self.drag_hint)
        self.list = QListWidget(self)
        if self.creating:
            self.resize(690, 260)
            self.list.hide()
        else:
            self.resize(750, 510)
            layout.addWidget(self.list)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for position, caption in enumerate(("目标 A", "目标 B")):
            button = QPushButton()
            button.setAccessibleName(caption)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, position=position: self.pick_target(position))
            self.target_buttons.append(button)
        self.relation = QComboBox()
        self.relation.addItem("在上方", "above")
        self.relation.addItem("依赖", "requires")
        self.swap = QPushButton("交换 A/B")
        self.swap.setAutoDefault(False)
        self.swap.clicked.connect(self.swap_targets)
        relation_controls = QHBoxLayout()
        relation_controls.addWidget(self.relation, 1)
        relation_controls.addWidget(self.swap)
        self.note = QLineEdit()
        self.note.setPlaceholderText("可选，例如补丁放在本体上方")
        self.note.setMaxLength(500)
        form.addRow("目标 A", self.target_buttons[0])
        form.addRow("关系", relation_controls)
        form.addRow("目标 B", self.target_buttons[1])
        form.addRow("说明", self.note)
        layout.addLayout(form)
        if not self.creating:
            actions = QHBoxLayout()
            self.add = QPushButton("添加规则")
            self.add.clicked.connect(self.add_rule)
            actions.addWidget(self.add)
            remove = QPushButton("移除所选")
            remove.clicked.connect(self.remove_rule)
            actions.addWidget(remove)
            actions.addStretch()
            layout.addLayout(actions)
        buttons = QDialogButtonBox()
        if self.creating:
            self.add = buttons.addButton("添加规则", QDialogButtonBox.ButtonRole.AcceptRole)
        else:
            buttons.addButton("保留规则", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save_requested.emit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self, self.list.viewport(), *self.target_buttons):
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)
        self.update_candidates(targets)
        for rule in self.original_rules:
            self.add_row(rule)
        self.prefill(first, second, prerequisite)

    def update_candidates(self, targets):
        self.drag_enabled = targets is not None
        self.drag_hint.setText(
            "将 Mod 拖到目标 A 或 B。拖到规则列表会填入空着的一侧。"
            if self.drag_enabled else "切回此窗口对应的方案后，可以继续拖入 Mod。"
        )
        if targets is None:
            self.highlight_drop(None)
            return
        self.targets = targets
        counts = Counter(record.title for record in targets.values())
        self.names = {
            key: f"{record.title} · {key}" if counts[record.title] > 1 else record.title
            for key, record in targets.items()
        }
        for position, selected in enumerate(self.selected):
            self.set_targets(position, selected)

    def prefill(self, first, second, prerequisite):
        title = "方案排序规则"
        if self.creating:
            title = "设置前置 Mod" if prerequisite else "添加排序规则"
        self.setWindowTitle(f"{title} · {self.profile_name}")
        self.relation.setCurrentIndex(self.relation.findData("requires" if prerequisite else "above"))
        self.relation.setEnabled(not prerequisite or not self.creating)
        self.set_targets(0, (first,))
        self.set_targets(1, (second,))
        if not second:
            self.target_buttons[1 if first else 0].setFocus()

    def eventFilter(self, widget, event):
        kind = event.type()
        if kind == QEvent.Type.DragLeave:
            self.highlight_drop(None)
        if kind not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            return super().eventFilter(widget, event)
        point = event.position().toPoint()
        if widget is not self:
            point = widget.mapTo(self, point)
        position = next(
            (i for i, button in enumerate(self.target_buttons) if button.geometry().contains(point)), None,
        )
        if position is None:
            position = next((i for i, selected in enumerate(self.selected) if not selected), None)
        references = self.dropped_references(event, position) if position is not None else ()
        self.highlight_drop(position if references and kind != QEvent.Type.Drop else None)
        if references:
            if kind == QEvent.Type.Drop:
                self.set_targets(position, references)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        return True

    def dropped_references(self, event, position):
        source = event.source()
        mime = event.mimeData()
        if (
            not self.drag_enabled or not isinstance(source, (ModList, GroupMemberTree))
            or source.window() is not self.parentWidget() or not mime.hasFormat(MIME)
            or not event.possibleActions() & Qt.DropAction.CopyAction
        ):
            return ()
        try:
            payload = json.loads(bytes(mime.data(MIME)))
        except ValueError:
            return ()
        references = payload.get("references") if isinstance(payload, dict) else None
        if not isinstance(references, list):
            return ()
        other = set(self.selected[1 - position])
        return tuple(dict.fromkeys(
            ref for ref in references
            if isinstance(ref, str) and ref in self.targets and ref not in other and not dynamic_target(ref)
        ))

    def highlight_drop(self, position):
        if position == self.drop_highlight:
            return
        for index in (self.drop_highlight, position):
            if index is not None:
                button = self.target_buttons[index]
                button.setProperty("ruleDropTarget", index == position)
                button.style().unpolish(button)
                button.style().polish(button)
                button.update()
        self.drop_highlight = position

    def set_targets(self, position, references):
        button = self.target_buttons[position]
        selected = tuple(reference for reference in references if reference in self.targets)
        self.selected[position] = selected
        if not selected:
            button.setText("拖入 Mod 或搜索…")
            button.setToolTip("")
        elif len(selected) == 1:
            reference = selected[0]
            record = self.targets[reference]
            button.setText(self.names[reference])
            button.setToolTip(f"{record.title}\n{record.location}")
        else:
            button.setText(f"已选 {len(selected)} 项 · {self.names[selected[0]]} 等")
            button.setToolTip("\n".join(self.names[reference] for reference in selected))
        self.swap.setEnabled(all(self.selected))
        pairs = len(self.selected[0]) * len(self.selected[1])
        self.add.setText(f"添加规则（{pairs} 组）" if pairs > 1 else "添加规则")

    def pick_target(self, position):
        other = set(self.selected[1 - position])
        mods_only = self.relation.currentData() == "requires"
        records = [
            record for reference, record in self.targets.items()
            if reference not in other and (not mods_only or not dynamic_target(reference))
        ]
        available = {record.target.identity: record.target for record in records}
        dialog = GlobalSearchDialog(
            records, self, rule_target=True,
            selected=tuple(available[reference] for reference in self.selected[position] if reference in available),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen_targets:
            self.set_targets(position, (target.identity for target in dialog.chosen_targets))

    def swap_targets(self):
        first, second = self.selected
        self.set_targets(0, second)
        self.set_targets(1, first)

    def add_row(self, rule: OrderRule):
        first = self.names.get(rule.first, "已移除的分组" if rule.first.startswith("group:") else rule.first)
        second = self.names.get(rule.second, "已移除的分组" if rule.second.startswith("group:") else rule.second)
        relation = "在上方" if rule.kind == "above" else "依赖"
        item = QListWidgetItem(f"{first}  ›  {relation}  ›  {second}")
        item.setData(ROLE, rule)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
        item.setToolTip(rule.note)
        if rule.note:
            item.setText(f"{item.text()}  ·  {rule.note}")
        self.list.addItem(item)

    def add_rule(self) -> bool:
        first, second = self.selected
        left, right = set(first), set(second)
        if not left or not right or left & right:
            QMessageBox.information(self, "请选择目标", "请为规则选择两个不同的目标。")
            return False
        kind = self.relation.currentData()
        if kind == "requires" and any(dynamic_target(reference) for reference in left | right):
            QMessageBox.information(self, "请选择具体 Mod", "分组、标签和颜色用于「在上方」规则。")
            return False
        existing = {}
        for index in range(self.list.count()):
            item = self.list.item(index)
            rule = item.data(ROLE)
            if rule.kind == kind and rule.first in left and rule.second in right:
                existing[(rule.first, rule.second)] = item
        new_count = len(first) * len(second) - len(existing)
        if not new_count:
            self.list.setCurrentItem(next(iter(existing.values())))
            if self.creating:
                QMessageBox.information(self, "规则已存在", "可在「编辑用户规则」里查看或启用所选规则。")
            return False
        if self.list.count() + new_count > MAX_RULES:
            QMessageBox.information(self, "排序规则过多", f"每份方案最多可保存 {MAX_RULES} 条排序规则。")
            return False
        note = self.note.text().strip()
        for source in first:
            for target in second:
                if (source, target) not in existing:
                    self.add_row(OrderRule(source, target, kind, note))
        self.note.clear()
        return True

    def remove_rule(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def result_rules(self) -> list[OrderRule]:
        rules = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            rule = item.data(ROLE)
            rules.append(
                OrderRule(
                    rule.first, rule.second, rule.kind, rule.note,
                    item.checkState() == Qt.CheckState.Checked,
                )
            )
        return rules
