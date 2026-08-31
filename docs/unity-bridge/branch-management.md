# Unity Bridge 分支管理

Unity Bridge 使用分层分支隔离可复用框架、特有业务覆盖和开发试验。目标是让核心框架可以独立审查和合并，同时保留完整功能的联调环境。

## 分支拓扑

```text
upstream/feat/NT-Suport
          |
          v
feat/unity-bridge
          |
          v
feat/unity-bridge-features
          |
          v
dev/unity-bridge
```

## 分支职责

| 分支 | 基线 | 允许内容 | 不应包含 |
| --- | --- | --- | --- |
| `feat/unity-bridge` | `upstream/feat/NT-Suport` | Controller、Bridge 协议、独立 runner、配置解析、通用动作接入、框架测试与文档 | 具体业务 pipeline、任务选项、卡带或 Steal 覆盖、现场临时修复 |
| `feat/unity-bridge-features` | `feat/unity-bridge` | `assets/resource/bridge`、Unity Bridge 特有任务与识别、业务配置、对应测试和文档 | 未验证实验、准备上游的通用修复 |
| `dev/unity-bridge` | `feat/unity-bridge-features` | 联调参数、诊断代码、候选 override、尚未确认归属的修复 | 需要直接整体合并到交付分支的杂糅提交 |

## 修改流向

开发和实机验证优先在 `dev/unity-bridge` 进行。验证完成后，不整体合并开发分支，而是整理提交并按归属提升：

1. 框架能力提交到 `feat/unity-bridge`。
2. Bridge 特有业务提交到 `feat/unity-bridge-features`。
3. 通用导航或采集问题先检查远程上游是否已有修复；已有修复优先同步上游，没有时可建立临时 `fix/*` 分支隔离验证，确认后再按实际归属拆分提交。
4. PC 或 Bridge 的特殊覆盖必须位于通用修复之后，不能让通用提交反向依赖 Unity Bridge。
5. 临时诊断、机器参数和未确认 override 留在开发分支或 `.tmp`，不提升。

每次提升前使用以下命令检查提交边界：

```powershell
git status --short
git diff --cached --name-only
git show --stat --oneline HEAD
```

## 同步顺序

上游更新时按依赖方向同步，避免下层提交进入上层：

1. 获取并检查 `upstream/feat/NT-Suport`。
2. 将 `feat/unity-bridge` 更新到新的上游基线并完成框架测试。
3. 将 `feat/unity-bridge-features` 变基到新的核心分支并完成全部离线测试。
4. 将 `dev/unity-bridge` 变基到新的特有功能分支；如果临时历史过于杂乱，可以从特有功能分支重建。
5. 单独检查尚未清理的临时 `fix/*` 分支。远程已有等价修复时优先采用远程实现，再判断本地覆盖是否仍有必要。

更新分支历史会改变提交 ID。尚未共享的本地分支可以直接变基；已经推送并被他人使用的分支，应先协调再改写历史。

## 重建开发分支

`dev/unity-bridge` 是可丢弃、可重建的联调分支。当实验提交已经完成归档、历史混入过多临时修改，或变基成本高于重建成本时，让它重新从最新的 `feat/unity-bridge-features` 起步。

重建前先确认工作区干净，并列出开发分支独有提交：

```powershell
git status --short
git log --oneline feat/unity-bridge-features..dev/unity-bridge
```

有价值的提交必须先按归属整理到核心、特有功能或临时修复分支。仍需短期保留现场时，可以额外创建带日期的归档指针，例如：

```powershell
git branch archive/dev-unity-bridge-20260831 dev/unity-bridge
```

确认开发分支上的独有历史可以舍弃后执行：

```powershell
git switch feat/unity-bridge-features
git branch -f dev/unity-bridge feat/unity-bridge-features
git switch dev/unity-bridge
git log --oneline feat/unity-bridge-features..dev/unity-bridge
```

最后一个命令应无输出，表示开发分支已经与特有功能分支处于同一起点。归档指针只用于短期兜底，确认不再需要后删除。

如果 `dev/unity-bridge` 已推送或被他人检出，重建前必须先协调。更新远程属于改写历史，应使用带租约的强制推送，不能直接使用无保护的 `--force`。

## 提交规则

- 一个提交只表达一个层级的目的；框架与业务覆盖分别提交。
- 核心框架提交不得依赖 `assets/resource/bridge` 或特有任务配置。
- 通用修复必须能在不加载 Unity Bridge runner 和资源的情况下工作并测试。
- 同一故障跨越多层时，顺序为“通用修复 → PC/Bridge 适配 → 开发诊断”。
- 稳定的行为变化需要在同一提交更新对应测试和文档。
- `dev/unity-bridge` 可以频繁重建，因此不要把它作为唯一代码保存位置。

## 合并前验证

核心框架至少运行框架相关单元测试；特有功能分支和准备交付的开发分支运行完整离线测试：

```powershell
uv run python -m unittest discover -v
git diff --check
```

涉及点击、滑动、按键或生产后继链的验证属于实机操作，执行前按[测试与排障](testing-and-debugging.md)确认范围。测试通过只说明当前分支组合有效，不代表临时修复分支已经具备直接向上游合并的边界。
