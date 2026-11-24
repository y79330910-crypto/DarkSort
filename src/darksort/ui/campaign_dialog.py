from __future__ import annotations

import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from darksort.adapters.campaigns import launch_campaign
from darksort.ui.plan_dialogs import text_view


class CampaignDialog(QDialog):
    def __init__(self, data_dir, targets, profile_name, resolved, installed, parent=None):
        super().__init__(parent)
        self.data_dir = data_dir
        self.resolved = list(resolved)
        self.installed = list(installed)
        self.process = None
        self.setWindowTitle("带方案开新档")
        self.resize(650, 485)
        layout = QVBoxLayout(self)
        heading = QLabel(f"{profile_name} · {len(self.resolved)} 个 Mod", objectName="section")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.game = QComboBox()
        self.game.setAccessibleName("启动的游戏安装")
        for target in targets:
            self.game.addItem(target.label, target)
        layout.addWidget(self.game)
        guide = QLabel(
            "启动后在游戏主菜单选择空档位或刚建的新档。"
            "本次启动后第一个打开的战役会使用下方清单。\n"
            "进入后完成一次游戏保存，Mod 顺序就会随该档位保留。"
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)
        order = text_view()
        order.setAccessibleName("首次进入战役的 Mod 顺序")
        lines = []
        for index, row in enumerate(self.resolved, 1):
            name = row.mod.name if row.mod else f"{row.entry.name} · 待定位"
            lines.append(f"{index:02d}  {name}")
        order.setPlainText("\n".join(lines) if lines else "当前方案没有启用的 Mod。")
        layout.addWidget(order, 1)

        controls = QDialogButtonBox()
        self.launch_button = controls.addButton("带方案启动游戏", QDialogButtonBox.ButtonRole.ActionRole)
        self.launch_button.setObjectName("primary")
        self.launch_button.clicked.connect(self.launch)
        controls.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def launch(self):
        target = self.game.currentData()
        if target is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.process = launch_campaign(target, self.data_dir, self.resolved, self.installed)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
        else:
            error = None
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            QMessageBox.warning(self, "游戏未启动", error)
        else:
            self.accept()
