from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from darksort.adapters.saves import (
    SaveBackups,
    SaveState,
    commit,
    discover_slots,
    entries_from_slot,
    mods_for_slot,
    prepare_write,
    read_slot,
)
from darksort.history import ProfileHistory
from darksort.profiles import ProfileStore, resolve_entries
from darksort.save_bindings import SaveBindings, compare_slot
from darksort.sorting import preview_order
from darksort.ui.mod_list import ROLE
from darksort.ui.plan_dialogs import confirmation_dialog, text_view


class SaveDialog(QDialog):
    def __init__(
        self, data_dir, steam_paths, game_roots, installed, resolved, parent=None, *,
        profile=None, initial_path=None, new_campaign=None, rule_marks=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("DD1 存档管理")
        self.resize(960, 580)
        self.backups = SaveBackups(data_dir)
        self.history = ProfileHistory(data_dir)
        self.store = ProfileStore(data_dir)
        self.bindings = SaveBindings(data_dir)
        self.profile = profile
        self.rule_marks = rule_marks or {}
        self.steam_paths = list(steam_paths)
        self.manual_slots: dict[str, str] = {}
        self.game_roots = list(game_roots)
        self.installed = list(installed)
        self.resolved = list(resolved)
        self.state: SaveState | None = None
        self.import_state: SaveState | None = None
        self.switch_to = None
        self.bound_profile = None
        self.binding_id = None
        self.binding_error = ""
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("目标档位"))
        self.slots = QComboBox()
        self.slots.currentIndexChanged.connect(self.load)
        row.addWidget(self.slots, 1)
        refresh = QPushButton("刷新档位")
        refresh.clicked.connect(self.refresh_slots)
        row.addWidget(refresh)
        browse = QPushButton("打开存档文件…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        layout.addLayout(row)
        self.path = QLabel()
        self.path.setObjectName("muted")
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path.setTextFormat(Qt.TextFormat.PlainText)
        self.path.setWordWrap(True)
        layout.addWidget(self.path)
        self.status = QLabel("选择现有档位或打开一份副本。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bindings_row = QHBoxLayout()
        self.binding_text = QLabel("选择档位后可绑定当前方案。")
        self.binding_text.setTextFormat(Qt.TextFormat.PlainText)
        self.binding_text.setWordWrap(True)
        bindings_row.addWidget(self.binding_text, 1)
        self.bind_button = QPushButton("绑定当前方案")
        self.bind_button.clicked.connect(lambda: self.change_binding(self.profile.id))
        bindings_row.addWidget(self.bind_button)
        self.unbind_button = QPushButton("解除绑定")
        self.unbind_button.clicked.connect(lambda: self.change_binding(None))
        bindings_row.addWidget(self.unbind_button)
        self.switch_button = QPushButton("切换到绑定方案")
        self.switch_button.clicked.connect(self.open_bound_profile)
        bindings_row.addWidget(self.switch_button)
        layout.addLayout(bindings_row)
        self.difference = QLabel()
        self.difference.setTextFormat(Qt.TextFormat.PlainText)
        self.difference.setObjectName("muted")
        layout.addWidget(self.difference)
        splitter = QSplitter()
        self.saved_view, _ = self.add_column(splitter, "档位当前顺序")
        self.desired_view, self.desired_title = self.add_column(splitter, "DarkSort 计划顺序")
        splitter.setSizes([455, 455])
        layout.addWidget(splitter, 1)
        actions = QDialogButtonBox()
        self.import_button = actions.addButton("从档位新建方案", QDialogButtonBox.ButtonRole.ActionRole)
        self.import_button.clicked.connect(self.import_selected)
        self.backup_button = actions.addButton("备份与恢复", QDialogButtonBox.ButtonRole.ActionRole)
        self.backup_button.clicked.connect(self.show_backups)
        self.apply_button = actions.addButton("预览应用…", QDialogButtonBox.ButtonRole.ActionRole)
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.apply_order)
        if new_campaign is not None:
            launch = actions.addButton("带方案开新档…", QDialogButtonBox.ButtonRole.ActionRole)
            launch.clicked.connect(lambda: new_campaign(self))
        actions.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)
        self.refresh_slots()
        if initial_path is not None:
            self.select_path(initial_path)

    def add_column(self, splitter, title):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(title)
        heading.setObjectName("section")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(heading)
        view = text_view()
        layout.addWidget(view, 1)
        splitter.addWidget(panel)
        return view, heading

    def browse(self):
        filename, _ = QFileDialog.getOpenFileName(self, "打开 DD1 存档", "", "DD1 存档 (*.json)")
        if not filename:
            return
        self.select_path(Path(filename).resolve())

    def select_path(self, path):
        path = path.resolve()
        target = str(path)
        index = self.slots.findData(target)
        if index < 0:
            description = f"手动选择 · {path.parent.name} / {path.name}"
            self.manual_slots[target] = description
            self.slots.addItem(description, target)
            index = self.slots.count() - 1
        if self.slots.currentIndex() == index:
            self.load()
        else:
            self.slots.setCurrentIndex(index)

    def refresh_slots(self):
        selected = self.slots.currentData()
        options = {str(slot.path): slot.label for slot in discover_slots(self.steam_paths)}
        for path, label in self.manual_slots.items():
            options.setdefault(path, label)
        with QSignalBlocker(self.slots):
            self.slots.clear()
            self.slots.addItem("请选择档位")
            for path, label in options.items():
                self.slots.addItem(label, path)
            self.slots.setCurrentIndex(max(0, self.slots.findData(selected)))
        self.load()

    def load(self, *_):
        self.state = None
        target = self.slots.currentData()
        self.path.setText(target or "")
        self.saved_view.clear()
        if target:
            try:
                self.state = read_slot(Path(target))
                version = f"DSON build {self.state.revision}" if self.state.revision else "JSON"
                availability = self.state.read_only_reason or "可预览应用"
                self.status.setText(f"{version}  ·  {len(self.state.mods)} 个 Mod  ·  {availability}")
            except (OSError, ValueError, RecursionError) as exc:
                self.status.setText(f"无法读取档位 · {exc}")
        else:
            self.status.setText("选择已经保存的档位，或用「带方案开新档」为新战役预载 Mod。")
        self.refresh_binding()

    def refresh_binding(self):
        self.bound_profile = None
        self.binding_id = None
        self.binding_error = ""
        if self.state:
            try:
                self.binding_id = self.bindings.get(self.state.path)
            except (OSError, ValueError) as exc:
                self.binding_error = f"无法读取档位绑定 · {exc}"
            if self.binding_id:
                try:
                    if self.profile and self.profile.id == self.binding_id:
                        self.bound_profile = self.profile
                    else:
                        self.bound_profile = self.store.load(self.binding_id)
                except (OSError, ValueError, TypeError):
                    pass
        if self.binding_error:
            text = self.binding_error
        elif self.bound_profile:
            text = f"已绑定方案 · {self.bound_profile.name}"
            if self.comparing_other():
                text += " · 切换方案后可应用"
        elif self.binding_id:
            text = "绑定的方案无法读取，可重新绑定或解除绑定。"
        elif self.state and self.profile:
            text = f"此档位未绑定 · 当前方案 {self.profile.name}"
        else:
            text = "选择档位后可绑定当前方案。"
        self.binding_text.setText(text)
        self.refresh_comparison()
        self.update_actions()

    def comparing_other(self):
        return self.bound_profile is not None and self.bound_profile is not self.profile

    def binding_allows_apply(self):
        return not self.binding_id or (self.profile is not None and self.bound_profile is self.profile)

    def refresh_comparison(self):
        plan = self.bound_profile or self.profile
        self.desired_title.setText(f"方案顺序 · {plan.name}" if plan else "DarkSort 计划顺序")
        if self.state:
            desired = resolve_entries(plan.entries, self.installed) if self.comparing_other() else self.resolved
            difference = compare_slot(self.state, self.installed, desired)
            self.saved_view.setPlainText("\n".join(difference.current) or "此档位的启用列表为空。")
            self.desired_view.setPlainText("\n".join(difference.desired) or "此方案为空。")
            summary = f"仅方案 {difference.plan_only}  ·  仅档位 {difference.slot_only}  ·  移位 {difference.moves}"
            if difference.unknown:
                summary += f"  ·  身份待确认 {difference.unknown}"
            self.difference.setText(summary)
        else:
            self.difference.clear()
            self.desired_view.setPlainText("\n".join(
                f"{i:02d}  {row.entry.name}" + ("  ·  待匹配安装" if row.mod is None else "")
                for i, row in enumerate(self.resolved, 1)
            ) or "此方案为空。")

    def change_binding(self, profile_id):
        if self.state is None or self.binding_error:
            return
        try:
            self.bindings.set(self.state.path, profile_id)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法保存档位绑定", str(exc))
        self.refresh_binding()

    def open_bound_profile(self):
        if self.comparing_other():
            self.switch_to = self.bound_profile.id
            self.accept()

    def update_actions(self):
        self.import_button.setEnabled(self.state is not None)
        self.backup_button.setEnabled(self.state is not None)
        self.apply_button.setEnabled(
            self.state is not None and not self.state.read_only_reason and self.binding_allows_apply()
        )
        self.bind_button.setText("改绑当前方案" if self.binding_id else "绑定当前方案")
        self.bind_button.setEnabled(bool(
            self.state and self.profile and not self.binding_error and self.binding_id != self.profile.id
        ))
        self.unbind_button.setVisible(bool(self.binding_id))
        self.switch_button.setVisible(self.comparing_other() and self.profile is not None)

    def import_selected(self):
        if self.state is not None:
            self.import_state = self.state
            self.accept()

    def apply_order(self):
        if self.state is None or not self.binding_allows_apply():
            return
        try:
            desired = mods_for_slot(self.resolved, self.installed, self.game_roots)
            plan = prepare_write(self.state, desired)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法准备方案", str(exc))
            return
        if plan.payload == self.state.raw:
            QMessageBox.information(self, "顺序一致", "档位中的启用列表已与当前方案一致。")
            return
        previous_positions = {mod.key: pos for pos, mod in enumerate(self.state.mods, 1)}
        selected_keys = {mod.key for mod in desired}
        current_entries = entries_from_slot(self.state, self.installed)
        lines = [str(self.state.path), "", f"{len(self.state.mods)} 个 Mod → {len(desired)} 个 Mod", ""]
        for index, (mod, row) in enumerate(zip(desired, self.resolved, strict=True), 1):
            old = previous_positions.get(mod.key)
            change = "加入" if old is None else f"{old:02d} → {index:02d}" if old != index else "原位"
            lines.append(f"{index:02d}  {row.entry.name}  ·  {change}")
        for mod, entry in zip(self.state.mods, current_entries, strict=True):
            if mod.key not in selected_keys:
                lines.append(f"移除  {entry.name}")
        if self.profile is not None and (self.profile.rules or self.profile.locks):
            rules = preview_order(self.profile, self.resolved, **self.rule_marks)
            notices = [*rules.blockers, *rules.notices]
            if rules.entries != tuple(self.profile.entries):
                notices.append("当前计划与规则建议的顺序不同。")
            if notices:
                lines.extend(["", "方案规则提示", *notices])
        lines.extend(["", "应用前会将原始存档备份到 DarkSort 数据目录。"])
        if confirmation_dialog(self, "核对档位与顺序", "\n".join(lines), "备份并应用"):
            try:
                refreshed = mods_for_slot(self.resolved, self.installed, self.game_roots)
                if refreshed != desired:
                    raise ValueError("Mod 标识已变动，请重新预览。")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "无法应用方案", str(exc))
                return
            self.finish_write(plan, record_profile=True)

    def finish_write(self, plan, *, record_profile=False):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        error = None
        try:
            backup = commit(plan, self.backups)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        self.load()
        if error is not None:
            QMessageBox.critical(self, "存档操作未完成", error)
        elif backup is not None:
            self.status.setText(f"{plan.reason}完成  ·  已验证 {len(plan.mods)} 个 Mod")
            message = f"原始备份\n{backup.directory}"
            if record_profile and self.profile is not None:
                try:
                    self.history.capture(self.profile, f"应用到 {plan.state.path.parent.name}")
                except (OSError, ValueError) as exc:
                    message += f"\n\n方案快照保存失败 · {exc}"
            QMessageBox.information(self, "存档已更新", message)

    def show_backups(self):
        if self.state is None:
            return
        backups, errors = self.backups.list(self.state.path)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"备份与恢复 · {self.state.path.parent.name}")
        dialog.resize(690, 410)
        layout = QVBoxLayout(dialog)
        items = QListWidget()
        setup = "只可直接恢复到与备份记录匹配的档位版本。"
        if not backups:
            setup = "此档位暂时没有 DarkSort 备份。"
        guide = QLabel(setup + (f"\n{len(errors)} 份备份清单无法读取。" if errors else ""))
        guide.setWordWrap(True)
        layout.addWidget(guide)
        for backup in backups:
            stamp = backup.created.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            item = QListWidgetItem(f"{stamp}  ·  {backup.reason}")
            item.setToolTip(str(backup.directory))
            item.setData(ROLE, backup)
            items.addItem(item)
        layout.addWidget(items)
        buttons = QDialogButtonBox()
        open_folder = buttons.addButton("打开备份目录", QDialogButtonBox.ButtonRole.ActionRole)
        restore = buttons.addButton("恢复所选", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        def changed(item, _previous=None):
            backup = item.data(ROLE) if item is not None else None
            restore.setEnabled(bool(backup and backup.after == self.state.digest))
            open_folder.setEnabled(backup is not None)

        items.currentItemChanged.connect(changed)
        if backups:
            items.setCurrentRow(0)
        else:
            changed(None)
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(items.currentItem().data(ROLE).directory)))
        )
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        backup = items.currentItem().data(ROLE)
        try:
            plan = self.backups.prepare_restore(read_slot(self.state.path), backup)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法恢复", str(exc))
            return
        if confirmation_dialog(
            self, "确认恢复", f"{plan.state.path}\n\n恢复后启用 {len(plan.mods)} 个 Mod。", "恢复并备份当前版本"
        ):
            self.finish_write(plan)
