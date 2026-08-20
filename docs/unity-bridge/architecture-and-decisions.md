# Unity Bridge 架构与设计决策

本文记录 Unity Bridge 的组件关系、稳定设计约束以及不能从代码表面直接推导出的取舍。具体卡带流程见[卡带采集](card-collection.md)。

## 运行链路

```text
run_unity_bridge.py / run_batch_tasks.py / unity_bridge_node_lab.py
  → 解析 CLI、TOML 和 interface 选项
  → 发现 UnityAutomationBridge 插件目录
  → 创建 UnityWin32Controller 并连接游戏
  → 注册 standalone 自定义动作与识别器
  → 加载 base、pc、bridge 资源
  → 创建并绑定 MaaFW Tasker
  → 执行 pipeline，记录 node 进度、超时和停止状态
```

MaaFramework 核心提供同进程 Custom Controller API；当前缺口位于跨进程 AgentServer 和 ProjectInterface V2 集成层，而不是 Custom Controller 核心能力本身。PI V2 尚无 `Custom` controller type，Agent 子进程也不能注册 Controller，相关上游设计见 MaaFramework [#1271](https://github.com/MaaXYZ/MaaFramework/issues/1271) 和 [#1119](https://github.com/MaaXYZ/MaaFramework/issues/1119)。

Unity Bridge 因此采用独立 runner 创建 Controller，不是 MFAAvalonia PI Client 中的普通 PC Controller。Bridge pipeline 覆盖只应由这些 runner 加载。

## 组件职责

### Controller

`agent/controller/unity_win32_controller.py` 把 MaaFW 的截图、点击、滑动和按键请求转换为 Unity Automation Bridge 请求。`agent/utils/unity_bridge.py` 维护按键映射等共享工具。

控制器失败日志应保留请求 ID、action、参数、耗时、请求状态和完整响应，使客户端超时、插件拒绝与 Unity 命中失败可以区分。

### Runner

`agent/run_unity_bridge.py` 是运行时能力的唯一基础实现，负责：

- 加载 interface、解析正式任务或直接 entry；
- 自动发现 Bridge；
- 确保 OCR 模型；
- 加载资源和注册扩展；
- 创建 Tasker；
- 轮询任务、处理超时和 Ctrl+C。

`agent/run_batch_tasks.py` 复用上述函数，在同一 Controller、Resource 和 Tasker 上串行执行正式任务。共享能力应优先留在单任务模块或独立 utility，避免两个入口出现不同的资源顺序和停止语义。

### 配置

`agent/utils/unity_bridge_config.py` 先把 TOML 转换为不可变 dataclass，再进入运行期。配置解析负责 schema 和路径；任务名、任务选项及 pipeline override 由 interface 解析负责。

argparse 对可配置字段使用 `None` 表示“CLI 未提供”。不能通过“参数值是否等于默认值”判断来源，否则用户显式传入 `--timeout 1200` 或 `--account 0` 时可能被 TOML 反向覆盖。

### PersistentStore

`account_id` 选择自动化的 `agent_save_data` 命名空间，用于冷却和完成标记。它不会切换游戏账号。实机复现周期性任务时使用专门的诊断 account，避免修改主账号的自动化记录。

## Pipeline 分层

| 层 | 允许内容 |
| --- | --- |
| `base` | 跨平台流程和通用语义 |
| `pc` | PC 布局、ROI、坐标、模板和阈值 |
| `bridge` | 仅 Unity Bridge 可用的快捷键、Bridge 路由和行为覆盖 |

后加载 bundle 对同名 node 做字段级覆盖，而不是自动替换整个 node。新增覆盖前应先确定需要改变的最小字段；删除字段或改变继承假设时，应通过 node lab 查看最终配置并增加回归断言。

## 设计决策

### Bridge 行为保持渠道隔离

键盘直选等能力依赖 Unity Automation Bridge，不能放进 base 或 pc。否则 MFAAvalonia 的普通 PC 渠道也会进入无法保证可用的按键路径。

### 卡带选择使用快捷键而不是横向 swipe

PC 横向卡带列表的 swipe 幅度不稳定；发生“剧情 → 角色 → 剧情”切换时，游戏还会自动重置剧情列表的横向位置，使依赖既有滚动位置的流程失效。Bridge 因此直接使用数字键和功能键直选卡带，不主动切换角色页签，也不依赖任何横向位置。

### 剧情使用正向类别门控

共享卡带路由会跨类别尝试 node，仅有冷却判断不足以证明当前处于剧情页。当前使用显式 tag 和正向 `CheckTag`，避免命名识别 node 上的顶层 inverse 在 MaaFW 组合识别中产生非预期结果。

### 类别出口复用 base 完成语义

Bridge 的快捷键已经覆盖可采集卡带，不需要大幅横向滑动，但类别结束仍需经过 base 的 `[Anchor]SmartSwipOut`。直接返回共享卡带路由会丢失新的槽位上下文并绕过完成标记。

### 快捷键失败时重试当前卡带

按键发送后需要等待加载页或箱庭确认。两个确认均失败时回到当前快捷键 node，避免一次瞬时加载失败被解释为“当前卡带不存在”，从而误选下一张。

### Bridge 保持严格点击失败

Raycast 命中对象不代表存在 click handler。`no-click-handler` 是有诊断价值的动作失败，不能在 Controller 层全局改成成功。已知的边界点击应由具体 pipeline 状态吸收，其他节点仍保留严格语义。

### 批处理只运行已登记任务

单任务 runner 允许直接 entry，便于开发 node；批处理是长期自动化入口，拼错名称若被当作 entry 会把配置错误推迟到运行期。因此批处理在连接前要求任务存在于 `interface.json`，并预检全部启用和禁用项。

### Ctrl+C 是全局终止

`continue_on_error` 只处理普通失败和超时。用户中断表示停止当前工作流，必须请求 MaaFW task 停止、跳过所有后续任务并返回 130。

## 已否决方案

以下方案已有反例，不应在缺少新证据和完整回归的情况下恢复：

1. 全局禁用 `Collect_LocatePackFrame_Smart_Swip`：这是多类别共享 node，会破坏其他卡带路径。
2. 只调整 swipe 幅度解决剧情 11–19：设备状态以及类别切换造成的横向位置重置使其不稳定。
3. 剧情 node 只使用 `CheckCoolDown`：会在角色或活动页跨类别串线。
4. 在被 `all_of` 按名称引用的识别 node 顶层使用 `inverse`：实机组合语义不符合预期。
5. Egress 直接跳回 `Collect_PackLocation_PassFieldsAndHub`：缺少新的定位上下文并跳过类别完成出口。
6. 用 NumPy 或 Pillow 近似 MaaFW 模板匹配作为最终判断：掩码和归一化差异可能给出与引擎相反的结果。
7. 用 35 秒的完整地图采集作为活动页签最小复现：流程可能在到达活动类别前超时。
8. 把 `[执行]地图采集[单章]` 当成按编号选章：它只在首张符合条件的卡带完成后停止。
9. 因已知的最左端重复点击而全局吞掉 `no-click-handler`：会隐藏其他真实点击目标错误。
10. 重新启用剧情 20 或角色 8：两者没有地图采集内容。

`.tmp/verify/s6_verify_bridge.py` 对应早期“全局禁滑、剧情仍点击”的方案，不能作为当前验收依据。

## 变更检查表

### 修改 Controller

- 区分传输失败、插件状态和 Unity 动作结果；
- 保留可关联的请求诊断字段；
- 回归截图、点击、滑动和按键映射；
- 确认严格失败语义没有被全局放宽。

### 修改 runner 或 TOML

- 单任务与批处理保持同一资源顺序；
- 检查 CLI/TOML 优先级和相对路径；
- 检查超时、Ctrl+C 和退出码；
- 运行 `tests.test_unity_bridge_config` 和 batch dry-run。

### 修改 pipeline

- 确认修复属于 base、pc 还是 bridge；
- 使用 node lab `inspect` 查看最终 node；
- 仅为 Unity Bridge 拥有的快捷键、范围和入口契约增加离线断言；
- 按最低必要安全级别进行实机验证；
- 更新[卡带采集](card-collection.md)或[测试与排障](testing-and-debugging.md)中的权威结论。
