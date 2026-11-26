from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from darksort.ui.conflict_dialog import plain_label


def destination_picker(parents):
    picker = QComboBox()
    picker.setAccessibleName("模组存放目录")
    picker.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    picker.setMinimumContentsLength(26)
    picker.currentTextChanged.connect(picker.setToolTip)
    for parent in parents:
        picker.addItem(str(parent), parent)
    return picker


class NewModDialog(QDialog):
    def __init__(self, parents, manual, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建空模组")
        self.resize(650, 245)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.destination = destination_picker(parents)
        self.destination.addItem(f"手动安装工具 · {manual}", manual)
        form.addRow("存放位置", self.destination)
        self.title = QLineEdit(maxLength=120)
        self.title.setPlaceholderText("输入 Mod 或工具名称")
        form.addRow("名称", self.title)
        self.description = QPlainTextEdit(placeholderText="可选，写下这个 Mod 或工具的安装方法")
        self.description.setFixedHeight(75)
        form.addRow("安装说明", self.description)
        layout.addLayout(form)
        buttons = QDialogButtonBox()
        create = buttons.addButton("创建并打开目录", QDialogButtonBox.ButtonRole.AcceptRole)
        create.setObjectName("primary")
        create.setEnabled(False)
        self.title.textChanged.connect(lambda title: create.setEnabled(bool(title.strip())))
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.title.setFocus()


class WorkshopCopyDialog(QDialog):
    def __init__(self, mod, parents, in_plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("工坊 Mod 转为本地")
        self.resize(680, 200)
        layout = QVBoxLayout(self)
        name = plain_label(f"{mod.name} · Workshop {mod.workshop_id}")
        name.setToolTip(mod.path)
        name.setWordWrap(True)
        layout.addWidget(name)
        form = QFormLayout()
        self.destination = destination_picker(parents)
        form.addRow("复制到", self.destination)
        layout.addLayout(form)
        self.replace_plan = QCheckBox("用本地副本替换当前计划中的条目", checked=in_plan)
        self.replace_plan.setVisible(in_plan)
        layout.addWidget(self.replace_plan)
        layout.addWidget(plain_label("本地副本会保留工坊链接。订阅状态由 Steam 管理。"))
        buttons = QDialogButtonBox()
        copy = buttons.addButton("复制到本地", QDialogButtonBox.ButtonRole.AcceptRole)
        copy.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
