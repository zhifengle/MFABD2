"""Unity Bridge 批量任务执行脚本

从 TOML 配置文件读取多个任务并串行执行。
支持 continue_on_error 模式，单个任务失败后继续执行后续任务。

使用方法:
    python agent\\run_batch_tasks.py --config config.daily-quick.toml

配置文件格式:
    [bridge]
    dir = ""  # 留空则自动发现

    [global]
    account_id = "0"
    default_timeout = 600.0
    default_stall_timeout = 180.0
    continue_on_error = true

    [[tasks]]
    name = "任务名"
    enabled = true
    timeout = 600.0  # 可选，覆盖 default_timeout
    options = [
        {name = "选项名1", value = "选项值1"},
        {name = "选项名2", value = "选项值2"}
    ]
"""

from __future__ import annotations

import argparse
import time
from typing import Any

# 复用 run_unity_bridge.py 的环境设置和工具函数
import run_unity_bridge as bridge

# 从 run_unity_bridge 复用函数
_load_interface = bridge._load_interface
_resolve_bridge_dir = bridge._resolve_bridge_dir
_resolve_task = bridge._resolve_task
_ensure_ocr_models = bridge._ensure_ocr_models
_run_task_with_timeout = bridge._run_task_with_timeout
_run_task_with_recovery = bridge._run_task_with_recovery
_load_pipeline_resources = bridge._load_pipeline_resources

from utils import mfaalog
from utils.unity_bridge_config import (
    BatchTaskConfig,
    TomlConfigError,
    load_batch_run_config,
)


class BatchTaskRunner:
    """批量任务执行器"""

    def __init__(
        self,
        controller: Any,
        resource: Any,
        default_timeout: float,
        continue_on_error: bool,
        default_stall_timeout: float = 180.0,
    ):
        self.controller = controller
        self.resource = resource
        self.default_timeout = default_timeout
        self.default_stall_timeout = default_stall_timeout
        self.continue_on_error = continue_on_error
        self.tasker = None
        self.results: list[dict[str, Any]] = []

    def _create_tasker(self) -> Any:
        """创建并绑定 Tasker"""
        from maa.tasker import Tasker

        tasker = Tasker()
        if not tasker.bind(self.resource, self.controller):
            raise RuntimeError("Tasker 绑定 Resource/Controller 失败")
        if not tasker.inited:
            raise RuntimeError("Tasker 初始化失败")
        return tasker

    def run_tasks(
        self, resource_root: str, tasks_config: tuple[BatchTaskConfig, ...]
    ) -> dict[str, Any]:
        """执行批量任务并返回统计结果"""
        if not tasks_config:
            mfaalog.warning("配置中没有任务")
            return {
                "total": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "interrupted": 0,
                "not_run": 0,
            }

        # 创建 tasker（复用同一个实例）
        self.tasker = self._create_tasker()
        progress = bridge._attach_progress_sink(self.tasker)
        self.results = []

        total = len(tasks_config)
        success = 0
        failed = 0
        skipped = 0
        interrupted = 0

        mfaalog.info(f"开始批量任务执行，共 {total} 个任务")
        mfaalog.info(f"失败后继续: {self.continue_on_error}")

        for index, task_config in enumerate(tasks_config, 1):
            task_name = task_config.name
            enabled = task_config.enabled
            timeout = (
                self.default_timeout
                if task_config.timeout is None
                else task_config.timeout
            )
            stall_timeout = (
                self.default_stall_timeout
                if task_config.stall_timeout is None
                else task_config.stall_timeout
            )

            if not enabled:
                mfaalog.info(f"[{index}/{total}] 跳过任务: {task_name} (已禁用)")
                skipped += 1
                self.results.append(
                    {
                        "index": index,
                        "name": task_name,
                        "status": "skipped",
                        "reason": "disabled",
                    }
                )
                continue

            mfaalog.info(f"[{index}/{total}] 开始任务: {task_name}")
            start_time = time.monotonic()

            try:
                # 解析任务
                resolved_name, entry, pipeline_override = _resolve_task(
                    resource_root, task_name, list(task_config.options)
                )
                mfaalog.info(f"[{index}/{total}] 已解析: {resolved_name} -> {entry}")
                exit_code, self.tasker, progress = _run_task_with_recovery(
                    self.tasker,
                    progress,
                    self._create_tasker,
                    entry,
                    pipeline_override,
                    timeout_seconds=timeout,
                    stall_timeout_seconds=stall_timeout,
                    progress_node_patterns=task_config.progress_nodes,
                    loop_exempt_node_patterns=task_config.loop_exempt_nodes,
                    recovery_entry=task_config.recovery_entry,
                    recovery_retries=task_config.recovery_retries,
                )

                elapsed = time.monotonic() - start_time

                if exit_code == 130:
                    interrupted = 1
                    mfaalog.warning(
                        f"[{index}/{total}] 用户中断任务: {task_name} "
                        f"(耗时 {elapsed:.1f}s)"
                    )
                    self.results.append(
                        {
                            "index": index,
                            "name": task_name,
                            "status": "interrupted",
                            "exit_code": exit_code,
                            "elapsed": elapsed,
                        }
                    )
                    mfaalog.warning("批处理已中断，不再执行后续任务")
                    break
                if exit_code == 0:
                    success += 1
                    mfaalog.info(
                        f"[{index}/{total}] 任务完成: {task_name} "
                        f"(耗时 {elapsed:.1f}s)"
                    )
                    self.results.append(
                        {
                            "index": index,
                            "name": task_name,
                            "status": "success",
                            "elapsed": elapsed,
                        }
                    )
                else:
                    failed += 1
                    mfaalog.error(
                        f"[{index}/{total}] 任务失败: {task_name} "
                        f"(退出码 {exit_code}, 耗时 {elapsed:.1f}s)"
                    )
                    self.results.append(
                        {
                            "index": index,
                            "name": task_name,
                            "status": "failed",
                            "exit_code": exit_code,
                            "elapsed": elapsed,
                            "reason": (
                                "stop_unconfirmed"
                                if exit_code == 125
                                else "task_failed"
                            ),
                        }
                    )

                    if exit_code == 125:
                        mfaalog.error(
                            "停止请求未确认完成；为避免复用仍在运行的 Tasker，"
                            "终止后续批处理"
                        )
                        break
                    if not self.continue_on_error:
                        mfaalog.error("任务失败，停止后续任务执行")
                        break
                    if exit_code in bridge._WATCHDOG_CAUSES and index < total:
                        mfaalog.info(
                            "watchdog 已确认任务停止，重建 Tasker 后继续批处理"
                        )
                        self.tasker = self._create_tasker()
                        progress = bridge._attach_progress_sink(self.tasker)

            except Exception as error:
                failed += 1
                elapsed = time.monotonic() - start_time
                mfaalog.error(f"[{index}/{total}] 任务异常: {task_name} - {error}")
                self.results.append(
                    {
                        "index": index,
                        "name": task_name,
                        "status": "error",
                        "error": str(error),
                        "elapsed": elapsed,
                    }
                )

                if not self.continue_on_error:
                    mfaalog.error("任务异常，停止后续任务执行")
                    break

        not_run = total - success - failed - skipped - interrupted
        not_run_reason = (
            "stopped_after_interrupt" if interrupted else "stopped_after_failure"
        )
        if not_run:
            for index in range(len(self.results) + 1, total + 1):
                task = tasks_config[index - 1]
                self.results.append(
                    {
                        "index": index,
                        "name": task.name,
                        "status": "not_run",
                        "reason": not_run_reason,
                    }
                )

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "interrupted": interrupted,
            "not_run": not_run,
            "results": self.results,
        }


