"""unity-automation-bridge 文件协议 Python 客户端

封装 BepInEx 插件 unity-automation-bridge 的文件请求/响应协议。
插件在游戏进程内通过 EventSystem.RaycastAll 派发点击/拖拽事件，
通过独立虚拟 Keyboard 发送按键，不激活窗口、不移动系统鼠标。

协议：客户端将 UTF-8 key=value 请求文件原子移动到
BepInEx/plugins/UnityAutomationBridge/requests/<id>.request，
插件轮询并写 responses/<id>.response。

协议实现与行为依据：E:\\AHK\\game_tools\\unity-automation-bridge\\README.md
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# -- VK → Unity Key 映射 ------------------------------------------------------

# Windows 虚拟键码 → Unity InputSystem Key 枚举名（大小写不敏感）
# bridge 接受 Unity Key 枚举名，如 "j"、"escape"、"digit2"、"leftCtrl"
VK_TO_UNITY_KEY: dict[int, str] = {}

# 字母 VK_A(0x41) – VK_Z(0x5A) → a – z
for _i in range(26):
    VK_TO_UNITY_KEY[0x41 + _i] = chr(ord("a") + _i)

# 主键盘数字 VK_0(0x30) – VK_9(0x39) → digit0 – digit9
for _i in range(10):
    VK_TO_UNITY_KEY[0x30 + _i] = f"digit{_i}"

# 功能键 VK_F1(0x70) – VK_F12(0x7B) → f1 – f12
for _i in range(12):
    VK_TO_UNITY_KEY[0x70 + _i] = f"f{_i + 1}"

# 控制键
VK_TO_UNITY_KEY.update(
    {
        0x08: "backspace",
        0x09: "tab",
        0x0D: "enter",
        0x10: "leftShift",  # Shift（不分左右时映射到 left）
        0x11: "leftCtrl",   # Ctrl
        0x12: "leftAlt",    # Alt
        0x13: "pause",
        0x14: "capsLock",
        0x1B: "escape",
        0x20: "space",
        0x21: "pageUp",
        0x22: "pageDown",
        0x23: "end",
        0x24: "home",
        0x25: "leftArrow",
        0x26: "upArrow",
        0x27: "rightArrow",
        0x28: "downArrow",
        0x2D: "insert",
        0x2E: "delete",
        # 区分左右修饰键
        0xA0: "leftShift",
        0xA1: "rightShift",
        0xA2: "leftCtrl",
        0xA3: "rightCtrl",
        0xA4: "leftAlt",
        0xA5: "rightAlt",
    }
)


def vk_to_unity_key(vk_code: int) -> Optional[str]:
    """将 Windows 虚拟键码翻译为 Unity Key 枚举名。

    返回 None 表示未知键码，调用方应记录警告并跳过。
    """
    return VK_TO_UNITY_KEY.get(vk_code)


# -- Bridge 客户端 -------------------------------------------------------------


class UnityBridgeClient:
    """unity-automation-bridge 文件协议客户端。

    构造参数为 bridge 插件目录路径，即
    ``<GameDir>/BepInEx/plugins/UnityAutomationBridge``。
    该目录下应有 ``requests/`` 和 ``responses/`` 子目录（构造时自动创建）。
    """

    PROTOCOL_VERSION = "2"
    POLL_INTERVAL_MS = 20
    DEFAULT_TIMEOUT_MS = 1500

    # no-click-handler 重试：Unity UI 的 IPointerClickHandler 注册可能晚于
    # 视觉渲染（OCR/ColorMatch 通过 ≠ 可点击）。收到 no-click-handler 时
    # 短暂等待后重试，给交互组件初始化时间。重试不改变最终失败语义——
    # 仍然返回 False，只是给了更多机会。不属于"全局吞掉"（见
    # architecture-and-decisions.md §Bridge 保持严格点击失败）。
    NO_CLICK_HANDLER_RETRIES = 2
    NO_CLICK_HANDLER_RETRY_DELAY_S = 0.15

    # 坐标基准（与项目 display_short_side=720 对齐）
    REFERENCE_WIDTH = 1280
    REFERENCE_HEIGHT = 720

    def __init__(self, bridge_dir: str):
        self._bridge_dir = os.path.abspath(bridge_dir)
        self._request_dir = os.path.join(self._bridge_dir, "requests")
        self._response_dir = os.path.join(self._bridge_dir, "responses")
        self._last_result: dict[str, str] = {}
        os.makedirs(self._request_dir, exist_ok=True)
        os.makedirs(self._response_dir, exist_ok=True)

    @property
    def bridge_dir(self) -> str:
        """返回当前客户端使用的 Bridge 插件目录。"""
        return self._bridge_dir

    @property
    def last_result(self) -> dict[str, str]:
        """返回最近一次请求的响应和客户端诊断元数据副本。"""
        return dict(self._last_result)

    def describe_last_result(self) -> str:
        """把最近一次请求压缩为适合单行日志的诊断文本。"""
        if not self._last_result:
            return "no-result"

        preferred_keys = (
            "_action",
            "id",
            "ok",
            "status",
            "message",
            "_elapsedMs",
            "_timeoutMs",
            "_requestState",
            "_request",
            "screenWidth",
            "screenHeight",
            "unityX",
            "unityY",
            "endUnityX",
            "endUnityY",
            "hitCount",
            "targetPath",
            "triggeredActions",
        )
        parts = []
        for key in preferred_keys:
            value = self._last_result.get(key)
            if value not in (None, ""):
                label = key[1:] if key.startswith("_") else key
                parts.append(f"{label}={value!r}")
        return " ".join(parts) or "empty-result"

    def _record_result(
        self,
        *,
        action: str,
        req_id: str,
        params: list[str],
        timeout_ms: int,
        started_at: float,
        response: dict[str, str],
        request_state: str = "",
    ) -> None:
        result = dict(response)
        result.setdefault("id", req_id)
        result["_action"] = action
        result["_elapsedMs"] = str(
            max(0, round((time.monotonic() - started_at) * 1000))
        )
        result["_timeoutMs"] = str(timeout_ms)
        result["_request"] = ",".join(params)
        if request_state:
            result["_requestState"] = request_state
        self._last_result = result

    # -- 核心协议 -------------------------------------------------------------

    def _send_request(
        self,
        action: str,
        params: list[str],
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, str]:
        """发送文件请求并等待响应。

        构造 key=value 行列表，写临时文件，os.replace 原子移动到
        requests/<id>.request，轮询 responses/<id>.response（20ms 间隔），
        解析返回字典。超时时清理请求文件。

        Returns:
            响应字典。``ok`` 字段为 ``"1"`` 表示成功。
        Raises:
            TimeoutError: 请求超时。
        """
        req_id = self._make_id()
        started_at = time.monotonic()
        max_age = max(timeout_ms + 500, 1000)

        lines = [
            f"protocol={self.PROTOCOL_VERSION}",
            f"id={req_id}",
            f"action={action}",
            f"maxAgeMs={max_age}",
        ]
        lines.extend(params)

        encoding = "utf-8"
        tmp_path = os.path.join(self._request_dir, f"{req_id}.tmp")
        req_path = os.path.join(self._request_dir, f"{req_id}.request")
        resp_path = os.path.join(self._response_dir, f"{req_id}.response")

        with open(tmp_path, "w", encoding=encoding, newline="") as f:
            f.write("\n".join(lines))
        os.replace(tmp_path, req_path)

        deadline = time.monotonic() + timeout_ms / 1000.0
        while not os.path.exists(resp_path):
            if time.monotonic() >= deadline:
                processing_path = req_path + ".processing"
                if os.path.exists(req_path):
                    request_state = "pending"
                elif os.path.exists(processing_path):
                    request_state = "processing"
                else:
                    request_state = "consumed-or-missing"
                message = (
                    f"Timed out after {timeout_ms} ms waiting for Unity bridge response"
                )
                self._record_result(
                    action=action,
                    req_id=req_id,
                    params=params,
                    timeout_ms=timeout_ms,
                    started_at=started_at,
                    response={
                        "ok": "0",
                        "status": "timeout",
                        "message": message,
                    },
                    request_state=request_state,
                )
                # 清理残留请求文件
                try:
                    os.remove(req_path)
                except OSError:
                    pass
                raise TimeoutError(
                    f"Timed out waiting for Unity bridge response: {req_id}"
                )
            time.sleep(self.POLL_INTERVAL_MS / 1000.0)

        # 解析响应
        response: dict[str, str] = {}
        with open(resp_path, "r", encoding=encoding) as f:
            for line in f:
                sep = line.find("=")
                if sep > 0:
                    response[line[:sep]] = line[sep + 1:].rstrip("\r\n")
                elif sep == 0:
                    logger.warning("Invalid response line (empty key): %r", line)

        # 清理响应文件
        try:
            os.remove(resp_path)
        except OSError:
            pass

        self._record_result(
            action=action,
            req_id=req_id,
            params=params,
            timeout_ms=timeout_ms,
            started_at=started_at,
            response=response,
            request_state="responded",
        )

        return response

    @staticmethod
    def _make_id() -> str:
        """生成唯一请求 ID（时间戳 + PID + UUID，对齐 request.ps1 格式）。"""
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        return f"{ts}-{os.getpid()}-{uuid.uuid4().hex}"

    def _is_ok(self, response: dict[str, str]) -> bool:
        return response.get("ok") == "1"

    # -- 公开 API -------------------------------------------------------------

    def ping(self, timeout_ms: int = 1500) -> bool:
        """发送 action=ping，确认插件主线程桥已就绪。"""
        try:
            resp = self._send_request("ping", [], timeout_ms)
        except TimeoutError:
            return False
        return self._is_ok(resp)

    def input_info(self, timeout_ms: int = 1500) -> dict[str, str]:
        """发送 action=input-info，查询输入能力。"""
        return self._send_request("input-info", [], timeout_ms)

    def click(
        self,
        x: float,
        y: float,
        *,
        button: str = "left",
        timeout_ms: int = 1500,
    ) -> bool:
        """发送 action=click，由插件选择 uGUI 或 BD2 TouchPad 输入路径。

        坐标以 1280×720 为基准，附带 referenceWidth/referenceHeight
        由 bridge 缩放到游戏实际内部分辨率。

        收到 ``no-click-handler`` 时自动重试至多
        ``NO_CLICK_HANDLER_RETRIES`` 次：Unity UI 的 IPointerClickHandler
        注册可能晚于视觉渲染，短暂等待后重试通常能命中。
        其他失败（超时、插件错误等）不重试，立即返回 False。
        """
        params = [
            "space=pixels-top-left",
            f"button={button}",
            "logHits=0",
            f"x={x}",
            f"y={y}",
            f"referenceWidth={self.REFERENCE_WIDTH}",
            f"referenceHeight={self.REFERENCE_HEIGHT}",
        ]
        max_attempts = 1 + self.NO_CLICK_HANDLER_RETRIES
        for attempt in range(max_attempts):
            try:
                resp = self._send_request("click", params, timeout_ms)
            except TimeoutError:
                return False
            # click 响应用 "status" 字段而非通用的 "ok" 字段（Bridge 协议设计）
            status = resp.get("status")
            if status == "clicked":
                return True
            if status != "no-click-handler":
                return False
            if attempt < self.NO_CLICK_HANDLER_RETRIES:
                logger.debug(
                    "click(%.1f, %.1f): no-click-handler (attempt %d/%d), "
                    "retrying in %dms",
                    x, y, attempt + 1, max_attempts,
                    int(self.NO_CLICK_HANDLER_RETRY_DELAY_S * 1000),
                )
                time.sleep(self.NO_CLICK_HANDLER_RETRY_DELAY_S)
        return False

    def hold(
        self,
        x: float,
        y: float,
        duration_ms: int = 500,
        *,
        button: str = "left",
        timeout_ms: int = 2000,
    ) -> bool:
        """发送 action=hold，进程内 PointerDown → 等待 → PointerUp。"""
        params = [
            "space=pixels-top-left",
            f"button={button}",
            "logHits=0",
            f"x={x}",
            f"y={y}",
            f"durationMs={duration_ms}",
            f"referenceWidth={self.REFERENCE_WIDTH}",
            f"referenceHeight={self.REFERENCE_HEIGHT}",
        ]
        timeout_ms = max(timeout_ms, duration_ms + 1000)
        try:
            resp = self._send_request("hold", params, timeout_ms)
        except TimeoutError:
            return False
        return self._is_ok(resp)

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int = 500,
        *,
        end_hold_ms: int = 0,
        steps: int = 15,
        button: str = "left",
        timeout_ms: int = 2000,
    ) -> bool:
        """发送 action=swipe，进程内 PointerDown → Drag → PointerUp。"""
        params = [
            "space=pixels-top-left",
            f"button={button}",
            "logHits=0",
            f"fromX={x1}",
            f"fromY={y1}",
            f"toX={x2}",
            f"toY={y2}",
            f"durationMs={duration_ms}",
            f"endHoldMs={end_hold_ms}",
            f"steps={steps}",
            f"referenceWidth={self.REFERENCE_WIDTH}",
            f"referenceHeight={self.REFERENCE_HEIGHT}",
        ]
        timeout_ms = max(timeout_ms, duration_ms + end_hold_ms + 1000)
        try:
            resp = self._send_request("swipe", params, timeout_ms)
        except TimeoutError:
            return False
        return self._is_ok(resp)

    def press_key(
        self,
        keys: list[str],
        hold_ms: int = 80,
        *,
        timeout_ms: int = 1500,
    ) -> bool:
        """发送 action=key，进程内虚拟键盘按下 → 保持 → 释放。

        Args:
            keys: Unity Key 枚举名列表，如 ["j"] 或 ["leftCtrl", "backquote"]。
            hold_ms: 按键保持时间（20–2000 ms）。
        """
        if not keys:
            return False
        # 校验键名不含非法字符
        for k in keys:
            if not k or any(c in k for c in ",=\r\n"):
                raise ValueError(f"Invalid key name: {k!r}")
        params = [
            f"keys={','.join(keys)}",
            "mode=press",
            f"holdMs={hold_ms}",
            "observeActions=0",
        ]
        timeout_ms = max(timeout_ms, hold_ms + 1000)
        try:
            resp = self._send_request("key", params, timeout_ms)
        except TimeoutError:
            return False
        return self._is_ok(resp)
