# DarkSort 架构

## 本地方案和扫描

- `models.py` 定义安装实例、方案条目、分组锚点与用户排序规则。安装实例按来源和实际路径识别。Workshop 逻辑标识独立于安装路径。
- `scanning/` 负责 Steam 多库发现、DD1 元数据、资源路径摘要和 SQLite 缓存。资源摘要优先读取清单，旧版 Mod 从常见资源目录采集。清单更新会使缓存失效。
- `profiles.py` 读取 v1 至 v6。v2 保存稳定条目 ID，v3 保存联锁，v4 保存分组和动态规则，v5 保存本地 Mod 的便携标识，v6 支持联锁分组。读取旧暂存时按顺序迁为分组，分组 ID 可重复生成。缺失和歧义条目保留在方案中。
- `sharing.py` 生成含便携方案和工坊清单的 ZIP，以及独立分组模板。移除安装路径时同时映射对应的分组成员与排序规则。导入时读取清单并交由方案解析器校验。
- `history.py` 保存独立的完整方案快照。排序和恢复前先记录工作状态，保存方案前记录上个版本。比对先用稳定条目 ID 对齐，回退到逻辑标识，再根据最长递增子序列区分最少的移位项与顺延项。
- `tags.py` 保存自定义标签，Workshop 标签随逻辑 ID 跨库匹配。作者标签来自元数据。

## 顺序、搜索和冲突

`classification.py` 将资源摘要、Mod 标题和标签归入八类。手动分类按 Mod 逻辑标识存入设置。仅需要资源路径的检测在扫描线程完成。

`sorting.py` 的 Kahn 就绪队列使用分类优先级和当前行号。联锁在图中作为不可拆分的单元，组内反向规则和要求在组内插入外项的循环会阻止排序。皮肤匹配唯一人物提供者时加入连边。未定位或待归类条目优先保留原位置，为避免拆散联锁发生顺延时会提示。

`ui/` 用 Qt Widgets 实现可折叠的四栏工作台。右侧分组支持单个组员和整组拖动。`locks.py` 为拖放、选区和上下移动提供联锁边界。`dependencies.py` 在启用时补齐传递前置，应用到存档和开新档前再次校验。

草稿编辑通过 `QUndoStack` 应用完整的方案状态。保存基线随保存更新，用于判断撤销或重做后的草稿是否有未保存更改。

`search.py` 规范化文本并按 Workshop ID、名称和其他元数据排列全局搜索结果。搜索与关联使用同一份位置标识，可用库按安装键定位，计划按条目 ID 定位，待匹配组员按分组与组员 ID 定位。`relations.py` 汇总规则和人物资源关系。`ui/mod_list.py` 在计划中绘制直接连线。

`conflicts.py` 按需枚举已启用计划中的安装目录，索引 Mod 相对路径和清单。它报告缺失、清单未知、内容相同及提供者优先级。已知普通英雄文件的候选覆盖按顶部优先计算。已识别的叠加资源同时展示各提供者。其余资源保留同路径诊断。规则参考 DD1 编辑器的资源清单和覆盖说明。[DD1 内容与存档规则](https://github.com/BzhChara/darkest_dungeon_save_editor/blob/4497a1e2250f6d5b22d9b0219949dede991b50c2/docs/content-save-rules.md)

## AI 接入与模型结果

`ai_client.py` 使用 Chat Completions 文本接口。接入参数单独保存。可选密钥由 Windows DPAPI 按用户和接口地址保护。`ui/ai_dialogs.py` 在后台发送显式请求，预览日志摘录和 Mod 数据，并在 Qt 主线程展示结果。[OpenAI Docs](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) · [Microsoft DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)

`ai_tasks.py` 提供两套内置提示词、日志脱敏以及排序建议校验。发送模型的是当前方案的临时编号和选定元数据。模型返回的顺序为拓扑排序提供优先偏好，用户规则、联锁、皮肤关系和预留位置仍由本地算法处理。结果通过现有预览进入方案草稿。

## 按档位处理存档

`adapters/campaigns.py` 识别支持 `-mod_profile` 的 Windows 游戏程序，复用安装来源与标题校验，将当前计划按顺序导出为游戏原生的 version 1 清单。游戏在本次启动后首次进入战役时读取清单，并由自己的流程初始化档位。中文路径使用游戏工作目录下的相对清单路径。

`adapters/launch_options.py` 只读提取当前 Steam 账号的 DD1 启动项，并用 Windows 参数规则解析预览与实际启动。手动参数按游戏安装保存在设置中。`campaigns.py` 将解析后的附加参数与唯一的方案清单交给游戏进程。

`adapters/dson.py` 从 DSON 表重建字段结构，按数字键读取 `base_root.applied_ugcs_1_0`。写入时只替换该对象的条目。编码会保留非目标字段的名称、数据、层级和标志，重新计算对象表、字段表及必要的字节对齐。准备写入前验证原档逐字节往返，候选再次解码后对照非目标字段。验证过的原生写入范围是 Windows build 28385 的支持结构。[DSON 布局参考](https://github.com/robojumper/DarkestDungeonSaveEditor/blob/master/docs/dson.md)

`adapters/saves.py` 发现档位并解析 Workshop ID 和本地 Mod 标题。执行前确认游戏进程状态和目标原始字节，保存原件和 SHA-256 清单，再用 Windows `ReplaceFileW` 替换同目录候选文件。提交后校验目标和被替换文件。直接恢复要求当前哈希对应这次应用，且原始备份通过校验。需要继续调查的外部版本保留为独立文件。[Windows ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)

`save_bindings.py` 将规范化的档位路径和方案 UUID 写入应用数据目录。差异按 Workshop ID 或本地 `project.xml` 标题比对，区分相对移位和无法确认的身份。档位绑定支持只读预览，实际应用仍走原有 DSON 验证、预览与备份流程。