def _print_summary(stats: dict[str, Any]) -> None:
    """打印执行摘要"""
    print("\n" + "=" * 60)
    print("批量任务执行摘要")
    print("=" * 60)
    print(f"总任务数: {stats['total']}")
    print(f"成功: {stats['success']}")
    print(f"失败: {stats['failed']}")
    print(f"跳过: {stats['skipped']}")
    if stats.get("interrupted"):
        print(f"用户中断: {stats['interrupted']}")
    if stats.get("not_run"):
        print(f"未运行: {stats['not_run']}")
    print("=" * 60)

    if stats.get("results"):
        print("\n详细结果:")
        for result in stats["results"]:
            index = result["index"]
            name = result["name"]
            status = result["status"]
            elapsed = result.get("elapsed", 0)

            if status == "success":
                print(f"  [{index}] ✓ {name} ({elapsed:.1f}s)")
            elif status == "failed":
                exit_code = result.get("exit_code", "?")
                print(f"  [{index}] ✗ {name} (退出码 {exit_code}, {elapsed:.1f}s)")
            elif status == "error":
                error = result.get("error", "Unknown")
                print(f"  [{index}] ✗ {name} (异常: {error})")
            elif status == "interrupted":
                print(f"  [{index}] ! {name} (用户中断, {elapsed:.1f}s)")
            elif status == "skipped":
                reason = result.get("reason", "")
                print(f"  [{index}] - {name} (跳过: {reason})")
            elif status == "not_run":
                print(f"  [{index}] - {name} (未运行: 前序任务失败)")
        print()


def _validate_tasks(resource_root: str, tasks: tuple[BatchTaskConfig, ...]) -> None:
    """Resolve every configured task before connecting to the game."""
    interface_tasks = _load_interface(resource_root).get("task", [])
    for task in tasks:
        query = task.name.casefold()
        if not any(
            query in str(item.get("name", "")).casefold()
            or query in str(item.get("entry", "")).casefold()
            for item in interface_tasks
        ):
            raise ValueError(
                f"批处理任务 {task.name!r} 不在 interface.json 中；"
                "批处理不接受未登记的 pipeline entry"
            )
        _resolve_task(resource_root, task.name, list(task.options))


