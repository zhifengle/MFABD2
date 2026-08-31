"""Inspect or run one effective MaaFW node through Unity Bridge.

The tool loads resources in production order (base -> pc -> bridge). Its default
``recognition`` mode replaces the target action with ``DoNothing`` and removes
all successors, making it the safest live diagnostic mode.

Examples:
    uv run scripts/unity_bridge_node_lab.py NODE --mode inspect
    uv run scripts/unity_bridge_node_lab.py NODE
    uv run scripts/unity_bridge_node_lab.py NODE --mode isolated
    uv run scripts/unity_bridge_node_lab.py NODE --mode flow --timeout 10
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parents[1]
AGENT_ROOT = PROJECT_ROOT / "agent"
DEFAULT_RESOURCE_ROOT = PROJECT_ROOT / "assets"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / ".tmp" / "unity_bridge_artifacts"

for path in (PROJECT_ROOT, AGENT_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

os.environ.setdefault("PYTHONUTF8", "1")

import run_unity_bridge as bridge  # noqa: E402
from utils import mfaalog  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过 Unity Bridge 检查或执行合并资源中的任意 MaaFW node"
    )
    parser.add_argument("node", help="pipeline node 名称")
    parser.add_argument(
        "--mode",
        choices=("recognition", "isolated", "flow", "inspect"),
        default="recognition",
        help=(
            "recognition=只识别；isolated=执行动作但不走后继；"
            "flow=生产后继链；inspect=只查看合并配置"
        ),
    )
    parser.add_argument(
        "--timeout", type=bridge._non_negative_float, default=15.0, help="超时秒数"
    )
    parser.add_argument("--account-id", default="0", help="PersistentStore 账号 ID")
    parser.add_argument("--bridge-dir", help="Bridge 插件目录；默认自动发现")
    parser.add_argument(
        "--resource-root", default=str(DEFAULT_RESOURCE_ROOT), help="资源根目录"
    )
    parser.add_argument("--override-file", help="额外 pipeline override JSON")
    parser.add_argument(
        "--artifact-root",
        default=str(DEFAULT_ARTIFACT_ROOT),
        help="记录输出目录",
    )
    parser.add_argument("--no-artifacts", action="store_true", help="不保存测试记录")
    return parser.parse_args()


def deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def load_override(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("override 文件顶层必须是 JSON object")
    return value


def build_override(
    node: str, mode: str, extra_override: dict[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(extra_override)
    if mode == "recognition":
        deep_merge(
            result,
            {node: {"action": "DoNothing", "next": [], "on_error": [], "max_hit": 1}},
        )
    elif mode == "isolated":
        deep_merge(result, {node: {"next": [], "on_error": [], "max_hit": 1}})
    return result


def make_artifact_dir(root: Path, node: str, enabled: bool) -> Path | None:
    if not enabled:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_node = re.sub(r"[^0-9A-Za-z_.-]+", "_", node).strip("_") or "node"
    output = root / f"{stamp}_{safe_node}"
    output.mkdir(parents=True, exist_ok=False)
    return output


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def save_screenshot(controller: Any, path: Path) -> None:
    image = controller.post_screencap().wait().get()
    if image is None:
        raise RuntimeError("截图返回 None")
    Image.fromarray(image[:, :, ::-1]).save(path)


class EventRecorder:
    """Collect relevant MaaFW events while satisfying the timeout helper API."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.last_node = ""
        self._lock = threading.Lock()

    def record(self, message: str, details: dict[str, Any]) -> None:
        if not message.startswith(
            ("Node.PipelineNode.", "Node.Action.", "Node.WaitFreezes.")
        ):
            return
        name = str(details.get("name", "?"))
        event = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "message": message,
            "name": name,
            "state": message.rsplit(".", 1)[-1],
            "focus": details.get("focus", ""),
        }
        with self._lock:
            self.events.append(event)
            if message.startswith("Node.PipelineNode."):
                self.last_node = name
        mfaalog.info(f"[实验台] {name}: {event['state']}")


def attach_recorder(tasker: Any, recorder: EventRecorder) -> Any:
    from maa.context import ContextEventSink

    class Sink(ContextEventSink):
        def on_raw_notification(self, context, message, details):
            del context
            recorder.record(message, details)

    sink = Sink()
    tasker.add_context_sink(sink)
    return sink


def main() -> int:
    args = parse_args()
    resource_root = Path(args.resource_root).expanduser().resolve()
    artifact_dir = make_artifact_dir(
        Path(args.artifact_root).expanduser().resolve(),
        args.node,
        not args.no_artifacts,
    )

    from maa.resource import Resource
    from utils.standalone_runtime import register_standalone_extensions

    resource = Resource()
    register_standalone_extensions(resource)
    if not bridge._load_pipeline_resources(resource, str(resource_root)):
        raise RuntimeError("资源加载失败")

    extra_override = load_override(args.override_file)
    node_in_resource = args.node in resource.node_list
    node_in_override = args.node in extra_override
    if not node_in_resource and not node_in_override:
        raise ValueError(f"资源和 override 中均不存在 node: {args.node}")

    node_data = (
        resource.get_node_data(args.node)
        if node_in_resource
        else copy.deepcopy(extra_override[args.node])
    )
    pipeline_override = build_override(args.node, args.mode, extra_override)
    if artifact_dir:
        write_json(artifact_dir / "node.merged.json", node_data)
        write_json(artifact_dir / "pipeline_override.json", pipeline_override)

    if args.mode == "inspect":
        print(json.dumps(node_data, ensure_ascii=False, indent=2, default=str))
        if artifact_dir:
            print(f"artifacts: {artifact_dir}")
        return 0

    from controller.unity_win32_controller import UnityWin32Controller
    from maa.tasker import Tasker
    from utils.persistent_store import PersistentStore

    PersistentStore.switch_account(args.account_id)
    PersistentStore.load()
    bridge_dir, class_regex, window_regex = bridge._resolve_bridge_dir(
        str(resource_root), args.bridge_dir
    )
    controller = UnityWin32Controller(
        bridge_dir,
        window_class=class_regex,
        window_title=window_regex,
    )
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("Unity Bridge 控制器连接失败")
    if artifact_dir:
        save_screenshot(controller, artifact_dir / "before.png")

    tasker = Tasker()
    if not tasker.bind(resource, controller) or not tasker.inited:
        raise RuntimeError("Tasker 绑定或初始化失败")
    recorder = EventRecorder()
    sink = attach_recorder(tasker, recorder)
    exit_code = bridge._run_task_with_timeout(
        tasker,
        args.node,
        pipeline_override,
        args.timeout,
        recorder,
    )

    if artifact_dir:
        try:
            save_screenshot(controller, artifact_dir / "after.png")
        except Exception as error:
            (artifact_dir / "after.error.txt").write_text(str(error), encoding="utf-8")
        write_json(artifact_dir / "events.json", recorder.events)
        write_json(
            artifact_dir / "result.json",
            {
                "node": args.node,
                "mode": args.mode,
                "exit_code": exit_code,
                "last_node": recorder.last_node,
                "bridge_dir": bridge_dir,
            },
        )
    del sink
    print(
        f"result: {'PASS' if exit_code == 0 else 'FAIL'}; "
        f"node={args.node}; mode={args.mode}; last_node={recorder.last_node or '-'}"
    )
    if artifact_dir:
        print(f"artifacts: {artifact_dir}")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
