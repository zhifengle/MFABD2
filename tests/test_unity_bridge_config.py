from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from agent.run_unity_bridge import (
    _apply_single_config,
    _load_pipeline_resources,
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

    def test_ctrl_c_stops_active_task(self) -> None:
        class InterruptedJob:
            @property
            def done(self) -> bool:
                raise KeyboardInterrupt

        class StoppedJob:
            done = True

        class Tasker:
            stopped = False

            def post_task(self, *args: object, **kwargs: object) -> InterruptedJob:
                return InterruptedJob()

            def post_stop(self) -> StoppedJob:
                self.stopped = True
                return StoppedJob()

        tasker = Tasker()
        progress = Namespace(last_node="")

        exit_code = _run_task_with_timeout(tasker, "Task", {}, 10, progress)

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
                "agent.run_batch_tasks._run_task_with_timeout", return_value=130
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


if __name__ == "__main__":
    unittest.main()
