# Unity Bridge 测试与排障

本文提供从完全离线到生产 flow 的分级测试方法。卡带范围和指定卡带命令见[卡带采集](card-collection.md)。

## 安全级别

| 级别 | 方式 | 连接游戏 | 操作游戏 |
| --- | --- | --- | --- |
| A | unittest、编译、dry-run、node `inspect` | 否 | 否 |
| B | node `recognition` | 是 | 否；目标 action 被替换为 `DoNothing` |
| C | node `isolated` | 是 | 是；执行目标动作但切断后继 |
| D | node `flow`、正式 runner | 是 | 是；执行后继链或完整任务 |

截图和 recognition 虽不发送点击或按键，仍会读取当前游戏画面。`isolated`、`flow`、`--click` 和正式任务必须按实机操作对待。

## A：完全离线验证

```powershell
uv run python -m unittest discover -v
uv run python -m py_compile `
  agent/run_unity_bridge.py `
  agent/run_batch_tasks.py `
  agent/utils/unity_bridge_config.py `
  scripts/unity_bridge_node_lab.py
uv run agent/run_batch_tasks.py `
  --config config.daily-quick.toml `
  --dry-run
```

修改范围较小时可先运行定向回归：

```powershell
uv run python -m unittest tests.test_unity_bridge_config -v
uv run python -m unittest tests.test_unity_bridge_pipeline -v
```

### 自动化测试范围

Unity Bridge 测试只保护本项目在 Bridge 边界上拥有的行为：

| 测试文件 | 纳入范围 |
| --- | --- |
| `test_unity_bridge_client.py` | Bridge 文件请求/响应、超时清理和失败诊断 |
| `test_unity_bridge_config.py` | runner 配置、CLI/TOML 合并、资源加载顺序、批处理与 Ctrl+C |
| `test_unity_bridge_pipeline.py` | Bridge 卡带快捷键、可采集范围，以及剧情入口不主动切换页签 |

以下内容不在这套单元测试中重复验证：

- MaaFW 的 tag、anchor、pipeline 调度、资源合并算法和通用 Custom Action 语义；
- `base`、`pc` 中与 Bridge 无关的通用地图采集行为；
- ROI、颜色阈值、滑动距离和等待时间等画面校准参数；
- Unity 中按键是否被接收、识别是否命中和完整采集是否成功。

前三类应由其所属模块或 MaaFW 自身测试负责；最后两类使用 node lab 和实机日志验证。pipeline 离线测试可以加载最终 `base → pc → bridge` 资源，但不因此承担 MaaFW 框架语义的测试责任。

### 查看最终合并 node

`inspect` 按生产顺序合并 `base → pc → bridge`，但不连接游戏：

```powershell
uv run scripts/unity_bridge_node_lab.py `
  Collect_QuickCart_EventPack_OcrCheck `
  --mode inspect
```

排查覆盖问题时以该结果为准。只查看 `assets/resource/bridge` 无法判断 base 或 pc 中仍然生效的字段。

## B：只做 recognition

默认模式连接游戏并识别目标 node，同时将其 action 改为 `DoNothing`，清空 `next/on_error`：

```powershell
uv run scripts/unity_bridge_node_lab.py Collect_QuickCart_EventPack_OcrCheck
```

适合验证 ROI、模板、OCR、颜色阈值或自定义识别。它不适合验证点击是否命中、按键映射或后继链路。

## C：隔离动作

`isolated` 执行目标 node 的真实 action，但清空后继：

```powershell
uv run scripts/unity_bridge_node_lab.py NODE --mode isolated
```

使用前将游戏置于可重复、可确认的页面。若 action 是快捷键、滑动或自定义动作，这条命令会真实改变游戏状态。

## D：完整 flow

`flow` 保留生产后继链：

```powershell
uv run scripts/unity_bridge_node_lab.py NODE --mode flow --timeout 10
```

只在入口状态、测试范围和停止条件明确时使用。正式任务通过 `agent/run_unity_bridge.py` 或 `agent/run_batch_tasks.py` 执行。

## 临时 override

实验台可在生产三层资源之后追加一个 JSON override：

```powershell
uv run scripts/unity_bridge_node_lab.py NODE `
  --mode flow `
  --override-file .tmp/unity-bridge-node-override.json `
  --timeout 10
```

实验性 override 保存在 `.tmp`。只有当它保护的是 Unity Bridge 自身契约、可离线重复且需要长期回归时，才迁入 `tests`。MaaFW 的 tag、anchor、pipeline 调度和通用 Custom Action 不在 Unity Bridge 测试中重复验证。

override 是否安全取决于其当前内容；执行前检查它是否覆盖测试路径上的真实点击、按键和生产出口，不能只根据文件名判断。

## 产物与日志

node 实验台默认将测试记录写入：

```text
.tmp/unity_bridge_artifacts/<timestamp>_<node>/
```

可用 `--artifact-root` 改目录，或用 `--no-artifacts` 禁止保存。正式 runner 的 MaaFW 日志通常写入 `debug/maafw.log`；Bridge 插件侧请求和 Raycast 细节应结合游戏的 BepInEx 日志检查。

`.tmp` 中适合保留：

- 截图、事件 JSON、实机结果和机器现场；
- 临时探针和一次性分析脚本；
- 用于当前调查、但尚未稳定的假设。

应迁移进版本管理：

- 可复用且不依赖机器状态的测试工具；
- 安全、稳定的 JSON fixture；
- 最终合并 pipeline 中由 Unity Bridge 拥有的离线契约；
- 已验证且下次维护仍需要的结论。

不要取消 `.tmp` 的忽略规则，也不要把整个目录强制加入 Git。

## 排障顺序

1. 用 `git status --short` 确认现场，记录相关提交和资源层。
2. 用 `--mode inspect` 确认最终 node，而不是猜测覆盖是否生效。
3. 将问题缩小到 recognition、单一 action、局部 flow 或完整任务中的一层。
4. recognition 问题检查 ROI、阈值、模板和当前页面；action 问题检查 Bridge 请求与插件响应；路由问题检查 `next/on_error`、tag、冷却和完成标记。
5. 用专门的诊断 `--account` 或 `--account-id` 隔离 PersistentStore 周期记录。
6. 对比 MaaFW node 日志与 BepInEx Bridge 请求日志，确认失败发生在识别、动作、等待还是后继路由。
7. 修复后先添加离线断言，再按最低必要安全级别进行实机验证。

## 日志判断

不要只根据游戏画面、最后一条报错或任务最终退出码判断路径。至少确认：

- 实际进入的 pipeline node 名；
- recognition 是否命中，action 是否成功；
- `next`、`on_error` 或重试走向；
- 最后进入的 node 和停止原因；
- 是否写入了预期的 PersistentStore 完成标记；
- Bridge 失败时的请求 ID、action、参数、耗时、状态和完整响应。

对于指定卡带测试，还必须核对 `Collect_Pack_<类别>_<编号>`、对应快捷键 node 和完成标记，详见[指定单一卡带](card-collection.md#指定单一卡带)。

## 变更验收

提交前至少执行：

```powershell
uv run python -m unittest discover -v
uv run agent/run_batch_tasks.py --config config.daily-quick.toml --dry-run
git diff --check
```

若改动会影响真实 UI，离线通过不能代替实机验证；交付时明确说明实际运行了哪个级别、哪些实机路径尚未执行。
