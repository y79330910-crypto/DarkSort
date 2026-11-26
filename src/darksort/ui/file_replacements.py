from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from darksort.file_replacements import ModOrigin, game_installation, import_preview
from darksort.resource_import import inspect_import
from darksort.search import ModLocation
from darksort.ui.conflict_dialog import plain_label
from darksort.ui.mod_list import ROLE
from darksort.ui.plan_dialogs import confirmation_dialog
from darksort.ui.search import GlobalSearchDialog
from darksort.ui.workspace_dock import data_table, fill_table

OPERATION_ERRORS = (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError)


class ResourceJob(QThread):
    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.result = self.failure = None

    def run(self):
        try:
            self.result = self.operation()
        except Exception as exc:
            self.failure = exc


def run_operation(parent, message, operation, *, title="游戏文件替换"):
    progress = QDialog(parent)
    progress.setWindowTitle(title)
    progress.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    progress.setMinimumWidth(350)
    layout = QVBoxLayout(progress)
    layout.addWidget(plain_label(message))
    bar = QProgressBar()
    bar.setRange(0, 0)
    layout.addWidget(bar)
    work = ResourceJob(operation, progress)
    work.finished.connect(progress.accept)
    work.start()
    progress.exec()
    work.wait()
    result, failure = work.result, work.failure
    progress.deleteLater()
    if failure:
        raise failure
    return result


class FileImportDialog(QDialog):
    def __init__(self, root, source, name, references, remove_current, parent, *, previous=None):
        super().__init__(parent)
        self.root = root
        self.source = source
        self.directory = previous.origin.directory if previous and previous.origin else ""
        self.setWindowTitle("导入预览 · 游戏文件替换")
        self.resize(750, 515)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(f"游戏安装目录 · {root}"))
        form = QFormLayout()
        self.name = QLineEdit(name, maxLength=120)
        form.addRow("名称", self.name)
        self.layout_choice = QComboBox()
        for choice in source.layouts:
            self.layout_choice.addItem(choice.prefix or "源目录", choice)
        if previous and previous.origin:
            index = next((i for i, item in enumerate(source.layouts) if item.prefix == previous.origin.layout), 0)
            self.layout_choice.setCurrentIndex(index)
        if len(source.layouts) > 1:
            form.addRow("包内资源起点", self.layout_choice)
        self.destination = QPushButton("选择游戏内的目标文件夹…")
        self.destination.clicked.connect(self.choose_destination)
        self.target_label = plain_label("目标文件夹")
        form.addRow(self.target_label, self.destination)
        layout.addLayout(form)
        if references:
            layout.addWidget(plain_label(references, "muted"))
        self.table = data_table(("游戏内目标路径", "操作"), (0, 95))
        layout.addWidget(self.table, 1)
        self.feedback = plain_label("", "muted")
        layout.addWidget(self.feedback)
        controls = QDialogButtonBox()
        self.confirm = controls.addButton(
            "更新导入副本" if previous else "导入并移出当前方案" if remove_current else "导入",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        controls.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.layout_choice.currentIndexChanged.connect(self.preview)
        self.name.textChanged.connect(self.update_confirm)
        self.preview()

    @property
    def selected_layout(self):
        return self.layout_choice.currentData()

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "选择游戏内的目标资源文件夹", str(self.root))
        if path:
            try:
                destination = Path(path).resolve().relative_to(self.root)
                if not destination.parts:
                    raise ValueError
            except (OSError, RuntimeError, ValueError):
                self.feedback.setText("请选择当前游戏目录下的资源文件夹。")
                return
            self.directory = destination.as_posix()
            self.preview()

    def preview(self, *_):
        choice = self.selected_layout
        self.destination.setVisible(choice.manual)
        self.target_label.setVisible(choice.manual)
        self.destination.setText(self.directory or "选择游戏内的目标文件夹…")
        self.rows = ()
        self.confirm.setEnabled(False)
        try:
            self.rows = import_preview(self.root, (path for path, _ in choice.targets(self.directory)))
        except (OSError, ValueError) as exc:
            fill_table(self.table, ())
            self.feedback.setText(str(exc))
            return
        fill_table(self.table, self.rows)
        replaced = sum(status == "替换" for _, status in self.rows)
        self.feedback.setText(f"新增 {len(self.rows) - replaced} 项 · 替换 {replaced} 项")
        self.update_confirm()

    def update_confirm(self):
        self.confirm.setEnabled(bool(self.rows and self.name.text().strip()))


