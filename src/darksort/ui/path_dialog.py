from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from darksort.scanning.discovery import Discovery
from darksort.settings import Settings


class PathDialog(QDialog):
    def __init__(self, settings: Settings, discovery: Discovery | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("扫描路径 · DarkSort")
        self.resize(720, 730)
        self.settings = settings
        layout = QVBoxLayout(self)
        label = QLabel("指定目录只影响扫描范围。Workshop 和本地目录应选择包含各个 Mod 的父目录。")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.auto_detect = QCheckBox("自动检测 Steam 注册表与游戏库")
        self.auto_detect.setChecked(settings.auto_detect)
        layout.addWidget(self.auto_detect)
        layout.addWidget(QLabel("游戏目录（留空自动检测）"))
        row = QHBoxLayout()
        self.game = QLineEdit(settings.game_path)
        self.game.setPlaceholderText("…/steamapps/common/DarkestDungeon")
        row.addWidget(self.game)
        button = QPushButton("浏览…")
        button.clicked.connect(self.choose_game)
        row.addWidget(button)
        layout.addLayout(row)
        self.steam = self.path_field(
            layout, "额外 Steam 安装／库目录（每行一个，可留空）", settings.steam_paths
        )
        self.workshop = self.path_field(
            layout, "Workshop 根目录（每行一个；填写后覆盖自动检测）", settings.workshop_paths
        )
        self.local = self.path_field(
            layout, "本地 mods 根目录（每行一个；留空使用游戏目录/mods）", settings.local_paths
        )
        layout.addWidget(QLabel("排除目录（同时跳过其中的子目录）"))
        exclusions = QHBoxLayout()
        self.excluded = QListWidget()
        self.excluded.setAccessibleName("已排除的扫描目录")
        self.excluded.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.excluded.setMaximumHeight(100)
        self.excluded.addItems(settings.excluded_paths)
        exclusions.addWidget(self.excluded, 1)
        exclusion_actions = QVBoxLayout()
        add = QPushButton("添加排除…")
        add.clicked.connect(self.choose_exclusion)
        restore = QPushButton("取消排除")
        restore.setEnabled(False)
        restore.clicked.connect(self.restore_exclusions)
        self.excluded.itemSelectionChanged.connect(
            lambda: restore.setEnabled(bool(self.excluded.selectedItems()))
        )
        exclusion_actions.addWidget(add)
        exclusion_actions.addWidget(restore)
        exclusion_actions.addStretch()
        exclusions.addLayout(exclusion_actions)
        layout.addLayout(exclusions)
        if discovery:
            detected = "\n".join(f"{root.source.label}：{root.path}" for root in discovery.roots)
            info = QLabel("最近检测到：\n" + (detected or "尚未找到目录"))
            info.setObjectName("muted")
            info.setWordWrap(True)
            info.setTextInteractionFlags(info.textInteractionFlags())
            layout.addWidget(info)
        buttons = QDialogButtonBox()
        buttons.addButton("保存并扫描", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_game(self):
        path = QFileDialog.getExistingDirectory(
            self, "选择 DarkestDungeon 游戏目录", self.game.text()
        )
        if path:
            self.game.setText(path)

    def choose_exclusion(self):
        path = QFileDialog.getExistingDirectory(self, "选择要排除的目录", self.game.text())
        if path:
            try:
                resolved = Path(path).resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                QMessageBox.warning(self, "无法排除扫描目录", str(exc))
                return
            if resolved not in map(Path, self.exclusion_paths()):
                self.excluded.addItem(str(resolved))

    def restore_exclusions(self):
        for item in self.excluded.selectedItems():
            self.excluded.takeItem(self.excluded.row(item))

    def exclusion_paths(self):
        return [self.excluded.item(row).text() for row in range(self.excluded.count())]

    def path_field(self, layout, label, paths):
        layout.addWidget(QLabel(label))
        row = QHBoxLayout()
        edit = QPlainTextEdit("\n".join(paths))
        edit.setMaximumHeight(65)
        row.addWidget(edit)
        button = QPushButton("添加…")

        def choose():
            path = QFileDialog.getExistingDirectory(self, "选择包含各个 Mod 的目录")
            if path:
                edit.appendPlainText(path)

        button.clicked.connect(choose)
        row.addWidget(button)
        layout.addLayout(row)
        return edit

    def result_settings(self) -> Settings:
        def lines(edit):
            return list(
                dict.fromkeys(
                    line.strip() for line in edit.toPlainText().splitlines() if line.strip()
                )
            )

        return replace(
            self.settings,
            auto_detect=self.auto_detect.isChecked(),
            game_path=self.game.text().strip(),
            steam_paths=lines(self.steam),
            workshop_paths=lines(self.workshop),
            local_paths=lines(self.local),
            excluded_paths=self.exclusion_paths(),
        )
