from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

from darksort.sorting import OrderPreview
from darksort.ui.theme import icon


def data_table(headers, widths) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(23)
    table.verticalHeader().setMinimumSectionSize(20)
    header = table.horizontalHeader()
    header.setFixedHeight(24)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    for index, width in enumerate(widths):
        if width:
            table.setColumnWidth(index, width)
        else:
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
    return table


def fill_table(table: QTableWidget, rows) -> None:
    rows = list(rows)
    table.setUpdatesEnabled(False)
    try:
        table.setRowCount(len(rows))
        for index, values in enumerate(rows):
            for column, value in enumerate(values):
                text = str(value)
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                table.setItem(index, column, item)
    finally:
        table.setUpdatesEnabled(True)


def decision_rows(preview: OrderPreview, *, changed_only=False):
    return (
        (
            f"{row.before}  →  {row.after}" if row.before != row.after else str(row.after),
            row.name,
            row.category,
            row.reason,
        )
        for row in preview.decisions
        if not changed_only or row.before != row.after
    )


class WorkspaceDock(QTabWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("diagnosticsDock")
        self.setDocumentMode(True)
        self.tabBar().setDrawBase(False)
        self.setMinimumHeight(121)
        self.sort = self.add_page(
            "排序",
            "预览会显示每个 Mod 的分类、位置与依据。",
            "智能排序…",
            lambda: window.sort_profile(),
            ("序号", "MOD", "分类", "排序依据"),
            (77, 250, 100, 0),
        )
        self.issues = self.add_page(
            "提示",
            "当前没有扫描提示。",
            "查看详情…",
            window.show_issues,
            ("来源", "说明"),
            (220, 0),
        )
        self.hide_button = QPushButton(icon("close"), "")
        self.hide_button.setObjectName("quiet")
        self.hide_button.setFixedSize(26, 23)
        self.hide_button.setToolTip("收起诊断面板")
        self.hide_button.setAccessibleName("收起诊断面板")
        self.hide_button.clicked.connect(window.toggle_dock)
        self.setCornerWidget(self.hide_button, Qt.Corner.TopRightCorner)

    def add_page(self, title, info, action, callback, headers, widths):
        page = QFrame()
        page.setObjectName("dockBody")
        body = QVBoxLayout(page)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 3, 8, 3)
        caption = QLabel(info)
        caption.setTextFormat(Qt.TextFormat.PlainText)
        caption.setObjectName("muted")
        toolbar.addWidget(caption, 1)
        button = QPushButton(action)
        button.setObjectName("quiet")
        button.clicked.connect(callback)
        toolbar.addWidget(button)
        body.addLayout(toolbar)
        table = data_table(headers, widths)
        body.addWidget(table, 1)
        index = self.addTab(page, title)
        return index, caption, table

    def set_sort(self, preview: OrderPreview | None):
        index, caption, table = self.sort
        if preview is None:
            caption.setText("预览会显示每个 Mod 的分类、位置与依据。")
            fill_table(table, ())
            self.setTabText(index, "排序")
        elif preview.blockers:
            caption.setText(f"{len(preview.blockers)} 条规则需要处理")
            fill_table(table, (("—", "规则诊断", "循环或歧义", line) for line in preview.blockers))
            self.setTabText(index, "排序 · 需处理")
        else:
            changed = sum(row.before != row.after for row in preview.decisions)
            count = len(preview.decisions)
            caption.setText(f"{changed} 个 Mod 建议移动  ·  {count} 个条目")
            fill_table(table, decision_rows(preview))
            self.setTabText(index, f"排序  {changed}")

    def set_issues(self, scan_result):
        index, caption, table = self.issues
        rows = [(issue.path, issue.message) for issue in scan_result.issues]
        rows += [(mod.name, warning) for mod in scan_result.mods for warning in mod.warnings]
        caption.setText(f"{len(rows)} 条扫描提示" if rows else "当前没有扫描提示。")
        fill_table(table, rows)
        self.setTabText(index, f"提示  {len(rows)}")


