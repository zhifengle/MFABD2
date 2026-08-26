# Unity Bridge 卡带采集

本文是地图采集、卡带选择、屏蔽规则和相关故障的权威维护说明。通用 runner 用法见[运行与配置](runtime-and-config.md)，分级测试方法见[测试与排障](testing-and-debugging.md)。

## 维护范围

主要资源：

| 路径 | 作用 |
| --- | --- |
| `assets/interface.json` | `[完整]`、`[单章]` 任务和卡带屏蔽选项 |
| `assets/resource/base/pipeline/Collect_Launcher.json` | 跨渠道的卡带调度基础流程 |
| `assets/resource/pc/pipeline/Collect_Launcher.json` | PC 页签 ROI 和阈值覆盖 |
| `assets/resource/pc/pipeline/Collect_Navigation.json` | PC 地图导航覆盖 |
| `assets/resource/pc/pipeline/Collect_Story14.json` | 剧情14左右回廊的 PC 专用步行模块 |
| `assets/resource/bridge/pipeline/Collect_Launcher.json` | Bridge 快捷键、类别门控和出口 |
| `tests/test_unity_bridge_pipeline.py` | 快捷键、范围和剧情入口的离线契约 |

修改时必须检查 `base → pc → bridge` 的最终合并结果。

## 可采集范围与快捷键

| 类别 | 可采集编号 | Bridge 快捷键 |
| --- | --- | --- |
| 剧情 | `1–19` | `1–9` 对应 1–9，`0` 对应 10，`F1–F9` 对应 11–19 |
| 角色 | `1–7` | 主键盘 `1–7` |
| 活动 | `1–5` | 主键盘 `1–5` |

剧情 20 和角色 8 没有采集内容，必须保持 `enabled = false`。游戏中存在卡带或可用快捷键，不代表该卡带应该进入地图采集。

Windows VK 映射：

- 主键盘 `0–9`：`48–57`；
- `F1–F9`：`112–120`。

## 回到探索告示板

PC 通用入口为 `Collect_ReturnToExplorationBoard`。它先复用已实机校准的 `Nvi_SandGuideButt_PC.png` 定位小地图旁的寻路按钮；列表展开后，在左侧完整下拉区域内 OCR 查找“探索告示板”并点击识别框中心，因此不依赖选项数量或固定纵坐标。流程兼容“自动移动”确认框、移动中状态和加载状态，抵达后通过可选锚点 `ExplorationBoard_Arrived` 交还控制权；调用方未设置锚点时正常结束。

该节点是 PC 坐标流程，只定义在 `assets/resource/pc/pipeline/Collect_Navigation.json`。实机分级验证先运行 `Collect_ReturnToExplorationBoard_Open` 的 recognition 模式确认按钮模板，再运行入口的 flow 模式；后者会实际点击并移动角色。

## `[完整]` 与 `[单章]`

| 任务 | entry | 语义 |
| --- | --- | --- |
| `[执行]地图采集[完整]` | `Collect_StartGame_HomePage` | 按正常类别和卡带顺序执行全部符合条件的采集 |
| `[执行]地图采集[单章]` | `Collect_StartGame_HomePage_OnlyOnce` | 正常调度中首张符合条件的卡带完成后停止，以满足日常需求 |

`[单章]` 不提供“类别 + 章节编号”定位参数，不能把名称理解成指定第 N 章。排查一张指定卡带时，使用 `[完整]` 并屏蔽其他所有可采集卡带。

## 屏蔽规则

只有先设置：

```text
采集卡带屏蔽=Yes
```

三个子选项才会写入 `Collect_BlackList_User.attach`：

- `故事卡带屏蔽`
- `角色卡带屏蔽`
- `活动卡带屏蔽`

值表示“跳过的编号”，不是目标编号。支持逗号和区间，例如 `1,3-5`、`1,3~5`。指定目标时只列出目标之外的可采集编号。

| 目标 | 故事卡带屏蔽 | 角色卡带屏蔽 | 活动卡带屏蔽 |
| --- | --- | --- | --- |
| 剧情 N，`N=1–19` | `1–19` 排除 N | `1-7` | `1-5` |
| 角色 N，`N=1–7` | `1-19` | `1–7` 排除 N | `1-5` |
| 活动 N，`N=1–5` | `1-19` | `1-7` | `1–5` 排除 N |

### 指定单一卡带

以剧情 9 为例：

