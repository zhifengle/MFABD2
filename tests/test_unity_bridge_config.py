from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from agent.run_unity_bridge import (
    _ProgressSink,
    _apply_single_config,
    _load_pipeline_resources,
    _run_task_with_recovery,
    _run_task_with_timeout,
)
from agent.run_batch_tasks import BatchTaskRunner
from agent.utils.unity_bridge_config import (
    BatchTaskConfig,
    SingleRunConfig,
    TomlConfigError,
    load_batch_run_config,
    load_single_run_config,
)


class UnityBridgeConfigTests(unittest.TestCase):
    def _write(self, directory: str, content: str) -> Path:
        path = Path(directory) / "config.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_single_config_is_typed_and_paths_are_config_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_single_run_config(
                self._write(
                    directory,
                    """
resource_root = "assets"
[bridge]
dir = "bridge"
[task]
name = "Task"
account_id = 2
timeout = 0
options = [{ name = "模式", value = "Yes" }]
""",
                )
            )

            self.assertEqual(
                config.bridge_dir, str((Path(directory) / "bridge").resolve())
            )
            self.assertEqual(
                config.resource_root, str((Path(directory) / "assets").resolve())
            )
            self.assertEqual(config.account_id, "2")
            self.assertEqual(config.timeout, 0.0)
            self.assertEqual(config.options, ("模式=Yes",))

    def test_explicit_cli_defaults_still_override_toml(self) -> None:
        args = Namespace(
            bridge_dir=None,
            task=None,
            account_id="0",
            timeout=1200.0,
            stall_timeout=None,
            progress_node=None,
            loop_exempt_node=None,
            recovery_entry=None,
            recovery_retries=None,
            option=[],
            click=None,
            after_click=1.0,
            resource_root="cli-assets",
        )
        config = SingleRunConfig(
            bridge_dir="toml-bridge",
            task_name="TomlTask",
            account_id="3",
            timeout=35.0,
            options=("A=B",),
            after_click=4.0,
            resource_root="toml-assets",
        )

        merged = _apply_single_config(args, config)

        self.assertEqual(merged.account_id, "0")
        self.assertEqual(merged.timeout, 1200.0)
        self.assertEqual(merged.option, [])
        self.assertEqual(merged.after_click, 1.0)
        self.assertEqual(merged.resource_root, "cli-assets")
        self.assertEqual(merged.bridge_dir, "toml-bridge")
        self.assertEqual(merged.task, "TomlTask")

    def test_cli_recovery_retries_require_recovery_entry(self) -> None:
        args = Namespace(
            bridge_dir=None,
            task="Task",
            account_id=None,
            timeout=None,
            stall_timeout=None,
            progress_node=None,
            loop_exempt_node=None,
            recovery_entry=None,
            recovery_retries=3,
            option=None,
            click=None,
            after_click=None,
            resource_root=None,
        )

        with self.assertRaisesRegex(ValueError, "必须配置 recovery_entry"):
            _apply_single_config(args, SingleRunConfig())

    def test_single_rejects_task_and_click_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                """
[task]
name = "Task"
[click]
x = 1
y = 2
""",
            )
            with self.assertRaisesRegex(TomlConfigError, "不能同时配置"):
                load_single_run_config(path)

    def test_single_rejects_unknown_fields_and_partial_clicks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unknown = self._write(directory, "[task]\nname = 'Task'\ntimeot = 3\n")
            with self.assertRaisesRegex(TomlConfigError, "timeot"):
                load_single_run_config(unknown)

            partial = self._write(directory, "[click]\nx = 1\n")
            with self.assertRaisesRegex(TomlConfigError, "必须同时配置"):
                load_single_run_config(partial)

    def test_single_rejects_incomplete_mode_tables(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = self._write(directory, "[task]\ntimeout = 1\n")
            with self.assertRaisesRegex(TomlConfigError, "task.name"):
                load_single_run_config(task)

            click = self._write(directory, "[click]\nafter_click = 1\n")
            with self.assertRaisesRegex(TomlConfigError, "click.x"):
                load_single_run_config(click)

    def test_batch_config_normalizes_empty_bridge_and_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_batch_run_config(
                self._write(
                    directory,
                    """
[bridge]
dir = ""
[global]
account_id = "dev"
default_timeout = 35
continue_on_error = false
[[tasks]]
name = "Task"
enabled = false
timeout = 0
options = [{ name = "选项", value = "值" }]
""",
                )
            )

            self.assertIsNone(config.bridge_dir)
            self.assertEqual(config.account_id, "dev")
            self.assertEqual(config.default_timeout, 35.0)
            self.assertFalse(config.continue_on_error)
            self.assertEqual(config.tasks[0].options, ("选项=值",))
            self.assertEqual(config.tasks[0].timeout, 0.0)

    def test_batch_rejects_invalid_types_before_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                """
[global]
continue_on_error = "yes"
[[tasks]]
name = "Task"
""",
            )
            with self.assertRaisesRegex(TomlConfigError, "布尔值"):
                load_batch_run_config(path)

    def test_batch_requires_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "[global]\ndefault_timeout = 1\n")
            with self.assertRaisesRegex(TomlConfigError, "至少一个"):
                load_batch_run_config(path)

    def test_batch_watchdog_defaults_are_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_batch_run_config(
                self._write(directory, '[[tasks]]\nname = "Task"\n')
            )

        self.assertEqual(config.default_timeout, 600.0)
        self.assertEqual(config.default_stall_timeout, 180.0)

    def test_resource_loader_applies_bridge_overlay_last(self) -> None:
        class Job:
            succeeded = True

            def wait(self) -> "Job":
                return self

        class Resource:
            def __init__(self) -> None:
                self.paths: list[Path] = []

            def post_bundle(self, path: str) -> Job:
                self.paths.append(Path(path))
                return Job()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "resource" / "bridge").mkdir(parents=True)
            resource = Resource()

            self.assertTrue(_load_pipeline_resources(resource, str(root)))
            self.assertEqual(
                [path.name for path in resource.paths], ["base", "pc", "bridge"]
            )

    def test_single_config_parses_stall_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_single_run_config(
                self._write(
                    directory,
                    """
[task]
name = "Task"
timeout = 300
stall_timeout = 90
progress_nodes = ["*Completed"]
loop_exempt_nodes = ["Battle*"]
recovery_entry = "RecoverToHome"
""",
                )
            )
            self.assertEqual(config.timeout, 300.0)
            self.assertEqual(config.stall_timeout, 90.0)
            self.assertEqual(config.progress_nodes, ("*Completed",))
            self.assertEqual(config.loop_exempt_nodes, ("Battle*",))
            self.assertEqual(config.recovery_entry, "RecoverToHome")
            self.assertEqual(config.recovery_retries, 1)

    def test_batch_config_parses_stall_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_batch_run_config(
                self._write(
                    directory,
                    """
[global]
default_timeout = 600
default_stall_timeout = 100
[[tasks]]
name = "Task 1"
stall_timeout = 45
[[tasks]]
name = "Task 2"
""",
                )
            )
            self.assertEqual(config.default_stall_timeout, 100.0)
            self.assertEqual(config.tasks[0].stall_timeout, 45.0)
            self.assertIsNone(config.tasks[1].stall_timeout)

    def test_stall_timeout_fires_when_no_node_activity(self) -> None:
        class PendingJob:
            job_id = 7
            done = False
            succeeded = False

            def get(self) -> None:
                return None

        class StoppedJob:
            done = True
            succeeded = True

        class Tasker:
            stopped = False

            def post_task(self, *args: object, **kwargs: object) -> PendingJob:
                return PendingJob()

            def post_stop(self) -> StoppedJob:
                self.stopped = True
                return StoppedJob()

        tasker = Tasker()
        progress = _ProgressSink()

        exit_code = _run_task_with_timeout(tasker, "Task", {}, 0, 0.01, progress)

        self.assertEqual(exit_code, 129)
        self.assertTrue(tasker.stopped)

    def test_stall_watchdog_skipped_when_zero(self) -> None:
        class DoneJob:
            job_id = 8
            done = True
            succeeded = True

            def get(self) -> None:
                return None

        class Tasker:
            def post_task(self, *args: object, **kwargs: object) -> DoneJob:
                return DoneJob()

            def post_stop(self) -> None:
                pass

        tasker = Tasker()
        progress = _ProgressSink()

        exit_code = _run_task_with_timeout(tasker, "Task", {}, 0, 0, progress)

        self.assertEqual(exit_code, 0)

    def test_progress_is_reset_and_filtered_by_task_id(self) -> None:
        progress = _ProgressSink()
        progress.begin_task(10, 1.0)

        with patch("agent.run_unity_bridge.time.monotonic", return_value=5.0):
            progress.emit(
                "Node.Recognition.Succeeded",
                {"task_id": 9, "name": "OldTask"},
            )
            self.assertEqual(progress.snapshot().last_activity_time, 1.0)

            progress.emit(
                "Node.Recognition.Succeeded",
                {"task_id": 10, "name": "CurrentTask"},
            )

        snapshot = progress.snapshot()
        self.assertEqual(snapshot.last_activity_time, 5.0)
        self.assertEqual(snapshot.phase, "Recognition")
        self.assertEqual(snapshot.phase_started_time, 5.0)
        progress.begin_task(11, 8.0)
        reset = progress.snapshot()
        self.assertEqual(reset.last_activity_time, 8.0)
        self.assertEqual(reset.last_node, "")

    def test_cycle_detector_allows_new_correction_path(self) -> None:
        progress = _ProgressSink()
        progress.prepare_task(0.0)
        progress.begin_task(20, 0.0)

        with patch("agent.run_unity_bridge.time.monotonic", return_value=1.0):
            for name in ("A", "B", "A", "Fix", "A", "C"):
                progress.emit(
                    "Node.PipelineNode.Starting",
                    {"task_id": 20, "name": name},
                )

        self.assertIsNone(progress.snapshot().repeated_transition_since)

    def test_progress_checkpoint_prevents_legitimate_loop_detection(self) -> None:
        progress = _ProgressSink()
        progress.prepare_task(0.0, progress_node_patterns=("LoopDone",))
        progress.begin_task(21, 0.0)

        with patch("agent.run_unity_bridge.time.monotonic", return_value=1.0):
            for _ in range(5):
                for name in ("Work", "LoopDone"):
                    progress.emit(
                        "Node.PipelineNode.Starting",
                        {"task_id": 21, "name": name},
                    )

        self.assertIsNone(progress.snapshot().repeated_transition_since)

    def test_unconfirmed_stop_returns_125(self) -> None:
        class PendingJob:
            job_id = 11
            done = False

        class Tasker:
            def post_task(self, *args: object, **kwargs: object) -> PendingJob:
                return PendingJob()

            def post_stop(self) -> None:
                raise RuntimeError("native stop failed")

        exit_code = _run_task_with_timeout(
            Tasker(), "Task", {}, 0, 0.01, _ProgressSink()
        )

        self.assertEqual(exit_code, 125)

    def test_activity_during_grace_cancels_idle_watchdog(self) -> None:
        class Clock:
            now = 0.0

            def sleep(self, seconds: float) -> None:
                self.now += seconds

        clock = Clock()
        progress = _ProgressSink()

        class ActiveJob:
            job_id = 12
            succeeded = True
            emitted = False

            @property
            def done(self) -> bool:
                if clock.now >= 1.5 and not self.emitted:
                    self.emitted = True
                    progress.emit(
                        "Node.Action.Succeeded",
                        {"task_id": self.job_id, "name": "StillWorking"},
                    )
                return clock.now >= 2.4

            def get(self) -> None:
                return None

        class Tasker:
            stopped = False

            def post_task(self, *args: object, **kwargs: object) -> ActiveJob:
                return ActiveJob()

            def post_stop(self) -> None:
                self.stopped = True

        tasker = Tasker()
        with (
            patch(
                "agent.run_unity_bridge.time.monotonic",
                side_effect=lambda: clock.now,
            ),
            patch("agent.run_unity_bridge.time.sleep", side_effect=clock.sleep),
        ):
            exit_code = _run_task_with_timeout(tasker, "Task", {}, 0, 1.0, progress)

        self.assertEqual(exit_code, 0)
        self.assertFalse(tasker.stopped)

    def test_pending_watchdog_has_distinct_exit_code(self) -> None:
        class PendingJob:
            job_id = 30
            done = False
            pending = True

        class StoppedJob:
            done = True
            succeeded = True

        class Tasker:
            def post_task(self, *args: object, **kwargs: object) -> PendingJob:
                return PendingJob()

            def post_stop(self) -> StoppedJob:
                return StoppedJob()

        exit_code = _run_task_with_timeout(
            Tasker(), "Task", {}, 0, 0.01, _ProgressSink()
        )

        self.assertEqual(exit_code, 126)

    def test_node_watchdog_stops_one_unfinished_node_attempt(self) -> None:
        progress = _ProgressSink()

        class PendingJob:
            job_id = 31
            done = False
            pending = False

        class StoppedJob:
            done = True
            succeeded = True

        class Tasker:
            def post_task(self, *args: object, **kwargs: object) -> PendingJob:
                progress.emit(
                    "Node.PipelineNode.Starting",
                    {"task_id": 31, "name": "HungAction"},
                )
                return PendingJob()

            def post_stop(self) -> StoppedJob:
                return StoppedJob()

        exit_code = _run_task_with_timeout(Tasker(), "Task", {}, 0, 0.01, progress)

        self.assertEqual(exit_code, 127)

    def test_repeated_transition_watchdog_stops_livelock(self) -> None:
        progress = _ProgressSink()

        class PendingJob:
            job_id = 32
            done = False
            pending = False

        class StoppedJob:
            done = True
            succeeded = True

        class Tasker:
            def post_task(self, *args: object, **kwargs: object) -> PendingJob:
                for name in ("A", "B", "A", "B"):
                    progress.emit(
                        "Node.PipelineNode.Starting",
                        {"task_id": 32, "name": name},
                    )
                return PendingJob()

            def post_stop(self) -> StoppedJob:
                return StoppedJob()

        exit_code = _run_task_with_timeout(Tasker(), "Task", {}, 0, 0.01, progress)

        self.assertEqual(exit_code, 128)

    def test_supervisor_runs_recovery_then_retries_original_entry(self) -> None:
        first_tasker = object()
        recovered_tasker = object()
        create_tasker = Mock(return_value=recovered_tasker)

        with (
            patch(
                "agent.run_unity_bridge._run_task_with_timeout",
                side_effect=[128, 0, 0],
            ) as run,
            patch(
                "agent.run_unity_bridge._attach_progress_sink",
                return_value=_ProgressSink(),
            ),
        ):
            exit_code, final_tasker, _ = _run_task_with_recovery(
                first_tasker,
                _ProgressSink(),
                create_tasker,
                "MainEntry",
                {"override": {}},
                timeout_seconds=600,
                stall_timeout_seconds=180,
                recovery_entry="RecoveryEntry",
                recovery_retries=1,
            )

        self.assertEqual(exit_code, 0)
        self.assertIs(final_tasker, recovered_tasker)
        self.assertEqual(create_tasker.call_count, 1)
        self.assertEqual(
            [call.args[1] for call in run.call_args_list],
            ["MainEntry", "RecoveryEntry", "MainEntry"],
        )

    def test_ctrl_c_stops_active_task(self) -> None:
        class InterruptedJob:
            job_id = 9

            @property
            def done(self) -> bool:
                raise KeyboardInterrupt

        class StoppedJob:
            done = True
            succeeded = True

        class Tasker:
            stopped = False

            def post_task(self, *args: object, **kwargs: object) -> InterruptedJob:
                return InterruptedJob()

            def post_stop(self) -> StoppedJob:
                self.stopped = True
                return StoppedJob()

        tasker = Tasker()
        progress = _ProgressSink()

        exit_code = _run_task_with_timeout(tasker, "Task", {}, 10, 0.0, progress)

        self.assertEqual(exit_code, 130)
        self.assertTrue(tasker.stopped)

    def test_ctrl_c_exit_code_stops_batch_even_when_continue_is_enabled(self) -> None:
        runner = BatchTaskRunner(
            controller=None,
            resource=None,
            default_timeout=10,
            continue_on_error=True,
        )
        runner._create_tasker = lambda: object()  # type: ignore[method-assign]
        tasks = (BatchTaskConfig("Task 1"), BatchTaskConfig("Task 2"))

        with (
            patch("agent.run_batch_tasks.bridge._attach_progress_sink") as attach,
            patch(
                "agent.run_batch_tasks._resolve_task",
                return_value=("Task", "Entry", {}),
            ),
            patch(
                "agent.run_batch_tasks._run_task_with_recovery",
                return_value=(130, object(), _ProgressSink()),
            ) as run,
        ):
            attach.return_value = Namespace(last_node="")
            stats = runner.run_tasks("assets", tasks)

        run.assert_called_once()
        self.assertEqual(stats["interrupted"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["not_run"], 1)
        self.assertEqual(
            [result["status"] for result in stats["results"]],
            ["interrupted", "not_run"],
        )

    def test_batch_recreates_tasker_after_confirmed_timeout(self) -> None:
        runner = BatchTaskRunner(
            controller=None,
            resource=None,
            default_timeout=10,
            continue_on_error=True,
        )
        runner._create_tasker = Mock(side_effect=[object(), object()])
        tasks = (BatchTaskConfig("Task 1"), BatchTaskConfig("Task 2"))

        with (
            patch("agent.run_batch_tasks.bridge._attach_progress_sink") as attach,
            patch(
                "agent.run_batch_tasks._resolve_task",
                return_value=("Task", "Entry", {}),
            ),
            patch(
                "agent.run_batch_tasks._run_task_with_recovery",
                side_effect=[
                    (124, object(), _ProgressSink()),
                    (0, object(), _ProgressSink()),
                ],
            ),
        ):
            attach.side_effect = [_ProgressSink(), _ProgressSink()]
            stats = runner.run_tasks("assets", tasks)

        self.assertEqual(runner._create_tasker.call_count, 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["success"], 1)

    def test_batch_stops_after_unconfirmed_stop(self) -> None:
        runner = BatchTaskRunner(
            controller=None,
            resource=None,
            default_timeout=10,
            continue_on_error=True,
        )
        runner._create_tasker = Mock(return_value=object())
        tasks = (BatchTaskConfig("Task 1"), BatchTaskConfig("Task 2"))

        with (
            patch("agent.run_batch_tasks.bridge._attach_progress_sink") as attach,
            patch(
                "agent.run_batch_tasks._resolve_task",
                return_value=("Task", "Entry", {}),
            ),
            patch(
                "agent.run_batch_tasks._run_task_with_recovery",
                return_value=(125, object(), _ProgressSink()),
            ) as run,
        ):
            attach.return_value = _ProgressSink()
            stats = runner.run_tasks("assets", tasks)

        run.assert_called_once()
        self.assertEqual(runner._create_tasker.call_count, 1)
        self.assertEqual(stats["not_run"], 1)
        self.assertEqual(stats["results"][0]["reason"], "stop_unconfirmed")


if __name__ == "__main__":
    unittest.main()