def main():
    parser = argparse.ArgumentParser(description="Unity Bridge 批量任务执行器")
    parser.add_argument(
        "--config",
        required=True,
        help="批量任务配置文件路径（TOML 格式）",
    )
    parser.add_argument(
        "--bridge-dir",
        default=None,
        help="覆盖配置文件中的 bridge 目录路径",
    )
    parser.add_argument(
        "--resource-root",
        default=None,
        help="资源根目录（默认 assets/）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印任务列表，不实际执行",
    )
    args = parser.parse_args()

    # 加载配置
    try:
        config = load_batch_run_config(args.config)
        mfaalog.info(f"已加载配置文件: {args.config}")
    except TomlConfigError as error:
        mfaalog.error(f"配置文件加载失败: {error}")
        return 2

    bridge_dir = args.bridge_dir if args.bridge_dir is not None else config.bridge_dir
    resource_root = (
        args.resource_root
        if args.resource_root is not None
        else config.resource_root or str(bridge.project_root / "assets")
    )

    try:
        _validate_tasks(resource_root, config.tasks)
    except (OSError, ValueError) as error:
        mfaalog.error(f"任务配置校验失败: {error}")
        return 2

    # Dry-run 模式
    if args.dry_run:
        print("批量任务列表（dry-run 模式）:")
        for index, task in enumerate(config.tasks, 1):
            status = "启用" if task.enabled else "禁用"
            print(f"  [{index}] {task.name} ({status})")
            for option in task.options:
                name, _, value = option.partition("=")
                print(f"      {name} = {value}")
        return 0

    # 切换账号并加载存档
    from utils.account_sync import bind_runtime_account
    from utils.persistent_store import PersistentStore

    bind_runtime_account(config.account_id)
    PersistentStore.load()
    mfaalog.info(f"使用存档账号: {config.account_id}")

    # 解析 bridge 目录
    try:
        resolved_bridge_dir, class_regex, window_regex = _resolve_bridge_dir(
            resource_root, bridge_dir
        )
    except (OSError, RuntimeError) as error:
        mfaalog.error(f"自动定位 bridge 失败: {error}")
        return 2

    # 确保 OCR 模型
    try:
        _ensure_ocr_models(resource_root)
    except RuntimeError as error:
        mfaalog.error(str(error))
        return 2

    # 创建控制器
    from controller.unity_win32_controller import UnityWin32Controller

    mfaalog.info(f"创建 UnityWin32Controller (bridge_dir={resolved_bridge_dir})")
    controller = UnityWin32Controller(
        resolved_bridge_dir,
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
        return 1
    mfaalog.info("控制器连接成功")

    # 截图验证
    mfaalog.info("截图验证...")
    try:
        image = controller.post_screencap().wait().get()
        mfaalog.info(f"截图成功: shape={image.shape}")
    except Exception as error:
        mfaalog.error(f"截图失败: {error}")
        return 1

    # 创建 Resource 并加载 pipeline
    from maa.resource import Resource
    from utils.standalone_runtime import register_standalone_extensions

    resource = Resource()

    mfaalog.info("注册自定义动作/识别器...")
    action_names, recognition_names = register_standalone_extensions(resource)
    mfaalog.info(f"  动作: {len(action_names)} 个")
    mfaalog.info(f"  识别: {len(recognition_names)} 个")

    if not _load_pipeline_resources(resource, resource_root):
        return 1
    missing_recovery_entries = sorted(
        {
            task.recovery_entry
            for task in config.tasks
            if task.recovery_entry and task.recovery_entry not in resource.node_list
        }
    )
    if missing_recovery_entries:
        mfaalog.error(
            "恢复 entry 不存在于已加载资源: "
            + "、".join(missing_recovery_entries)
        )
        return 2

    # 创建批量任务执行器
    runner = BatchTaskRunner(
        controller=controller,
        resource=resource,
        default_timeout=config.default_timeout,
        default_stall_timeout=config.default_stall_timeout,
        continue_on_error=config.continue_on_error,
    )

    # 执行批量任务
    try:
        stats = runner.run_tasks(resource_root, config.tasks)
    except KeyboardInterrupt:
        mfaalog.warning("收到 Ctrl+C，正在停止整个批处理...")
        if runner.tasker is not None:
            bridge._stop_task(runner.tasker)
        return 130
    except RuntimeError as error:
        mfaalog.error(str(error))
        return 1

    # 打印摘要
    _print_summary(stats)

    # 返回退出码
    if stats.get("interrupted"):
        return 130
    if stats["failed"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        mfaalog.warning("收到 Ctrl+C，批处理已终止")
        raise SystemExit(130) from None
