# Unity Bridge 运行与配置

本文说明单任务 runner、批处理 runner、TOML schema 和进程终止语义。开始维护前先阅读[维护入口](README.md)。

## 前置条件

- 游戏已启动，窗口能被 `assets/interface.json` 中的 Win32 窗口规则发现；
- `unity-automation-bridge` 插件已安装到游戏的 `BepInEx/plugins/UnityAutomationBridge`；
- 项目依赖可通过 `uv run` 启动；
- Python 3.11 及以上使用标准库 `tomllib`，Python 3.10 使用项目依赖 `tomli`。

未指定 `--bridge-dir` 时，runner 会根据游戏窗口和进程自动发现插件目录。

## 资源加载

单任务、批处理和 node 实验台使用相同的生产顺序：

```text
assets/resource/base
  → assets/resource/pc
  → assets/resource/bridge（目录存在时）
```

`--resource-root` 指向包含 `interface.json` 和 `resource/` 的目录，默认是仓库中的 `assets/`。

## 单任务 runner

### 常用命令

运行正式任务：

```powershell
uv run agent/run_unity_bridge.py `
  --task "[执行]快速狩猎扫荡" `
  --timeout 600
```

任务参数可使用完整显示名、pipeline entry 或唯一关键词。单任务入口还允许直接传入未登记到 `interface.json` 的 pipeline entry，供开发调试。

查看任务和选项不会连接游戏：

```powershell
uv run agent/run_unity_bridge.py --list-tasks
uv run agent/run_unity_bridge.py --list-options "[执行]地图采集[完整]"
```

不传 `--task` 或 `--click` 时，runner 只连接控制器并截图，用于验证 Bridge 是否可用。坐标点击模式会真实操作游戏：

```powershell
uv run agent/run_unity_bridge.py --click 640 360 --after-click 1
```

### 单任务 TOML

```toml
resource_root = "./assets"

[bridge]
dir = "" # 留空时自动发现

[task]
name = "[执行]装备制作&炼制日常"
account_id = "0"
timeout = 1200.0

[[task.options]]
name = "精炼按自定义标志排序"
value = "Yes"

[[task.options]]
name = "仅精炼<17 或 <10"
value = "低于17阶"
```

运行：

```powershell
uv run agent/run_unity_bridge.py --config config.example.toml
```

单任务根配置只允许 `bridge`、`task`、`click` 和 `resource_root`。`task` 与 `click` 互斥。点击配置示例：

```toml
[bridge]
dir = "C:/path/to/Game/BepInEx/plugins/UnityAutomationBridge"

[click]
x = 640.0
y = 360.0
after_click = 1.0
```

### CLI 与 TOML 优先级

优先级为 `CLI > TOML > 默认值`。CLI 未提供的字段才会从 TOML 补齐。

```powershell
uv run agent/run_unity_bridge.py `
  --config config.example.toml `
  --task "[领取]领取邮件" `
  --timeout 600
```

注意：CLI 只要出现一个 `--option`，就会替换整组 TOML options，而不是逐项合并。需要混用时，应在 CLI 中完整写出全部选项。

## 批处理 runner

批处理串行运行任务，并复用同一个 Controller、Resource 和 Tasker。

```powershell
uv run agent/run_batch_tasks.py --config config.daily-quick.toml --dry-run
uv run agent/run_batch_tasks.py --config config.daily-quick.toml
```

也可通过项目提供的 `run_daily_quick.bat` 启动日常配置。

### 批处理 TOML

```toml
resource_root = "./assets"

[bridge]
dir = ""

[global]
account_id = "0"
default_timeout = 1200.0
continue_on_error = true

[[tasks]]
name = "[全局]启动脚本(置顶)PC"
enabled = true

[[tasks]]
name = "[执行]快速狩猎扫荡"
enabled = true
timeout = 600.0
```

字段语义：

| 字段 | 含义 | 默认值 |
| --- | --- | --- |
| `bridge.dir` | Bridge 插件目录；空字符串表示自动发现 | 自动发现 |
| `resource_root` | MaaFW 资源根目录 | `assets/` |
| `global.account_id` | PersistentStore 存档 ID | `"0"` |
| `global.default_timeout` | 每个任务的默认超时秒数，`0` 表示不限制 | `1200.0` |
| `global.continue_on_error` | 普通失败或超时后是否继续 | `true` |
| `tasks.name` | `interface.json` 中登记的任务名或 entry | 必填 |
| `tasks.enabled` | 是否执行该任务 | `true` |
| `tasks.timeout` | 覆盖该任务的默认超时 | 使用全局值 |
| `tasks.options` | `{ name, value }` 选项数组 | `[]` |

批处理不接受未登记的 pipeline entry。启动游戏连接前会预检所有任务及选项，包括 `enabled = false` 的任务。`--dry-run` 完成同样的预检，然后打印任务列表，不连接游戏。

配置内的旧式 `[[tasks.options]]` 仍可由 TOML 表数组解析，但维护时统一使用内联 `options = [...]`，便于查看任务边界。

## 路径规则

- `--config` 的相对路径以当前工作目录为基准；
- TOML 中 `bridge.dir` 和 `resource_root` 的相对路径以 TOML 文件所在目录为基准；
- Windows 路径可使用 `/`，或在 TOML 双引号字符串中把反斜杠写成 `\\`。

## 校验规则

配置会在连接游戏前拒绝：

- 未知字段；
- 字段类型错误；
- 空任务名、空路径或缺少 `name/value` 的 option；
- 负数或非有限超时；
- 不完整的点击坐标；
- 同时配置 task 与 click；
- 不存在的正式任务、选项或 cases-only 选项值。

input 类型的 option 由 interface 定义和 pipeline 自身处理其内容。维护配置时可先运行：

```powershell
uv run agent/run_unity_bridge.py --list-options "任务名"
uv run agent/run_batch_tasks.py --config path/to/config.toml --dry-run
```

## 超时、失败和 Ctrl+C

单任务退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 连接、资源、任务或运行时失败 |
| `2` | 参数、配置、任务解析或运行前准备失败 |
| `124` | 任务超时 |
| `130` | 用户中断 |

批处理中，单个任务超时会记录为失败；若 `continue_on_error = true`，继续下一任务，批处理最终返回 `1`。该选项不影响 Ctrl+C。

收到 Ctrl+C 后：

1. 当前 MaaFW task 收到 `post_stop()`；
2. 最多等待 5 秒；等待期间再次 Ctrl+C 会停止等待；
3. 当前任务记录为 `interrupted`，后续任务记录为 `not_run`；
4. 整个批处理返回 `130`，不再启动下一任务。

相关回归位于 `tests/test_unity_bridge_config.py`。修改 runner、TOML schema、任务预检或停止逻辑时必须更新并运行这些测试。
