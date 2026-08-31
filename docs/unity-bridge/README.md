# Unity Bridge 维护入口

本目录面向维护 Unity Bridge 的开发者和开发 Agent。这里记录当前实现、操作约束、验证方法和已经沉淀的设计结论；普通用户功能说明仍以项目主文档和 `assets/interface.json` 为准。

## 接手工作

开始修改前先确认工作区和离线基线：

```powershell
git status --short
uv run python -m unittest discover -v
uv run agent/run_batch_tasks.py --config config.daily-quick.toml --dry-run
```

工作区可能含有其他人的未提交修改。保留无关改动，不要使用 `git reset --hard`、`git checkout --` 等命令覆盖现场。

随后按任务类型阅读：

| 任务 | 文档 |
| --- | --- |
| 运行单任务、维护 TOML、检查批处理或 Ctrl+C | [运行与配置](runtime-and-config.md) |
| 检查最终 node、设计最小复现、分析日志 | [测试与排障](testing-and-debugging.md) |
| 修改地图采集、卡带选择、快捷键或屏蔽规则 | [卡带采集](card-collection.md) |
| 修改控制器、资源加载方式或 pipeline 架构 | [架构与设计决策](architecture-and-decisions.md) |

## 能力与边界

MaaFramework 核心支持在同一进程中创建 Custom Controller，但当前 AgentServer 不能跨进程注册 Controller，ProjectInterface V2 的 `controller.type` 也没有 `Custom`。因此 MFAAvalonia 等 PI Client 无法通过 `interface.json` 启动该控制器；上游进展见 MaaFramework [#1271](https://github.com/MaaXYZ/MaaFramework/issues/1271) 和早期跟踪项 [#1119](https://github.com/MaaXYZ/MaaFramework/issues/1119)。Unity Bridge runner 采用独立入口，在自身进程中创建 `UnityWin32Controller`，再加载 MaaFW 资源并执行任务。

当前包含：

- 单任务、单 node、连接和坐标点击入口；
- TOML 驱动的串行批处理；
- `base → pc → bridge` 三层资源加载；
- Unity Bridge 专用的卡带快捷键和类别门控；
- 不连接游戏的 pipeline 回归测试，以及分级实机 node 实验台。

Bridge 专用行为只放在 `assets/resource/bridge`。普通 PC GUI 只使用 `base + pc`，不应受到 Bridge 键盘选卡方案影响。

## 代码地图

| 路径 | 职责 |
| --- | --- |
| `agent/run_unity_bridge.py` | 单任务入口、Bridge 自动发现、连接、资源加载、任务超时和停止 |
| `agent/run_batch_tasks.py` | TOML 批处理、任务预检、串行执行、摘要和全局中断 |
| `agent/utils/unity_bridge_config.py` | 单任务与批处理 TOML 的解析、路径归一化和严格校验 |
| `agent/controller/unity_win32_controller.py` | MaaFW Unity Bridge 控制器实现 |
| `agent/utils/unity_bridge.py` | Windows VK 与 Unity Key 等 Bridge 工具 |
| `assets/interface.json` | 正式任务、选项和 preset；批处理任务必须在这里登记 |
| `assets/resource/bridge` | Unity Bridge 专用 pipeline 覆盖 |
| `scripts/unity_bridge_node_lab.py` | 查看或实机执行最终合并 node |
| `tests/test_unity_bridge_config.py` | 配置、资源顺序和 Ctrl+C 回归 |
| `tests/test_unity_bridge_client.py` | Bridge 文件协议、超时和失败诊断回归 |
| `tests/test_unity_bridge_pipeline.py` | 卡带快捷键、范围和剧情入口的离线契约 |
| `tests/test_steal_avail.py` | 偷窃卡片检测、冷却过滤和混合砍价卡跳过逻辑 |

## 不变量

维护时必须保持以下约束：

1. 生产资源加载顺序是 `assets/resource/base → pc → bridge`；后加载的同名 node 按字段覆盖前层。
2. 判断一个 node 时必须检查最终合并结果，不能只读取某一份 JSON。
3. Bridge 专用修复不得写入共享 base，也不得改变普通 PC GUI 的行为。
4. 单任务允许直接执行未登记的 pipeline entry；批处理只接受 `interface.json` 中登记的任务。
5. `Ctrl+C` 必须停止当前 MaaFW task 和整个批处理，不能被 `continue_on_error` 吞掉。
6. 会点击、滑动、发送按键或运行生产后继链的测试属于实机操作，执行前必须明确范围。
7. `.tmp` 只存放机器现场和临时实验。任何需要下次会话依赖的结论都必须写入本目录或正式测试。

## 文档维护规则

- 当前行为只在一个文档中定义，其他位置使用链接，不复制整段说明。
- 命令示例统一从仓库根目录使用 `uv run` 执行。
- 测试命令明确标注是否连接或操作游戏。
- 不记录会快速过期的工作区文件清单、测试通过数量或某次提交前状态。
- 故障复盘保留可复用的“现象、根因、当前实现、回归点”，不保留调查流水账。
- 代码、选项或 node 语义变化时，同一提交更新对应文档和回归测试。

## 提交边界

- 通用修复按可独立向上游提交 PR 的标准整理：不得依赖 PC/Unity Bridge 资源、runner 或自用配置；相关测试和文档也必须能在上游独立使用。
- `assets/interface.json`、`assets/resource/base` 的跨平台行为通常属于通用修复；`assets/resource/pc`、`assets/resource/bridge` 和 Bridge runner 通常属于自用修改，不混入上游提交。
- 同一故障涉及两层时，先提交可独立工作的通用修复，再提交 PC/Bridge 适配；后者可以依赖前者，反向不行。
- 提交前用 `git diff --cached --name-only` 检查边界；通用提交使用 `fix:`，自用适配使用 `fix(PC):` 等明确作用域。
