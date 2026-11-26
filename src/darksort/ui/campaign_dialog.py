from __future__ import annotations

import subprocess

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from darksort.adapters.campaigns import launch_campaign
from darksort.adapters.launch_options import parse_launch_options, read_steam_launch_options
from darksort.models import path_key
from darksort.ui.plan_dialogs import text_view


class CampaignDialog(QDialog):
    def __init__(
        self, data_dir, targets, profile_name, resolved, installed, parent=None, *,
        rules=(), launch_preferences=None, steam_roots=(),
    ):
        super().__init__(parent)
        self.data_dir = data_dir
        self.resolved = list(resolved)
        self.installed = list(installed)
        self.rules = tuple(rules)
        self.process = None
        self.launch_preference = None
        self.preferences = {key: value.copy() for key, value in (launch_preferences or {}).items()}
        self.steam_roots = tuple(steam_roots)
        self.steam_options = self.steam_error = ""
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
        self.build_launch_options(layout)
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
        self.game.currentIndexChanged.connect(self.select_game)
        self.select_game()

    def build_launch_options(self, layout):
        heading = QHBoxLayout()
        self.options_toggle = QToolButton(text="启动项", checkable=True, objectName="quiet")
        self.options_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.options_toggle.setArrowType(Qt.ArrowType.RightArrow)
        heading.addWidget(self.options_toggle)
        self.options_summary = QLabel(objectName="muted", wordWrap=True)
        self.options_summary.setTextFormat(Qt.TextFormat.PlainText)
        heading.addWidget(self.options_summary, 1)
        layout.addLayout(heading)

        self.options_panel = QWidget()
        options = QFormLayout(self.options_panel)
        self.option_mode = QComboBox()
        self.option_mode.addItem("沿用 Steam", "steam")
        self.option_mode.addItem("手动输入", "manual")
        actions = QHBoxLayout()
        actions.addWidget(self.option_mode, 1)
        self.refresh_button = QPushButton("刷新 Steam", objectName="quiet")
        self.refresh_button.clicked.connect(self.refresh_steam_options)
        actions.addWidget(self.refresh_button)
        self.use_steam_button = QPushButton("填入 Steam 参数", objectName="quiet")
        self.use_steam_button.clicked.connect(lambda: self.option_text.setText(self.steam_options))
        actions.addWidget(self.use_steam_button)
        options.addRow("参数来源", actions)

        self.option_text = QLineEdit(placeholderText="填写游戏启动参数")
        self.option_text.setAccessibleName("游戏启动参数")
        options.addRow("启动参数", self.option_text)
        self.option_preview = QLabel(wordWrap=True, objectName="muted")
        self.option_preview.setTextFormat(Qt.TextFormat.PlainText)
        options.addRow("本次附加", self.option_preview)
        self.options_panel.hide()
        layout.addWidget(self.options_panel)
        self.options_toggle.toggled.connect(self.toggle_launch_options)
        self.option_mode.currentIndexChanged.connect(self.update_option_mode)
        self.option_text.textChanged.connect(self.update_option_text)

    def toggle_launch_options(self, expanded):
        self.options_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.options_panel.setVisible(expanded)

    def option_preference(self, target):
        key = path_key(target.root)
        return self.preferences.setdefault(key, {"mode": "steam" if target.steam else "manual", "manual": ""})

    def select_game(self):
        target = self.game.currentData()
        if target is None:
            self.launch_button.setEnabled(False)
            return
        mode = self.option_preference(target)["mode"] if target.steam else "manual"
        with QSignalBlocker(self.option_mode):
            self.option_mode.model().item(0).setEnabled(target.steam)
            self.option_mode.setCurrentIndex(self.option_mode.findData(mode))
        self.refresh_steam_options()

    def refresh_steam_options(self):
        target = self.game.currentData()
        if target is None:
            return
        self.steam_options = self.steam_error = ""
        if target.steam:
            try:
                self.steam_options = read_steam_launch_options(self.steam_roots)
            except (OSError, ValueError, RuntimeError) as exc:
                self.steam_error = str(exc)
        self.display_launch_options(target)

    def update_option_mode(self):
        target = self.game.currentData()
        if target is not None:
            self.option_preference(target)["mode"] = self.option_mode.currentData()
            self.display_launch_options(target)

    def display_launch_options(self, target):
        manual = self.option_mode.currentData() == "manual"
        self.option_text.setReadOnly(not manual)
        with QSignalBlocker(self.option_text):
            self.option_text.setText(self.option_preference(target)["manual"] if manual else self.steam_options)
        self.refresh_button.setVisible(target.steam)
        self.use_steam_button.setVisible(target.steam and manual and bool(self.steam_options))
        self.update_preview()

    def update_option_text(self, text):
        target = self.game.currentData()
        if target is not None and self.option_mode.currentData() == "manual":
            self.option_preference(target)["manual"] = text
        self.update_preview()

    def update_preview(self):
        source = self.option_mode.currentText()
        try:
            options = parse_launch_options(self.option_text.text())
        except (OSError, ValueError) as exc:
            self.options_summary.setText(f"{source} · 需要调整参数")
            self.option_preview.setText(str(exc))
            self.options_toggle.setChecked(True)
            self.launch_button.setEnabled(False)
            return
        if self.option_mode.currentData() == "steam" and self.steam_error:
            summary = "沿用 Steam · 未读到启动项，可手动输入"
            preview = self.steam_error
        else:
            summary = f"{source} · {len(options.arguments)} 个附加参数"
            preview = subprocess.list2cmdline(options.arguments) if options.arguments else "仅加载当前方案"
        if options.replaced_profile:
            summary += " · 使用当前方案清单"
            preview += "\n检测到已有 -mod_profile。本次使用当前方案的清单。"
        self.options_summary.setText(summary)
        self.option_preview.setText(preview)
        self.launch_button.setEnabled(True)

    def launch(self):
        target = self.game.currentData()
        if target is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.process = launch_campaign(
                target, self.data_dir, self.resolved, self.installed,
                rules=self.rules, launch_options=self.option_text.text(),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
        else:
            error = None
        finally:
            QApplication.restoreOverrideCursor()
        if error is not None:
            QMessageBox.warning(self, "游戏未启动", error)
        else:
            preference = self.option_preference(target).copy()
            preference["mode"] = self.option_mode.currentData()
            self.launch_preference = (path_key(target.root), preference)
            self.accept()
