from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from darksort.search import search_text
from darksort.ui.mod_list import ROLE

FEATURES = (
    ("扫描与文件", (
        ("扫描已安装 Mod", "主界面 → 扫描 · F5",
         "发现本地和创意工坊模组。修改过模组文件时，可以用「工具 → 强制刷新」重新读取。"),
        ("扫描路径和排除", "工具 → 扫描路径…",
         "指定游戏、Steam 库和额外模组目录。这里也可以管理排除目录。"
         "右键 → 文件与扫描 → 从扫描中排除，可跳过当前模组。"),
        ("新建空模组", "工具 → 新建空模组…",
         "在游戏的 mods 目录创建带元数据的空包。手动安装的工具也可以单独建目录并记录安装方法。"),
        ("工坊 Mod 转为本地", "工具 → 工坊 Mod 转为本地…",
         "复制工坊内容到本地并保留原页面链接。可以把当前计划中的条目切换到副本。"),
        ("卸载或删除本地 Mod", "Mod 右键 → 文件与扫描 → 卸载本地 Mod…",
         "核对目录与引用它的方案，再将文件移到 Windows 回收站。"
         "工坊模组可在详细选项的「创意工坊」里打开 Steam 的取消订阅入口。"),
        ("游戏文件替换", "工具 → 游戏文件替换…",
         "导入汉化、标题背景等资源，预览后启用。停用时还原备份并移除该包新增的文件。"
         "也能通过右键详细选项的「文件与扫描」转入已扫描的模组。"),
        ("扫描提示", "工具 → 扫描提示",
         "查看扫描失败、重复 Workshop ID 和缺少元数据等提示。"),
    )),
    ("查找与 Mod 详情", (
        ("跨列表搜全部", "主界面 → 搜全部 · Ctrl+F",
         "按名称、别名或 Workshop ID 查找可用、计划和分组中的模组，选中结果即可定位。"),
        ("列表筛选与定位", "可用 Mod 或计划顺序 → 搜索栏",
         "「筛选」收窄当前列表，「条件」可按来源、分类、标签等组合筛选。"
         "在「定位」中用回车和 Shift+回车在结果间跳转。"),
        ("别名、简介与个人备注", "左侧 Mod 详情 → 双击 Mod 名称",
         "名称下方可直接改备注。双击名称可一起编辑别名、简介和备注，备注随本机设置保存。"),
        ("标签、颜色与分类", "Mod 右键 → 模组信息",
         "编辑标签、标记颜色并修正自动分类。多选后可以批量打标签和标色。左侧详情也提供这些入口。"),
        ("创意工坊链接", "Mod 右键 → 创意工坊",
         "打开 Steam 或浏览器、复制链接。缺少 ID 的本地模组可以关联工坊页面，"
         "关联后也能按 Workshop ID 搜索。"),
        ("打开目录与封面", "左侧 Mod 详情 → 打开目录，或点击封面",
         "在资源管理器中查看安装目录，点击封面可放大查看。"),
    )),
    ("计划、分组与排序", (
        ("启用和调整加载顺序", "可用 Mod → 双击或「加入」 · 计划顺序 → 拖动或上下箭头",
         "计划顶部优先。拖到指定位置前，清除计划筛选并展开折叠的计划标题。"),
        ("分组、整组启停与模板", "工作区 → 分组 · Mod 右键 → 分组管理",
         "整理多套候选模组。拖组头手柄可整组启停，拖组员可调组内顺序或加入计划。"
         "组头「⋯」能编辑或保存模板，点「载入模板」可复用。"
         "详细选项里的分组管理位于「计划与分组」。"),
        ("计划标题", "计划条目右键 → 计划与分组 → 添加或编辑分组标题…",
         "在计划中插入可折叠标题，也可以从可用 Mod 右键「加入计划并设标题」。"
         "右击已有标题可以编辑，或保存为可拖动分组。"),
        ("联锁与解除联锁", "计划条目右键 → 计划与分组",
         "多选后用「联锁选中项」让成员整块移动和停用。"
         "同处可解除联锁。也可以多选可用 Mod，右键「加入计划并联锁」。"
         "停用的联锁组还能从分组整组启用。"),
        ("设置单向前置", "Mod 右键 → 计划与分组 → 设置前置…",
         "启用时自动补齐前置。停用依赖方后，前置继续启用。"
         "如前置仍被计划中的模组使用，停用前置时会指出依赖方。"),
        ("智能排序", "主界面 → 智能排序",
         "预览分类、建议位置和依据。确认后可保存方案。按钮旁的箭头也能仅按自定义规则排序。"),
        ("用户排序规则", "工具 → 用户规则…",
         "设置 Mod、分组、标签和颜色之间的优先位置。"
         "也可以从右键详细选项的「计划与分组」带入当前 Mod。"),
        ("手动匹配安装", "计划底部 → 匹配… · 分组待匹配项右键 → 手动匹配本机 Mod…",
         "为缺失、移位或有多份同 ID 安装的条目选择本机对应目录。"
         "也可以在可用库右键「计划与分组 → 匹配待定位条目」使用当前安装。"),
        ("关系和依赖连线", "工作区 → 关联 · 计划顺序 → 行左侧连线",
         "查看相对优先级、启用依赖和人物资源关联。可点关联目标跳转，再返回原位置。"),
        ("冲突检查和忽略", "工作区 → 冲突 · 计划行尾黄标或红标",
         "检查资源覆盖、缺少前置和缺失文件。"
         "右侧可核对判定依据、忽略提醒、恢复已忽略项或重新检查文件。"),
    )),
    ("方案与存档", (
        ("新建、复制和管理方案", "文件 → 新建方案… / 复制当前方案… / 管理方案…",
         "预览、重命名或删除已保存方案，左上方下拉框可切换当前方案。"),
        ("保存、撤销和历史", "主界面 → 保存方案 · 编辑 → 撤销或重做 · 工作区 → 历史",
         "保存当前计划，使用撤销恢复最近的编辑，或从历史快照比较和载入旧方案。"),
        ("导入、导出和分享", "文件 → 导入方案… / 导出方案… / 生成分享包… / 导入分享包…",
         "导入前预览方案与匹配情况。分享包附带 Mod 清单，接收方可按工坊 ID 匹配并为本地模组手动定位。"),
        ("从现有存档导入", "应用到存档… → 选择档位 → 从档位新建方案",
         "读取该档的启用顺序。也可以手动打开存档文件，或刷新档位以读取游戏最新写入的存档。"),
        ("档位绑定和差异", "应用到存档… → 绑定当前方案",
         "记住档位对应的方案，之后可比较缺项和移位，或切换到已绑定方案。"),
        ("应用计划、备份与恢复", "应用到存档… → 选择档位 → 预览应用… · 备份与恢复",
         "受支持的存档可在游戏退出后备份并应用顺序。在「备份与恢复」窗口选择备份还原。"),
        ("带方案开新档", "主界面 → 带方案开新档…",
         "用当前方案启动游戏，在主菜单选择新档。首次进入战役时应用计划。"),
        ("Steam 启动项和手动参数", "带方案开新档… → 展开「启动项」",
         "Steam 版默认沿用当前账号的游戏启动项，也可切换手动输入。"
         "核对预览后启动，手动参数会按游戏安装保存。"),
    )),
    ("AI 助手与翻译", (
        ("接入设置和提示词", "AI → 接入设置与提示词…",
         "配置模型、兼容接口和 API Key，测试连接并查看内置提示词。"),
        ("提问与日志分析", "AI → 打开 AI 助手 / AI 整理日志…",
         "按当前方案查询模组和文件，也能附上日志。发送预览可核对共享给接口的资料。"),
        ("AI 排序", "AI → AI 排序…",
         "给出排序偏好，查看建议。点击「核对排序」后可把检查过的顺序应用到方案草稿。"),
        ("生成别名与中文简介", "左侧 Mod 详情 → AI 生成 · AI → 批量生成别名与翻译",
         "单项生成后可编辑再保存。批量模式补齐缺项，可在 AI 菜单重新打开进度。"),
    )),
    ("界面与入口", (
        ("切换常用和详细右键选项", "Mod 右键 → 显示详细选项 / 返回常用选项",
         "默认打开详细菜单。动作位于计划与分组、模组信息、创意工坊、文件与扫描。"),
        ("自定义右键菜单", "视图 → 右键菜单设置…",
         "设置默认打开常用或详细选项。可分别给可用、计划和分组成员勾选动作并拖动排序。"),
        ("界面主题与四栏宽度", "视图 → 界面主题… · 四栏之间的分隔线",
         "选择主题，拖动分隔线可调整或收起各栏，也能从「视图」重新显示。"),
        ("键盘快捷键", "帮助 → 键盘快捷键",
         "查看搜索、撤销、保存、多选和键盘打开右键菜单的方式。"),
        ("本机数据目录", "工具 → 打开数据目录",
         "查看方案、历史快照、存档备份、游戏替换包与日志所在位置。"),
    )),
)


class FeatureGuideDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("功能与入口")
        self.resize(850, 645)
        layout = QVBoxLayout(self)
        self.query = QLineEdit(placeholderText="搜索功能或入口，例如 前置、回收站、存档")
        self.query.setAccessibleName("搜索项目功能与入口")
        self.query.setClearButtonEnabled(True)
        layout.addWidget(self.query)

        self.tree = QTreeWidget(objectName="featureGuide")
        self.tree.setAccessibleName("DarkSort 功能与入口")
        self.tree.setHeaderLabels(("功能", "入口"))
        self.tree.setUniformRowHeights(True)
        self.tree.header().setMinimumSectionSize(185)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        self.path = QLabel(objectName="section")
        self.description = QLabel(objectName="muted")
        for label in (self.path, self.description):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        buttons = QDialogButtonBox()
        buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.tree.currentItemChanged.connect(self.show_item)
        self.query.textChanged.connect(self.refresh)
        self.refresh("")
        self.query.setFocus()

    def refresh(self, query):
        tokens = search_text(query).split()
        first = None
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for category, rows in FEATURES:
                matches = [
                    (name, path, description) for name, path, description in rows
                    if all(token in search_text(f"{category} {name} {path} {description}") for token in tokens)
                ]
                if not matches:
                    continue
                section = QTreeWidgetItem(self.tree, [category])
                section.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = section.font(0)
                font.setBold(True)
                section.setFont(0, font)
                for name, path, description in matches:
                    item = QTreeWidgetItem(section, [name, path])
                    item.setData(0, ROLE, description)
                    item.setToolTip(1, path)
                    if first is None:
                        first = item
                section.setExpanded(True)
            self.tree.setCurrentItem(first)
        self.show_item(first)

    def show_item(self, item, _previous=None):
        self.path.setText(item.text(1) if item else "")
        self.description.setText(item.data(0, ROLE) if item else "没有匹配的功能或入口。")