```powershell
uv run agent/run_unity_bridge.py `
  --task "[执行]地图采集[完整]" `
  --option "采集卡带屏蔽=Yes" `
  --option "故事卡带屏蔽=1-8,10-19" `
  --option "角色卡带屏蔽=1-7" `
  --option "活动卡带屏蔽=1-5" `
  --account unity-bridge-story9-diagnostic `
  --timeout 1200
```

以角色 4 为例：

```powershell
uv run agent/run_unity_bridge.py `
  --task "[执行]地图采集[完整]" `
  --option "采集卡带屏蔽=Yes" `
  --option "故事卡带屏蔽=1-19" `
  --option "角色卡带屏蔽=1-3,5-7" `
  --option "活动卡带屏蔽=1-5" `
  --account unity-bridge-character4-diagnostic `
  --timeout 1200
```

以活动 3 为例：

```powershell
uv run agent/run_unity_bridge.py `
  --task "[执行]地图采集[完整]" `
  --option "采集卡带屏蔽=Yes" `
  --option "故事卡带屏蔽=1-19" `
  --option "角色卡带屏蔽=1-7" `
  --option "活动卡带屏蔽=1-2,4-5" `
  --account unity-bridge-event3-diagnostic `
  --timeout 1200
```

这些命令会真实切换页签、发送快捷键和执行采集。`--account` 只隔离自动化 PersistentStore，不会切换游戏账号。完整任务仍可能切换其他类别，并执行 `Collect_FirstBootPack_To1` 等初始化探测，因此不能仅凭画面判断它运行了哪些正式卡带。

日志验收必须确认：

1. 只有目标 `Collect_Pack_<类别>_<编号>` 进入正式卡带路径；
2. 命中目标对应的 `Collect_Pack_KeyClick_*`；
3. 目标地图、技能和完成标记按预期执行；
4. 没有其他正式卡带 node 或错误的类别完成记录。

## 页签状态与快捷键路由

PC 卡带页是一行横向布局。大幅 swipe 难以稳定标定，而且用户或流程从剧情切到角色再切回剧情时，游戏会自动重置剧情列表的横向位置。这是 swipe 状态不可靠的现象，不是进入剧情前应主动执行的复位步骤。

Bridge 使用快捷键直选卡带，不依赖当前横向位置，也不会为了复位而额外切换角色页签。剧情入口保持 base 的直接选择链：

```text
Collect_FeatureSwitch_StoryPack
  → Collect_QuickCart_SelectType_StoryPack
  → Collect_QuickCart_StoryPack_OcrCheck
  → Collect_Loc_ClearNonStoryTag