class FilePackDialog(QDialog):
    def __init__(self, owner, *, mod=None):
        super().__init__(owner)
        self.owner = owner
        self.store = owner.file_packs
        self.packs = []
        self.states = {}
        self.versions = {}
        self.setWindowTitle("游戏文件替换 · DarkSort")
        self.resize(850, 495)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        add = QPushButton("导入…")
        add.setObjectName("primary")
        menu = QMenu(add)
        menu.addAction("从 Mod 列表…", self.choose_mod)
        menu.addAction("ZIP 压缩包…", self.choose_zip)
        menu.addAction("资源文件夹…", self.choose_folder)
        menu.addAction("散文件…", self.choose_files)
        add.setMenu(menu)
        bar.addWidget(add)
        refresh = QPushButton("检查来源更新")
        refresh.clicked.connect(self.check_updates)
        bar.addWidget(refresh)
        bar.addStretch()
        layout.addLayout(bar)
        content = QSplitter()
        self.list = QListWidget()
        self.list.setAccessibleName("已导入的游戏资源包")
        content.addWidget(self.list)
        detail = QWidget()
        details = QVBoxLayout(detail)
        details.setContentsMargins(4, 0, 0, 0)
        self.description = plain_label("")
        details.addWidget(self.description)
        self.table = data_table(("游戏内目标路径",), (0,))
        details.addWidget(self.table, 1)
        content.addWidget(detail)
        content.setSizes([265, 560])
        layout.addWidget(content, 1)
        actions = QHBoxLayout()
        self.enable = QPushButton("启用…")
        self.enable.clicked.connect(self.enable_pack)
        self.disable = QPushButton("停用并还原")
        self.disable.clicked.connect(self.disable_pack)
        self.update = QPushButton("更新导入副本…")
        self.update.clicked.connect(self.update_pack)
        self.workshop = QPushButton("打开创意工坊")
        self.workshop.clicked.connect(self.open_workshop)
        self.remove = QPushButton("卸载…")
        self.remove.clicked.connect(self.remove_pack)
        for control in (self.enable, self.disable, self.update, self.workshop, self.remove):
            actions.addWidget(control)
        actions.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.list.currentItemChanged.connect(self.show_pack)
        self.reload()
        if mod:
            QTimer.singleShot(0, self, lambda: self.import_mod(mod))
        elif any(pack.origin for pack in self.packs):
            QTimer.singleShot(0, self, self.check_updates)

    def selected(self):
        item = self.list.currentItem()
        return item.data(ROLE) if item else None

    def reload(self, identity=""):
        current = self.selected()
        identity = identity or (current.id if current else "")
        self.packs, errors = self.store.list()
        self.list.clear()
        self.states = {}
        for pack in self.packs:
            try:
                state = self.store.state(pack)
            except OPERATION_ERRORS as exc:
                state = str(exc)
            self.states[pack.id] = state
            status = self.status(state)
            version = self.versions.get(pack.id, "")
            item = QListWidgetItem(f"{pack.name}\n{status}" + (f" · {version}" if version else ""))
            item.setToolTip(f"{pack.name}\n{pack.game}")
            item.setData(ROLE, pack)
            self.list.addItem(item)
            if pack.id == identity:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentItem() is None:
            self.list.setCurrentRow(0)
        self.show_pack()
        if errors:
            self.description.setText(self.description.text() + "\n" + "\n".join(errors))

    @staticmethod
    def status(state):
        if isinstance(state, str):
            return "状态暂不可用"
        if state is None:
            return "未启用"
        return "已启用" if state.phase == "enabled" else "等待还原"

    def show_pack(self, *_):
        pack = self.selected()
        state = self.states.get(pack.id) if pack else None
        known = bool(pack and not isinstance(state, str))
        self.enable.setEnabled(known and state is None)
        self.disable.setEnabled(known and state is not None)
        self.update.setEnabled(bool(known and state is None and pack.origin))
        self.workshop.setEnabled(bool(pack and pack.origin and pack.origin.workshop_id))
        self.remove.setEnabled(known)
        self.remove.setText("移回普通 Mod 列表…" if pack and pack.origin else "卸载…")
        if pack:
            source = f"\n来源 · {pack.origin.path}" if pack.origin else ""
            self.description.setText(
                f"{self.status(state)} · {pack.game}{source}" + (f"\n{state}" if isinstance(state, str) else "")
            )
            fill_table(self.table, ((asset.relative,) for asset in pack.files))
        else:
            self.description.setText("可导入汉化、标题背景等有明确目标位置的游戏资源包。")
            fill_table(self.table, ())

    def choose_game(self):
        roots = []
        for path in self.owner.game_locations():
            try:
                root = game_installation(path)
            except OPERATION_ERRORS:
                continue
            if root not in roots:
                roots.append(root)
        if len(roots) == 1:
            return roots[0]
        if roots:
            name, accepted = QInputDialog.getItem(
                self, "选择游戏安装", "要处理哪一份《暗黑地牢 1》？", [str(p) for p in roots], editable=False,
            )
            return Path(name) if accepted else None
        path = QFileDialog.getExistingDirectory(self, "选择《暗黑地牢 1》游戏安装目录")
        return game_installation(Path(path)) if path else None

    def choose_mod(self):
        candidates = {
            mod.key: mod for mod in self.owner.mods if mod.key not in self.owner.file_tool_keys
        }
        records = [
            self.owner.search_record(ModLocation("library", mod.key), mod, None, "已扫描")
            for mod in candidates.values()
        ]
        dialog = GlobalSearchDialog(records, self, rule_target=False)
        dialog.setWindowTitle("选择要转入工具的 Mod")
        dialog.jump.setText("转入…")
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        target = dialog.target
        dialog.deleteLater()
        if accepted and target:
            self.import_mod(candidates[target.identity])

    def choose_zip(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 ZIP 资源包", "", "ZIP 压缩包 (*.zip)")
        if path:
            self.import_paths((Path(path),))

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择要导入的资源文件夹")
        if path:
            self.import_paths((Path(path),))

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择资源文件")
        if paths:
            self.import_paths(tuple(Path(path) for path in paths))

    def import_mod(self, mod):
        origin = ModOrigin(mod.path, mod.key, mod.reference, self.owner.workshop_identifier(mod))
        self.import_paths((Path(mod.path),), origin=origin, name=self.owner.display_name(mod.reference, mod.name))

    def import_paths(self, paths, *, origin=None, name="", previous=None):
        try:
            root = Path(previous.game) if previous else self.choose_game()
            if root is None:
                return
            source = run_operation(self, "正在读取资源文件…", lambda: inspect_import(paths))
            if origin and not previous:
                updated, references = self.owner.plan_for_file_import(origin)
            else:
                updated, references = None, ""
            dialog = FileImportDialog(
                root, source, name or source.name, references, updated is not None, self, previous=previous,
            )
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            selected = dialog.selected_layout
            directory = dialog.directory if selected.manual else ""
            title = dialog.name.text()
            dialog.deleteLater()
            if not accepted:
                return
            pack = run_operation(self, "正在保存资源包副本…", lambda: self.store.import_pack(
                title, root, source, selected, directory, origin=origin, previous=previous,
            ))
            self.owner.refresh_file_packs(updated)
            self.versions.pop(pack.id, None)
            self.reload(pack.id)
        except OPERATION_ERRORS as exc:
            QMessageBox.critical(self, "导入失败", str(exc))

    def check_updates(self):
        def inspect():
            versions = {}
            for pack in self.packs:
                if pack.origin:
                    try:
                        if self.store.has_update(pack):
                            versions[pack.id] = "有更新"
                    except OPERATION_ERRORS:
                        versions[pack.id] = "来源暂不可用"
            return versions

        if any(pack.origin for pack in self.packs):
            self.versions = run_operation(self, "正在检查来源更新…", inspect)
            self.reload()

    def update_pack(self):
        pack = self.selected()
        if pack and pack.origin:
            self.import_paths((Path(pack.origin.path),), name=pack.name, origin=pack.origin, previous=pack)

    def open_workshop(self):
        pack = self.selected()
        if pack and pack.origin and pack.origin.workshop_id:
            self.owner.open_workshop(identifier=pack.origin.workshop_id)

    def enable_pack(self):
        pack = self.selected()
        if pack is None:
            return
        try:
            if pack.origin and any(
                (row.mod.reference if row.mod else row.entry.reference) == pack.origin.reference
                for row in self.owner.resolved
            ):
                raise ValueError("此 Mod 仍在当前计划中。请先移出，再使用游戏文件替换。")
            originals = run_operation(self, "正在核对游戏文件…", lambda: self.store.preview(pack))
            details = "\n".join(
                f"{'替换' if original else '新增'}  {asset.relative}"
                for asset, original in zip(pack.files, originals, strict=True)
            )
            if confirmation_dialog(
                self, "启用资源包",
                f"「{pack.name}」将应用到此游戏安装的所有档位。\n{pack.game}\n\n{details}",
                "备份并启用",
            ):
                run_operation(self, "正在备份并安装…", lambda: self.store.enable(pack, originals))
        except OPERATION_ERRORS as exc:
            QMessageBox.critical(self, "启用失败", str(exc))
        self.reload(pack.id)

    def disable_pack(self):
        pack = self.selected()
        if pack:
            try:
                run_operation(self, "正在还原游戏文件…", lambda: self.store.disable(pack))
            except OPERATION_ERRORS as exc:
                QMessageBox.critical(self, "还原失败", str(exc))
            self.reload(pack.id)

    def remove_pack(self):
        pack = self.selected()
        if pack is None:
            return
        action = "移回普通 Mod 列表" if pack.origin else "卸载资源包"
        if QMessageBox.question(self, action, f"确认{action}「{pack.name}」？启用的资源会先停用并还原。") != QMessageBox.StandardButton.Yes:
            return
        try:
            def remove():
                if self.store.state(pack):
                    self.store.disable(pack)
                self.store.remove(pack)

            run_operation(self, "正在还原并移除导入副本…", remove)
            self.owner.refresh_file_packs()
            self.versions.pop(pack.id, None)
        except OPERATION_ERRORS as exc:
            QMessageBox.critical(self, "移除失败", str(exc))
        self.reload()
