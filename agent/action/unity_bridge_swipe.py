"""Unity Bridge 专用、保留 end_hold 语义的滑动动作。"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from agent.utils.unity_bridge import UnityBridgeClient
from utils import mfaalog


_SWIPE_KEYS = frozenset(
    {
        "action",
        "begin",
        "end",
        "duration",
        "end_hold",
        "steps",
        "contact",
        "pressure",
    }
)


def _parse_non_negative_int(params: dict[str, Any], key: str, default: int) -> int:
    value = params.get(key, default)
    if isinstance(value, list):
        if not value:
            value = default
        else:
            value = random.choice(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 必须是非负整数")
    result = int(value)
    if result < 0 or result != value:
        raise ValueError(f"{key} 必须是非负整数")
    return result


def _parse_point(params: dict[str, Any], key: str) -> tuple[int, int]:
    value = params.get(key)
    if (
        key == "end"
        and isinstance(value, list)
        and value
        and isinstance(value[0], (list, tuple))
    ):
        value = random.choice(value)
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 4):
        raise ValueError(f"{key} 必须是 [x, y] 或 [x, y, w, h]")
    result = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise ValueError(f"{key} 必须是 [x, y]")
        number = int(coordinate)
        if number != coordinate:
            raise ValueError(f"{key} 坐标必须是整数")
        result.append(number)
    if len(result) == 2:
        return result[0], result[1]
    x, y, width, height = result
    if width < 1 or height < 1:
        raise ValueError(f"{key} 的 w/h 必须大于 0")
    return random.randrange(x, x + width), random.randrange(y, y + height)


def _bridge_info(context: Context) -> dict[str, Any] | None:
    try:
        info = context.tasker.controller.info
    except (RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    value = info.get("unity_bridge")
    if not isinstance(value, dict) or not value.get("protocol_end_hold"):
        return None
    return value


def bridge_swipe_override(
    context: Context,
    override: dict[str, Any],
) -> dict[str, Any] | None:
    """把原生 Swipe override 转成 Bridge CustomAction；非 Bridge 返回 None。"""
    if override.get("action") != "Swipe" or not override.get("end_hold"):
        return None
    if _bridge_info(context) is None:
        return None

    params = {key: deepcopy(override[key]) for key in _SWIPE_KEYS if key in override}
    params.pop("action", None)
    result = {
        key: deepcopy(value)
        for key, value in override.items()
        if key not in _SWIPE_KEYS
    }
    result.update(
        {
            "action": "Custom",
            "custom_action": "UnityBridgeSwipe",
            "custom_action_param": params,
        }
    )
    return result


@AgentServer.custom_action("UnityBridgeSwipe")
class UnityBridgeSwipe(CustomAction):
    """绕过 MaaFW Custom Controller 的精简 swipe ABI，直达 Bridge 协议。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            raw = argv.custom_action_param
            params = raw if isinstance(raw, dict) else json.loads(str(raw).strip())
            if not isinstance(params, dict):
                raise ValueError("custom_action_param 必须是 object")

            x1, y1 = _parse_point(params, "begin")
            x2, y2 = _parse_point(params, "end")
            duration = _parse_non_negative_int(params, "duration", 500)
            end_hold = _parse_non_negative_int(params, "end_hold", 0)
            steps = _parse_non_negative_int(params, "steps", 15)
            if steps < 1:
                raise ValueError("steps 必须大于 0")

            bridge_info = _bridge_info(context)
            bridge_dir = bridge_info.get("directory") if bridge_info else None
            if not isinstance(bridge_dir, str) or not bridge_dir:
                raise RuntimeError("当前控制器未暴露 Unity Bridge 目录")

            client = UnityBridgeClient(bridge_dir)
            mfaalog.info(
                f"[UnityBridgeSwipe] ({x1},{y1}) → ({x2},{y2}), "
                f"duration={duration}ms, end_hold={end_hold}ms, steps={steps}"
            )
            ok = client.swipe(
                x1,
                y1,
                x2,
                y2,
                duration_ms=duration,
                end_hold_ms=end_hold,
                steps=steps,
            )
            if not ok:
                mfaalog.error(
                    f"[UnityBridgeSwipe] 滑动失败: {client.describe_last_result()}"
                )
            return ok
        except (json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError) as error:
            mfaalog.error(f"[UnityBridgeSwipe] 参数或执行错误: {error}")
            return False