```

卡带判定 node 的 `post_delay` 为 0，等待放在快捷键 node。按键后依次确认加载页或箱庭；均未命中时重试当前快捷键，不能落到下一张卡。

类别走到可视页尾后，`Collect_Loc_OutToSwip_Hub_Egress` 进入 `[Anchor]SmartSwipOut`，复用 base 的类别完成出口。不要回跳共享路由 `Collect_PackLocation_PassFieldsAndHub`，那里没有新的槽位定位上下文，而且会绕过类别完成语义。

## 剧情类别门控

共享路由 `Collect_PackLocation_PassFieldsAndHub` 会在各类别尝试剧情、角色和活动 node。剧情 node 不能只判断 `CheckCoolDown`，否则角色或活动页可能命中同序号剧情并错误写入剧情完成记录。

Bridge 使用 `NonStoryPackActive` tag：

- 剧情页 OCR 成功后由 `Collect_Loc_ClearNonStoryTag` 清零；
- 角色或活动页 OCR 成功后由 `Collect_Loc_SetNonStoryTag` 置为非零；
- `Collect_StoryPack_IsActive` 使用正向 `CheckTag(max=1)`；
- 每个剧情 node 同时要求 `Collect_StoryPack_IsActive` 和 `CheckCoolDown`。

tag 默认 0 是剧情允许态，但生产类别选择链必须在路由前刷新它。不要在被 `all_of` 按名称引用的识别 node 顶层加入 `inverse: true`；MaaFW 实机行为不会按这里需要的方式应用该 inverse。

## 已知故障与当前实现

### 活动卡带页签死循环

**现象：** `Collect_QuickCart_SelectType_EventPack` 后不断自环，`Collect_QuickCart_EventPack_OcrCheck` 无法成功。

**根因：** 确认 node 的字符串 ROI 收缩到前一步 OCR 命中的“活动游戏卡”文字框；PC 选中态实测亮色像素约 383，base 的 `count = 500` 无法命中。

**当前实现：** `assets/resource/pc/pipeline/Collect_Launcher.json` 将 count 覆盖为 300。

**验证方式：** 使用 node lab recognition 或实机任务验证选中态。Unity Bridge 单元测试不锁定具体颜色阈值。

### 剧情 9 战斗 2 被跳过

**现象：** 从战斗 1 右移时偶发连续点击两次，直接越过战斗 2。

**根因：** 两个地图名只相差一个罗马数字 `I`，名称 ROI 实测帧差约 1.48，低于 SmartAction 默认阈值 3.0，首次点击被误判为画面未变化并补点。

**当前实现：** PC 的 `Collect_MapRight` 无论帧差是否超过阈值都只走一次点击结果，`unchanged_streak = 1`；若点击实际未生效，后续地图识别仍停在当前状态并可重新进入采集链。

**验证方式：** 这是通用 PC 地图采集行为，不属于 Unity Bridge 单元测试。实机验收检查目标地图、`Collect_Skill3_2` 和剧情完成标记；若补自动化回归，应放入通用地图采集测试。

### 剧情 14 漏采无传送阵回廊

**现象：** 阿尔卡迪亚居住区域包含剑之神殿左侧回廊与中央回廊；截图右边的按钮是中央回廊，该区域没有传送阵，通用传送阵导航会漏采。

**根因：** 按键传送进入卡带时，起点传送图标可能在区域地图上覆盖战斗区域图标；同时角色当前位置也可能让头像遮住导航地图中左侧回廊的一部分可点击区域。直接从不稳定落点选图会点中错误对象，或无法打开“自动移动”确认。

**当前实现：** 剧情 14 使用探索告示板作为两次地图导航的稳定起点，不再使用按键传送定位，也不改写 `TargetMap2/3`。完整顺序为：进入后回探索告示板并采集告示板所在区域；展开地图步行到左侧回廊并采集；再次回探索告示板；再展开地图步行到中央回廊并完成最后一次采集。两次回告示板复用 PC 通用入口 `Collect_ReturnToExplorationBoard`，两次地图步行沿用“自动移动”确认、移动中和加载识别。技能出口和告示板抵达出口分别通过 `OnFoot_Skill`、`ExplorationBoard_Arrived` 锚点串联，最后进入通用步行模块收尾并记录卡带完成。

**流程收尾与卡带循环：** 中央回廊最后一次采集后，`Collect_IM_Closure` 打完成标记、`Collect_IM_Reset` 还原环境；随后剧情14专用回归节点 `Collect_Story14_AfterReset` 调出快速卡带菜单，交还卡带循环继续后续卡带（与通用剧情收尾一致，不再单卡即止）。回归出口由 `Collect_IM_Reset` 的 PC 覆盖 + `IM_Reset_Next` 锚点门控：剧情14 在 `Collect_Story14_Init` 注册该锚、在回归节点处清锚；其他步行流程未注册该锚，`Collect_IM_Reset` 走 `Collect_IM_Reset_End` 安静终点，保持原有收尾语义不变。

**坐标边界：** 左右入口坐标来自 PC 1280×720 管线画面的实机测量，仅写入 `assets/resource/pc`，不作为安卓/base 坐标使用。

**验证方式：** 离线回归锁定两次告示板归位、三段技能出口、PC 地图落点、移动确认链、公共导航零剧情专属引用以及完成标记，并禁止剧情 14 引用 `Collect_ForceTeleporCircle` 和 `Collect_OperationsMain_Sandplay`。实机验收顺序必须是“告示板采集 → 左侧采集 → 回告示板 → 中央采集”，最后出现 `Story_14_神圣审判` 完成标记，随后应回到快速卡带菜单并继续采集剩余待完成卡带（不再单卡即止）。

### `Collect_MapLift` 返回 false

**现象：** Bridge 报 `status=no-click-handler`，随后 node 重试。

**根因：** base 的 `repeat = 7` 会连续点击区域地图左箭头。实际到达最左端后，剩余 Raycast 仍命中对象，但对象及父级没有 `IPointerClickHandler`，严格点击语义返回 false。

**当前实现：** 保留 base 行为。重试通常能识别已经处于最左端并继续，没有证据表明这是 Bridge 线程或文件轮询故障。

**处理边界：** 不要全局吞掉 `no-click-handler`。只有确认该模式导致任务失败、明显延迟或错误回退时，才考虑局部 pipeline 修改。

## 回归入口

完全离线：

```powershell
uv run python -m unittest tests.test_unity_bridge_pipeline -v
```

临时诊断覆盖见[临时 override](testing-and-debugging.md#临时-override)。指定卡带端到端测试属于实机操作，修复前后应使用不同的诊断 account，避免旧周期标记掩盖结果。
