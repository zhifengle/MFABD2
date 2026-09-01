"""Unity Bridge 控制器独立入口脚本

PI V2 interface.json 不支持 "type": "Custom"，MFAAvalonia GUI 无法声明此控制器。
此脚本绕过 GUI，直接创建 UnityWin32Controller 并运行 pipeline 任务。

运行模式:

1. CLI 单任务模式:
    .\\.venv\\Scripts\\python.exe agent\\run_unity_bridge.py \\
        --bridge-dir "C:\\path\\to\\Game\\BepInEx\\plugins\\UnityAutomationBridge" \\
        --task "[执行]快速狩猎扫荡"

2. TOML 配置模式:
    .\\.venv\\Scripts\\python.exe agent\\run_unity_bridge.py --config config.toml

    配置文件示例:
        [bridge]
        dir = "C:/path/to/Game/BepInEx/plugins/UnityAutomationBridge"

        [task]
        name = "[执行]快速狩猎扫荡"
        account_id = "0"
        timeout = 600.0
        stall_timeout = 180.0

        [[task.options]]
        name = "选项名"
        value = "选项值"

    不传 --task 则只连接并截图，验证 bridge 是否就绪。
    CLI 参数优先级高于配置文件。

前置条件:
    1. 游戏已启动且窗口可见（窗口类名 UnityWndClass + 标题含 BrownDust）
    2. unity-automation-bridge BepInEx 插件已安装到游戏目录
    3. 项目 .venv 已就绪（有 requirements.txt 时 dev 模式自动接管）
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# -- 环境设置（对齐 main.py）--------------------------------------------------

os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

project_root = Path(__file__).resolve().parent.parent
agent_path = project_root / "agent"
for path in (project_root, agent_path):
    path_str = str(path)
    if path.exists() and path_str not in sys.path:
        sys.path.insert(0, path_str)

# venv 接管（dev 模式）
from utils import mfaalog, venv_ops  # noqa: E402

if (project_root / "requirements.txt").exists():
    mfaalog.info("开发模式: 启动虚拟环境管理...")
    venv_ops.ensure_venv(project_root)

from utils.unity_bridge_config import (  # noqa: E402
    SingleRunConfig,
    TomlConfigError,
    load_single_run_config,
)


def _load_interface(resource_root: str) -> dict[str, Any]:
    interface_path = Path(resource_root) / "interface.json"
    with interface_path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _get_win32_window_config(resource_root: str) -> tuple[str, str]:
    """读取项目首个 Win32 控制器的窗口定位正则。"""
    interface = _load_interface(resource_root)
    for controller in interface.get("controller", []):
        if controller.get("type") != "Win32":
            continue
        config = controller.get("win32", {})
        class_regex = config.get("class_regex")
        window_regex = config.get("window_regex")
        if class_regex and window_regex:
            return str(class_regex), str(window_regex)
    raise RuntimeError("interface.json 中没有有效的 Win32 控制器配置")


def _resolve_bridge_dir(
    resource_root: str, explicit_path: str | None
) -> tuple[str, str, str]:
    """解析 bridge 目录；未显式指定时从游戏窗口/进程自动发现。"""
    from controller.unity_win32_controller import discover_bridge_dir

    class_regex, window_regex = _get_win32_window_config(resource_root)
    if explicit_path:
        bridge_dir = Path(explicit_path).expanduser().resolve()
        if not bridge_dir.is_dir():
            raise RuntimeError(f"bridge 目录不存在: {bridge_dir}")
    else:
        bridge_dir = discover_bridge_dir(class_regex, window_regex)
        mfaalog.info(f"已从游戏窗口自动定位 bridge: {bridge_dir}")
    return str(bridge_dir), class_regex, window_regex


def _resolve_task(
    resource_root: str,
    query: str,
    option_arguments: list[str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """将任务显示名、入口名或唯一关键词解析为 pipeline entry。"""
    from utils.interface_options import (
        build_pipeline_override,
        default_selections,
        get_task_options,
    )

    interface = _load_interface(resource_root)
    tasks = interface.get("task", [])

    exact = [
        task for task in tasks if query in (task.get("name", ""), task.get("entry", ""))
    ]
    if len(exact) == 1:
        task = exact[0]
    else:
        partial = [
            task
            for task in tasks
            if query.casefold() in task.get("name", "").casefold()
            or query.casefold() in task.get("entry", "").casefold()
        ]
        if len(partial) > 1:
            candidates = "\n".join(
                f"  {item.get('name')} -> {item.get('entry')}" for item in partial[:12]
            )
            raise ValueError(f"任务关键词不唯一，请使用完整名称或 entry:\n{candidates}")
        if not partial:
            # 允许开发者直接运行尚未写入 interface.json 的 pipeline entry。
            return query, query, {}
        task = partial[0]

    entry = task.get("entry", "")
    if not entry:
        raise ValueError(f"任务没有 entry: {task.get('name', query)}")

    selections = default_selections(task, interface.get("option", {}))
    available_options = {
        item["name"]: item["def"]
        for item in get_task_options(task, interface.get("option", {}))
    }
    for argument in option_arguments or []:
        option_name, separator, value = argument.partition("=")
        option_name = option_name.strip()
        if not separator or not option_name:
            raise ValueError(f'选项格式错误: {argument!r}；应为 --option "选项名=值"')

        option_def = available_options.get(option_name)
        if option_def is None:
            choices = "、".join(available_options)
            raise ValueError(
                f"任务 {task.get('name', entry)} 没有选项 {option_name!r}；"
                f"可用选项: {choices or '无'}"
            )

        option_type = option_def.get("type", "cases-only")
        if option_type == "checkbox":
            selected = [item.strip() for item in value.split(",") if item.strip()]
        else:
            selected = value.strip()

        if option_type not in ("input", "checkbox"):
            case_names = {str(case.get("name")) for case in option_def.get("cases", [])}
            if selected not in case_names:
                choices = "、".join(case_names)
                raise ValueError(
                    f"选项 {option_name!r} 不支持值 {selected!r}；"
                    f"可用值: {choices or '无'}"
                )

        selections[option_name] = selected

    override = build_pipeline_override(task, interface.get("option", {}), selections)
    return task.get("name", entry), entry, override


def _print_task_list(resource_root: str) -> None:
    for task in _load_interface(resource_root).get("task", []):
        name = task.get("name", "")
        entry = task.get("entry", "")
        if name and entry:
            print(f"{name}\t{entry}")


def _print_task_options(resource_root: str, query: str) -> None:
    from utils.interface_options import get_task_options

    interface = _load_interface(resource_root)
    task_name, task_entry, _ = _resolve_task(resource_root, query)
    task = next(
        item
        for item in interface.get("task", [])
        if item.get("name") == task_name and item.get("entry") == task_entry
    )
    options = get_task_options(task, interface.get("option", {}))
    print(f"{task_name}\t{task_entry}")
    if not options:
        print("  无可配置选项")
        return

    for item in options:
        name = item["name"]
        option_def = item["def"]
        option_type = option_def.get("type", "select")
        if option_type == "input":
            inputs = option_def.get("inputs", [])
            default = inputs[0].get("default", "") if inputs else ""
            print(f'  --option "{name}=VALUE"  [input, default={default!r}]')
            for input_def in inputs:
                label = input_def.get("label") or input_def.get("name", "")
                description = input_def.get("description", "")
                print(f"      {label}: {description}")
            continue

        cases = [str(case.get("name")) for case in option_def.get("cases", [])]
        if option_type == "checkbox":
            default = option_def.get("default_case", [])
        else:
            default = cases[0] if cases else None
        print(
            f'  --option "{name}=VALUE"  [{option_type}, '
            f"values={','.join(cases)}, default={default!r}]"
        )


def _apply_single_config(
    args: argparse.Namespace, config: SingleRunConfig
) -> argparse.Namespace:
    """Fill values omitted on the CLI from an already validated config."""
    args.bridge_dir = (
        args.bridge_dir if args.bridge_dir is not None else config.bridge_dir
    )
    args.task = args.task if args.task is not None else config.task_name
    args.account_id = (
        args.account_id if args.account_id is not None else config.account_id or "0"
    )
    args.timeout = args.timeout if args.timeout is not None else config.timeout
    if args.timeout is None:
        args.timeout = 600.0
    args.stall_timeout = (
        args.stall_timeout
        if args.stall_timeout is not None
        else config.stall_timeout
    )
    if args.stall_timeout is None:
        args.stall_timeout = 180.0
    args.progress_node = (
        args.progress_node
        if args.progress_node is not None
        else list(config.progress_nodes)
    )
    args.loop_exempt_node = (
        args.loop_exempt_node
        if args.loop_exempt_node is not None
        else list(config.loop_exempt_nodes)
    )
    cli_recovery_entry = args.recovery_entry
    args.recovery_entry = (
        args.recovery_entry
        if args.recovery_entry is not None
        else config.recovery_entry
    )
    if args.recovery_retries is None:
        args.recovery_retries = (
            1
            if cli_recovery_entry is not None and config.recovery_entry is None
            else config.recovery_retries
        )
    if args.recovery_retries > 0 and not args.recovery_entry:
        raise ValueError("recovery_retries 大于 0 时必须配置 recovery_entry")
    args.option = args.option if args.option is not None else list(config.options)
    args.click = args.click if args.click is not None else config.click
    args.after_click = (
        args.after_click if args.after_click is not None else config.after_click
    )
    if args.after_click is None:
        args.after_click = 1.0
    args.resource_root = (
        args.resource_root
        if args.resource_root is not None
        else config.resource_root or str(project_root / "assets")
    )
    return args


def _non_negative_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是数字") from error
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("必须是非负有限数")
    return result


def _non_negative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if result < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return result


def _ensure_ocr_models(resource_root: str) -> None:
    """确保 standalone 使用的项目资源包含 OCR 模型。"""
    model_dir = Path(resource_root) / "resource" / "base" / "model" / "ocr"
    required = [model_dir / name for name in ("det.onnx", "rec.onnx", "keys.txt")]
    if all(path.is_file() for path in required):
        return

    default_assets = (project_root / "assets").resolve()
    if Path(resource_root).resolve() == default_assets:
        common_assets = default_assets / "MaaCommonAssets" / "OCR"
        if common_assets.is_dir():
            mfaalog.info("OCR 模型尚未配置，正在运行项目 configure.py...")
            from configure import configure_ocr_model

            configure_ocr_model()
            if all(path.is_file() for path in required):
                return

    missing = ", ".join(path.name for path in required if not path.is_file())
    raise RuntimeError(
        f"项目 OCR 模型缺失 ({missing})。请准备 assets/MaaCommonAssets 后运行: "
        ".\\.venv\\Scripts\\python.exe configure.py"
    )


@dataclass(frozen=True)
class _ProgressSnapshot:
    last_node: str
    phase: str
    last_event: str
    last_activity_time: float
    phase_started_time: float
    phase_active: bool
    repeated_transition_since: float | None
    repeated_transition: tuple[str, str] | None


class _ProgressSink:
    """输出节点事件，并维护当前 MaaFramework task 的 watchdog 状态。"""

    _ACTIVITY_PREFIXES = (
        "Node.PipelineNode.",
        "Node.Recognition.",
        "Node.RecognitionNode.",
        "Node.Action.",
        "Node.ActionNode.",
        "Node.WaitFreezes.",
        "Node.NextList.",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task_id: int | None = None
        self._event_task_id: int | None = None
        self.last_node = ""
        self.phase = "waiting"
        self.last_event = ""
        self.last_activity_time = time.monotonic()
        self.phase_started_time = self.last_activity_time
        self.phase_active = False
        self.repeated_transition_since: float | None = None
        self.repeated_transition: tuple[str, str] | None = None
        self._previous_node = ""
        self._seen_transitions: set[tuple[str, str]] = set()
        self._progress_node_patterns: tuple[str, ...] = ()
        self._loop_exempt_node_patterns: tuple[str, ...] = ()

    def prepare_task(
        self,
        started_at: float,
        progress_node_patterns: tuple[str, ...] = (),
        loop_exempt_node_patterns: tuple[str, ...] = (),
    ) -> None:
        """Reset state before posting, while temporarily accepting the new task id."""
        with self._lock:
            self._task_id = None
            self._event_task_id = None
            self.last_node = ""
            self.phase = "starting"
            self.last_event = "Tasker.Task.Starting"
            self.last_activity_time = started_at
            self.phase_started_time = started_at
            self.phase_active = False
            self.repeated_transition_since = None
            self.repeated_transition = None
            self._previous_node = ""
            self._seen_transitions.clear()
            self._progress_node_patterns = progress_node_patterns
            self._loop_exempt_node_patterns = loop_exempt_node_patterns

    def begin_task(self, task_id: int, started_at: float) -> None:
        """Bind events to the posted task without losing synchronous start events."""
        with self._lock:
            self._task_id = task_id
            if self._event_task_id != task_id:
                self.last_node = ""
                self.phase = "starting"
                self.last_event = "Tasker.Task.Starting"
                self.last_activity_time = started_at
                self.phase_started_time = started_at
                self.phase_active = False

    def snapshot(self) -> _ProgressSnapshot:
        with self._lock:
            return _ProgressSnapshot(
                last_node=self.last_node,
                phase=self.phase,
                last_event=self.last_event,
                last_activity_time=self.last_activity_time,
                phase_started_time=self.phase_started_time,
                phase_active=self.phase_active,
                repeated_transition_since=self.repeated_transition_since,
                repeated_transition=self.repeated_transition,
            )

    @staticmethod
    def _matches(name: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    def _record_transition(self, name: str, now: float) -> None:
        edge = (self._previous_node, name)
        is_checkpoint = self._matches(name, self._progress_node_patterns)
        is_exempt = self._matches(name, self._loop_exempt_node_patterns)
        if is_checkpoint or is_exempt:
            self._seen_transitions.clear()
            self.repeated_transition_since = None
            self.repeated_transition = None
        elif edge not in self._seen_transitions:
            self._seen_transitions.add(edge)
            self.repeated_transition_since = None
            self.repeated_transition = None
        elif self.repeated_transition_since is None:
            self.repeated_transition_since = now
            self.repeated_transition = edge
        self._previous_node = name

    def emit(self, message: str, details: dict[str, Any]) -> None:
        if not message.startswith(self._ACTIVITY_PREFIXES):
            return

        name = str(details.get("name", "?"))
        state = message.rsplit(".", 1)[-1]
        with self._lock:
            event_task_id = details.get("task_id")
            if self._task_id is not None and event_task_id != self._task_id:
                return
            self._event_task_id = event_task_id
            now = time.monotonic()
            if message.startswith("Node.PipelineNode."):
                self.last_node = name
                if state == "Starting":
                    self._record_transition(name, now)
                    self.phase = "PipelineNode"
                    self.phase_started_time = now
                    self.phase_active = True
                else:
                    self.phase_active = False
            elif message.startswith("Node.NextList."):
                self.phase = "NextList"
                self.phase_started_time = now
                self.phase_active = state == "Starting"
            elif not self.phase_active:
                self.phase = message.split(".", 2)[1]
                self.phase_started_time = now
            self.last_event = message
            self.last_activity_time = now
            suffix = f" focus={details.get('focus')}" if details.get("focus") else ""
            mfaalog.info(f"[节点] {name}: {state}{suffix}")


def _attach_progress_sink(tasker: Any) -> _ProgressSink:
    from maa.context import ContextEventSink

    progress = _ProgressSink()

    class ContextSink(ContextEventSink):
        def on_raw_notification(self, context, msg, details):
            del context
            progress.emit(msg, details)

    if tasker.add_context_sink(ContextSink()) is None:
        mfaalog.warning("无法注册节点日志监听器")
    return progress


def _stop_task(tasker: Any, grace_seconds: float = 5.0) -> bool:
    """请求停止任务，避免再次进入无期限 wait。"""
    try:
        stop_job = tasker.post_stop()
    except Exception as error:
        mfaalog.error(f"提交停止请求失败: {error}")
        return False

    deadline = time.monotonic() + grace_seconds
    try:
        while not stop_job.done and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped = stop_job.done and stop_job.succeeded
    except KeyboardInterrupt:
        mfaalog.warning("再次收到 Ctrl+C，不再等待停止请求完成")
        return False
    except Exception as error:
        mfaalog.error(f"等待停止请求时失败: {error}")
        return False
    if not stopped:
        mfaalog.warning(
            f"停止请求在 {grace_seconds:g} 秒内未完成，进程将直接退出"
        )
        return False
    return True


def _run_task_with_timeout(
    tasker: Any,
    entry: str,
    pipeline_override: dict[str, Any],
    timeout_seconds: float,
    stall_timeout_seconds: float = 180.0,
    progress: _ProgressSink | None = None,
    progress_node_patterns: tuple[str, ...] = (),
    loop_exempt_node_patterns: tuple[str, ...] = (),
) -> int:
    progress = progress or _attach_progress_sink(tasker)
    started = time.monotonic()
    watchdog_warning: tuple[str, float] | None = None
    pending_limit = min(30.0, stall_timeout_seconds)
    grace_seconds = min(15.0, max(1.0, stall_timeout_seconds * 0.1))

    try:
        progress.prepare_task(
            started,
            progress_node_patterns,
            loop_exempt_node_patterns,
        )
        task_job = tasker.post_task(entry, pipeline_override=pipeline_override)
        progress.begin_task(task_job.job_id, started)
        while not task_job.done:
            now = time.monotonic()
            snapshot = progress.snapshot()
            if timeout_seconds > 0 and now - started >= timeout_seconds:
                if task_job.done:
                    break
                mfaalog.error(
                    f"任务超时（{timeout_seconds:g} 秒），最后节点: "
                    f"{snapshot.last_node or '尚未进入节点'}"
                )
                return 124 if _stop_task(tasker) else 125

            pending_for = now - started
            phase_for = now - snapshot.phase_started_time
            cycle_for = (
                now - snapshot.repeated_transition_since
                if snapshot.repeated_transition_since is not None
                else 0.0
            )
            idle_for = now - snapshot.last_activity_time
            violation: tuple[str, int, float, str] | None = None
            if (
                stall_timeout_seconds > 0
                and getattr(task_job, "pending", False)
                and pending_for >= pending_limit
            ):
                violation = ("pending", 126, pending_for, "任务一直处于 Pending")
            elif (
                stall_timeout_seconds > 0
                and snapshot.repeated_transition_since is not None
                and cycle_for >= stall_timeout_seconds
            ):
                edge = snapshot.repeated_transition or ("?", "?")
                violation = (
                    "cycle",
                    128,
                    cycle_for,
                    f"节点转移持续重复 {edge[0]} -> {edge[1]}",
                )
            elif (
                stall_timeout_seconds > 0
                and snapshot.phase_active
                and phase_for >= stall_timeout_seconds
            ):
                violation = (
                    "node",
                    127,
                    phase_for,
                    f"{snapshot.phase} 阶段未结束",
                )
            elif stall_timeout_seconds > 0 and idle_for >= stall_timeout_seconds:
                violation = (
                    "idle",
                    129,
                    idle_for,
                    f"节点事件静默，最后事件: {snapshot.last_event or '无'}",
                )

            if violation is None:
                if watchdog_warning is not None:
                    mfaalog.info(
                        f"watchdog 状态已恢复，取消 {watchdog_warning[0]} 宽限期"
                    )
                watchdog_warning = None
            else:
                cause, exit_code, elapsed, description = violation
                if watchdog_warning is None or watchdog_warning[0] != cause:
                    watchdog_warning = (cause, now)
                    mfaalog.warning(
                        f"watchdog 检测到 {description}（{elapsed:.1f} 秒），进入 "
                        f"{grace_seconds:g} 秒宽限期；最后节点: "
                        f"{snapshot.last_node or '尚未进入节点'}"
                    )
                if now - watchdog_warning[1] >= grace_seconds:
                    if task_job.done:
                        break
                    mfaalog.error(
                        f"watchdog {cause} 超时，停止任务；最后节点: "
                        f"{snapshot.last_node or '尚未进入节点'}"
                    )
                    return exit_code if _stop_task(tasker) else 125
            time.sleep(0.1)
    except KeyboardInterrupt:
        mfaalog.warning("收到 Ctrl+C，正在停止任务...")
        _stop_task(tasker)
        return 130

    elapsed = time.monotonic() - started
    if not task_job.succeeded:
        mfaalog.error(f"任务失败: entry={entry}, elapsed={elapsed:.1f}s")
        return 1

    detail = task_job.get()
    mfaalog.info(f"任务完成: entry={entry}, elapsed={elapsed:.1f}s")
    mfaalog.debug(f"任务详情: {detail}")
    return 0


_WATCHDOG_CAUSES = {
    124: "total",
    126: "pending",
    127: "node",
    128: "cycle",
    129: "idle",
}


def _run_task_with_recovery(
    tasker: Any,
    progress: _ProgressSink,
    create_tasker: Any,
    entry: str,
    pipeline_override: dict[str, Any],
    *,
    timeout_seconds: float,
    stall_timeout_seconds: float,
    progress_node_patterns: tuple[str, ...] = (),
    loop_exempt_node_patterns: tuple[str, ...] = (),
    recovery_entry: str | None = None,
    recovery_pipeline_override: dict[str, Any] | None = None,
    recovery_retries: int = 0,
) -> tuple[int, Any, _ProgressSink]:
    """Run one logical task, optionally recovering and retrying after watchdog stops."""
    logical_started = time.monotonic()
    attempt = 0

    def remaining_timeout(limit: float) -> float:
        if timeout_seconds <= 0:
            return limit
        remaining = max(0.0, timeout_seconds - (time.monotonic() - logical_started))
        if limit <= 0:
            return remaining
        return min(limit, remaining)

    while True:
        remaining = remaining_timeout(0.0)
        if timeout_seconds > 0 and remaining <= 0:
            mfaalog.error("逻辑任务（含恢复与重试）已耗尽总超时预算")
            return 124, tasker, progress

        exit_code = _run_task_with_timeout(
            tasker,
            entry,
            pipeline_override,
            remaining,
            stall_timeout_seconds,
            progress,
            progress_node_patterns,
            loop_exempt_node_patterns,
        )
        if exit_code == 0 or exit_code in (125, 130):
            return exit_code, tasker, progress

        cause = _WATCHDOG_CAUSES.get(exit_code)
        can_recover = (
            cause is not None
            and cause != "total"
            and recovery_entry is not None
            and attempt < recovery_retries
        )
        if not can_recover:
            return exit_code, tasker, progress

        attempt += 1
        mfaalog.warning(
            f"watchdog 原因={cause}；启动恢复 entry={recovery_entry}，"
            f"之后第 {attempt}/{recovery_retries} 次重试原任务"
        )
        try:
            tasker = create_tasker()
        except Exception as error:
            mfaalog.error(f"为恢复任务重建 Tasker 失败: {error}")
            return 1, tasker, progress
        progress = _attach_progress_sink(tasker)
        recovery_budget = remaining_timeout(120.0)
        if timeout_seconds > 0 and recovery_budget <= 0:
            return 124, tasker, progress
        recovery_code = _run_task_with_timeout(
            tasker,
            recovery_entry,
            recovery_pipeline_override or {},
            recovery_budget,
            stall_timeout_seconds,
            progress,
        )
        if recovery_code != 0:
            mfaalog.error(
                f"恢复 entry 失败: {recovery_entry}, 退出码 {recovery_code}"
            )
            return recovery_code, tasker, progress
        mfaalog.info(f"恢复 entry 完成，重新运行原任务: {entry}")


def _load_pipeline_resources(resource: Any, resource_root: str) -> bool:
    """Load the same base, platform and bridge overlays for every runner."""
    res_root = Path(resource_root)
    bundle_paths = [
        res_root / "resource" / "base",
        res_root / "resource" / "pc",
    ]
    bridge_path = res_root / "resource" / "bridge"
    if bridge_path.is_dir():
        bundle_paths.append(bridge_path)

    for bundle_path in bundle_paths:
        mfaalog.info(f"加载资源: {bundle_path}")
        if not resource.post_bundle(str(bundle_path)).wait().succeeded:
            mfaalog.error(f"资源加载失败: {bundle_path}")
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Unity Bridge 控制器独立运行入口")
    parser.add_argument(
        "--config",
        default=None,
        help="TOML 配置文件路径；CLI 参数优先级高于配置文件",
    )
    parser.add_argument(
        "--bridge-dir",
        default=None,
        help="unity-automation-bridge 插件目录路径，"
        "即 <GameDir>/BepInEx/plugins/UnityAutomationBridge；默认自动发现",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="任务显示名、pipeline entry 或唯一关键词；例如 邮件、Mail_HomePage",
    )
    parser.add_argument(
        "--option",
        action="append",
        default=None,
        metavar="NAME=VALUE",
        help="覆盖任务选项，可重复使用；input 选项直接填写其值",
    )
    parser.add_argument(
        "--account",
        "--account-id",
        dest="account_id",
        default=None,
        help="PersistentStore 存档账号 ID（默认 0，对应 agent_save_data.json）",
    )
    parser.add_argument(
        "--click",
        nargs=2,
        type=_non_negative_float,
        metavar=("X", "Y"),
        help="连接后通过 Unity bridge 点击一组 1280x720 基准坐标并退出",
    )
    parser.add_argument(
        "--after-click",
        type=_non_negative_float,
        default=None,
        help="--click 后等待秒数（默认 1）",
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="列出 interface.json 中的任务后退出，不需要 --bridge-dir",
    )
    parser.add_argument(
        "--list-options",
        metavar="TASK",
        help="列出指定任务可用的 --option 后退出，不连接游戏",
    )
    parser.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=None,
        help="任务超时秒数（默认 600；设为 0 表示不限制）",
    )
    parser.add_argument(
        "--stall-timeout",
        type=_non_negative_float,
        default=None,
        help="任务无有效进展的秒数（默认 180；0 表示关闭 watchdog）",
    )
    parser.add_argument(
        "--progress-node",
        action="append",
        default=None,
        metavar="GLOB",
        help="命中时重置循环检测窗口的节点 glob；可重复指定",
    )
    parser.add_argument(
        "--loop-exempt-node",
        action="append",
        default=None,
        metavar="GLOB",
        help="允许长期循环的节点 glob；可重复指定",
    )
    parser.add_argument(
        "--recovery-entry",
        default=None,
        help="watchdog 中断后运行的恢复 pipeline entry",
    )
    parser.add_argument(
        "--recovery-retries",
        type=_non_negative_int,
        default=None,
        help="恢复后重试原任务的次数",
    )
    parser.add_argument(
        "--resource-root",
        default=None,
        help="资源根目录（默认 assets/）",
    )
    args = parser.parse_args()

    config = SingleRunConfig()
    if args.config:
        try:
            config = load_single_run_config(args.config)
            mfaalog.info(f"已加载配置文件: {args.config}")
        except TomlConfigError as error:
            mfaalog.error(f"配置文件加载失败: {error}")
            return 2
    try:
        _apply_single_config(args, config)
    except ValueError as error:
        mfaalog.error(f"参数配置无效: {error}")
        return 2

    if args.list_tasks:
        _print_task_list(args.resource_root)
        return 0
    if args.list_options:
        try:
            _print_task_options(args.resource_root, args.list_options)
        except (OSError, ValueError, StopIteration, json.JSONDecodeError) as error:
            mfaalog.error(str(error))
            return 2
        return 0
    if args.task and args.click:
        parser.error("--task 与 --click 不能同时使用")

    resolved_task: tuple[str, str, dict[str, Any]] | None = None
    if args.task:
        try:
            resolved_task = _resolve_task(
                args.resource_root,
                args.task,
                args.option,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            mfaalog.error(str(error))
            return 2
        task_name, task_entry, _ = resolved_task
        mfaalog.info(f"已选择任务: {task_name} -> {task_entry}")

        from utils.persistent_store import PersistentStore

        PersistentStore.switch_account(args.account_id)
        PersistentStore.load()
        mfaalog.info(f"使用存档账号: {args.account_id}")

    try:
        args.bridge_dir, class_regex, window_regex = _resolve_bridge_dir(
            args.resource_root, args.bridge_dir
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        mfaalog.error(f"自动定位 bridge 失败: {error}")
        return 2

    # -- 1. 创建控制器 -------------------------------------------------------
    from controller.unity_win32_controller import UnityWin32Controller

    mfaalog.info(f"创建 UnityWin32Controller (bridge_dir={args.bridge_dir})")
    controller = UnityWin32Controller(
        args.bridge_dir,
        window_class=class_regex,
        window_title=window_regex,
    )

    # 连接
    mfaalog.info("连接控制器...")
    conn_job = controller.post_connection().wait()
    if not conn_job.succeeded:
        mfaalog.error(
            "连接失败！请检查:\n"
            "  1. 游戏是否已启动\n"
            "  2. unity-automation-bridge 插件是否已安装\n"
            "  3. bridge-dir 路径是否正确"
        )
        sys.exit(1)
    mfaalog.info("控制器连接成功")

    # 截图验证
    mfaalog.info("截图中...")
    try:
        image = controller.post_screencap().wait().get()
        mfaalog.info(f"截图成功: shape={image.shape}, dtype={image.dtype}")
    except Exception as e:
        mfaalog.error(f"截图失败: {e}")
        sys.exit(1)

    if args.click:
        x, y = (round(value) for value in args.click)
        mfaalog.info(f"通过 Unity bridge 点击: ({x}, {y})")
        click_job = controller.post_click(x, y).wait()
        if not click_job.succeeded:
            mfaalog.error(f"Unity bridge 点击失败: ({x}, {y})")
            return 1
        if args.after_click > 0:
            time.sleep(args.after_click)
        mfaalog.info(f"Unity bridge 点击成功: ({x}, {y})")
        return 0

    if not args.task:
        mfaalog.info("未指定 --task，连接+截图验证完成")
        return 0

    assert resolved_task is not None
    _, task_entry, pipeline_override = resolved_task

    try:
        _ensure_ocr_models(args.resource_root)
    except RuntimeError as error:
        mfaalog.error(str(error))
        return 2

    # -- 2. 创建 Resource 并加载 pipeline ----------------------------------
    from maa.resource import Resource
    from utils.standalone_runtime import register_standalone_extensions

    resource = Resource()

    # 独立模式不能让 maa.agent 把 Library 切换为 AgentServer 模式。
    # helper 会截获装饰器注册，再把实例注册到当前 Resource。
    mfaalog.info("注册自定义动作/识别器...")
    action_names, recognition_names = register_standalone_extensions(resource)
    for name in action_names:
        mfaalog.info(f"  动作: {name}")
    for name in recognition_names:
        mfaalog.info(f"  识别: {name}")

    if not _load_pipeline_resources(resource, args.resource_root):
        return 1
    if args.recovery_entry and args.recovery_entry not in resource.node_list:
        mfaalog.error(f"恢复 entry 不存在于已加载资源: {args.recovery_entry}")
        return 2

    # -- 3. 创建 Tasker 并运行任务 -------------------------------------------
    from maa.tasker import Tasker

    def create_tasker() -> Any:
        new_tasker = Tasker()
        if not new_tasker.bind(resource, controller):
            raise RuntimeError("Tasker 绑定 Resource/Controller 失败")
        if not new_tasker.inited:
            raise RuntimeError("Tasker 初始化失败")
        return new_tasker

    try:
        tasker = create_tasker()
    except RuntimeError as error:
        mfaalog.error(str(error))
        return 1

    mfaalog.info(f"运行任务: {task_entry}")
    progress = _attach_progress_sink(tasker)
    exit_code, _, _ = _run_task_with_recovery(
        tasker,
        progress,
        create_tasker,
        task_entry,
        pipeline_override,
        timeout_seconds=args.timeout,
        stall_timeout_seconds=args.stall_timeout,
        progress_node_patterns=tuple(args.progress_node),
        loop_exempt_node_patterns=tuple(args.loop_exempt_node),
        recovery_entry=args.recovery_entry,
        recovery_retries=args.recovery_retries,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
