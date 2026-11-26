from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
)

from darksort.profiles import MAX_PROFILE_NAME
from darksort.search import search_text
from darksort.sharing import suggested_profile_name
from darksort.ui.conflict_dialog import plain_label
from darksort.ui.mod_list import ROLE
from darksort.ui.plan_dialogs import text_view


class ProfileImportDialog(QDialog):
    def __init__(self, source, profile, existing, preview, parent=None):
        super().__init__(parent)
        self.existing = {search_text(name) for name in existing}
        self.setWindowTitle("导入方案 · DarkSort")
        self.resize(680, 460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        filename = plain_label(source.name)
        filename.setToolTip(str(source))
        form.addRow("来源文件", filename)
        self.name = QLineEdit(suggested_profile_name(profile.name, source, self.existing))
        self.name.setAccessibleName("新方案名称")
        form.addRow("方案名称", self.name)
        layout.addLayout(form)
        self.feedback = plain_label("", "muted")
        layout.addWidget(self.feedback)
        self.preview = text_view(preview)
        self.preview.setAccessibleName("导入方案预览")
        layout.addWidget(self.preview, 1)
        controls = QDialogButtonBox()
        self.confirm = controls.addButton("导入为新方案", QDialogButtonBox.ButtonRole.AcceptRole)
        self.confirm.setObjectName("primary")
        controls.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.name.textChanged.connect(self.validate_name)
        self.name.setFocus()
        self.name.selectAll()
        self.validate_name()

    def validate_name(self):
        name = self.name.text().strip()
        message = ""
        if not 0 < len(name) <= MAX_PROFILE_NAME:
            message = f"请输入 1 至 {MAX_PROFILE_NAME} 个字符的方案名称。"
        elif search_text(name) in self.existing:
            message = "已有同名方案，请换一个名称。"
        self.confirm.setEnabled(not message)
        self.feedback.setText(message)
        self.feedback.setVisible(bool(message))


class ProfileManagerDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("管理方案 · DarkSort")
        self.resize(770, 485)
        layout = QVBoxLayout(self)
        splitter = QSplitter()
        self.plans = QListWidget()
        self.plans.setAccessibleName("选择方案")
        splitter.addWidget(self.plans)
        self.preview = text_view()
        self.preview.setAccessibleName("所选方案的 Mod 顺序")
        splitter.addWidget(self.preview)
        splitter.setSizes([275, 495])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        controls = QDialogButtonBox()
        self.rename = controls.addButton("重命名…", QDialogButtonBox.ButtonRole.ActionRole)
        self.rename.clicked.connect(self.rename_selected)
        self.delete = controls.addButton("删除…", QDialogButtonBox.ButtonRole.ActionRole)
        self.delete.clicked.connect(self.delete_selected)
        controls.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.plans.currentItemChanged.connect(self.show_profile)
        self.reload(owner.profile.id)

    def reload(self, selected_id):
        profiles, _ = self.owner.store.list()
        selected = 0
        with QSignalBlocker(self.plans):
            self.plans.clear()
            for index, saved in enumerate(profiles):
                current = saved.id == self.owner.profile.id
                profile = self.owner.profile if current else saved
                marker = " · 当前方案" if current else ""
                item = QListWidgetItem(f"{profile.name}\n{len(profile.entries)} 个 Mod{marker}")
                item.setToolTip(profile.name)
                item.setData(ROLE, profile)
                self.plans.addItem(item)
                if profile.id == selected_id:
                    selected = index
            self.plans.setCurrentRow(selected)
        self.show_profile(self.plans.currentItem())

    def show_profile(self, item, *_):
        self.rename.setEnabled(item is not None)
        self.delete.setEnabled(item is not None)
        if not item:
            self.preview.clear()
            return
        profile = item.data(ROLE)
        sections = []
        for title, entries in (("计划", profile.entries), *((group.title, group.members) for group in profile.mod_groups)):
            lines = [f"{title} · {len(entries)} 项"]
            lines.extend(f"{index:02d}  {self.owner.display_name(entry.reference, entry.name)}"
                         for index, entry in enumerate(entries, 1))
            sections.append("\n".join(lines))
        self.preview.setPlainText("\n\n".join(sections))

    def rename_selected(self):
        item = self.plans.currentItem()
        if not item:
            return
        profile = item.data(ROLE)
        name, accepted = QInputDialog.getText(self, "重命名方案", "方案名称", text=profile.name)
        if accepted:
            try:
                self.owner.rename_profile(profile.id, name)
                self.reload(profile.id)
            except (OSError, ValueError, TypeError) as exc:
                QMessageBox.warning(self, "重命名失败", str(exc))

    def delete_selected(self):
        item = self.plans.currentItem()
        if not item:
            return
        profile = item.data(ROLE)
        try:
            count = sum(identity == profile.id for identity in self.owner.save_bindings.read().values())
            lines = [f"删除方案「{profile.name}」？", f"{len(profile.entries)} 个 Mod · {len(profile.mod_groups)} 个分组"]
            if count:
                lines.append(f"将解除 {count} 项档位绑定。")
            if profile.id == self.owner.profile.id and self.owner.dirty:
                lines.append("当前有未保存的更改，删除时会丢弃这些更改。")
            box = QMessageBox(QMessageBox.Icon.Warning, "删除方案", "\n".join(lines), parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            remove = box.addButton("删除方案", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            accepted = box.clickedButton() == remove
            box.deleteLater()
            if accepted:
                self.owner.delete_profile(profile.id)
                self.reload(self.owner.profile.id)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "删除方案失败", str(exc))
