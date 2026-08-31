"""MaaFramework Win32 图像 + Unity Automation Bridge 输入的混合控制器。

截图完全委托给项目原本使用的 MaaFramework ``Win32Controller``
（PrintWindow、短边 720）；点击、滑动和键盘才走 Unity Automation Bridge。

独立 CLI 直接创建本控制器，不依赖 GUI 或项目 Controller 工厂。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy
from maa.controller import CustomController, Win32Controller
from maa.define import (
    MaaControllerFeatureEnum,
    MaaWin32InputMethodEnum,
    MaaWin32ScreencapMethodEnum,
)

from agent.utils.unity_bridge import UnityBridgeClient, vk_to_unity_key

logger = logging.getLogger(__name__)

# 游戏窗口标识（与 pc_window.py / interface.json win32 配置一致）
WINDOW_CLASS = "UnityWndClass"
WINDOW_TITLE = "BrownDust"


# -- 窗口查找 -----------------------------------------------------------------

def find_game_hwnd(
    class_regex: str = WINDOW_CLASS,
    window_regex: str = WINDOW_TITLE,
) -> int:
    """按 interface.json 的 Win32 正则查找可见顶层窗口。

    与 pc_window._find_game_hwnd 逻辑一致，独立实现以避免 import pc_window
    触发 AgentServer 装饰器注册的副作用。
    """
    if sys.platform != "win32":
        return 0

    user32 = ctypes.windll.user32
    class_pattern = re.compile(class_regex)
    window_pattern = re.compile(window_regex)
    found = ctypes.wintypes.HWND(0)

    def enum_callback(hwnd, _lparam):
        nonlocal found
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if class_pattern.search(buf.value) and user32.IsWindowVisible(hwnd):
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            if window_pattern.search(title_buf.value):
                found = hwnd
                return False  # 停止枚举
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
    return int(found) if found else 0


def get_window_process_path(hwnd: int) -> Path:
    """返回窗口所属进程的可执行文件路径。"""
    if sys.platform != "win32" or not hwnd:
        raise RuntimeError("仅 Windows 有效，且 hwnd 不能为空")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = ctypes.wintypes.DWORD(0)
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        raise RuntimeError("无法获取游戏窗口 PID")

    process_query_limited_information = 0x1000
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPWSTR,
        ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

    handle = kernel32.OpenProcess(
        process_query_limited_information, False, pid.value
    )
    if not handle:
        raise RuntimeError(f"无法打开游戏进程 PID={pid.value}")
    try:
        capacity = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(capacity)
        ):
            raise RuntimeError(f"无法查询游戏进程路径 PID={pid.value}")
        return Path(buffer.value).resolve()
    finally:
        kernel32.CloseHandle(handle)


def discover_bridge_dir(
    class_regex: str = WINDOW_CLASS,
    window_regex: str = WINDOW_TITLE,
) -> Path:
    """由游戏窗口定位 exe，并拼接已安装的 bridge 插件目录。"""
    hwnd = find_game_hwnd(class_regex, window_regex)
    if not hwnd:
        raise RuntimeError(
            "未找到游戏窗口: "
            f"class_regex={class_regex!r}, window_regex={window_regex!r}"
        )
    return bridge_dir_from_hwnd(hwnd)


def bridge_dir_from_hwnd(hwnd: int) -> Path:
    """由已选中的游戏窗口定位 bridge 插件目录。"""
    executable = get_window_process_path(hwnd)
    bridge_dir = executable.parent / "BepInEx" / "plugins" / "UnityAutomationBridge"
    if not bridge_dir.is_dir():
        raise RuntimeError(
            f"已找到游戏进程 {executable}，但 bridge 目录不存在: {bridge_dir}"
        )
    return bridge_dir


def _find_and_close_window() -> tuple[bool, str]:
    """查找游戏窗口并关闭（WM_CLOSE → TerminateProcess 兜底）。

    与 pc_window._find_and_close_window 逻辑一致。
    """
    if sys.platform != "win32":
        return True, "非Windows平台，跳过关闭游戏"

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD
    ]
    kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
    ]

    hwnd = find_game_hwnd()
    if not hwnd:
        return True, "未找到游戏窗口，可能已关闭"

    title_buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title_buf, 256)
    title = title_buf.value

    WM_CLOSE = 0x0010
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.time() + 2.5
    while time.time() < deadline:
        time.sleep(0.1)
        if not find_game_hwnd():
            return True, f"窗口 '{title}' 已通过 WM_CLOSE 关闭"

    pid = ctypes.wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False, f"窗口 '{title}' 未响应 WM_CLOSE 且拿不到 PID"

    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid.value)
    if not handle:
        return False, f"窗口 '{title}' OpenProcess 失败"
    success = bool(kernel32.TerminateProcess(handle, 1))
    kernel32.CloseHandle(handle)
    if success:
        return True, f"窗口 '{title}' 已通过 TerminateProcess 强关"
    return False, f"窗口 '{title}' TerminateProcess 失败"


# -- 控制器 -------------------------------------------------------------------

class UnityWin32Controller(CustomController):
    """混合控制器：Maa Win32 截图，Unity bridge 输入。

    Args:
        bridge_dir: unity-automation-bridge 插件目录路径，即
            ``<GameDir>/BepInEx/plugins/UnityAutomationBridge``。
        window_class: 游戏窗口类名正则（默认 ``UnityWndClass``）。
        window_title: 游戏窗口标题子串（默认 ``BrownDust``）。
    """

    def __init__(
        self,
        bridge_dir: str,
        *,
        hwnd: int = 0,
        window_class: str = WINDOW_CLASS,
        window_title: str = WINDOW_TITLE,
    ):
        self._bridge = UnityBridgeClient(bridge_dir)
        self._window_class = window_class
        self._window_title = window_title
        self._requested_hwnd = int(hwnd)
        self._hwnd: int = 0
        self._vision: Win32Controller | None = None
        self._connected: bool = False  # 跟踪连接状态
        super().__init__()

    # -- 连接与生命周期 -------------------------------------------------------

    def connect(self) -> bool:
        """连接项目原生 Win32 截图控制器，并确认 bridge 输入可用。"""
        requested_is_valid = bool(
            sys.platform == "win32"
            and self._requested_hwnd
            and ctypes.windll.user32.IsWindow(self._requested_hwnd)
        )
        self._hwnd = (
            self._requested_hwnd
            if requested_is_valid
            else find_game_hwnd(self._window_class, self._window_title)
        )
        if not self._hwnd:
            self._connected = False
            logger.error("UnityWin32Controller connect failed: game window not found")
            return False

        self._vision = Win32Controller(
            self._hwnd,
            screencap_method=MaaWin32ScreencapMethodEnum.PrintWindow,
            mouse_method=MaaWin32InputMethodEnum.PostMessageWithWindowPos,
            keyboard_method=MaaWin32InputMethodEnum.PostMessageWithWindowPos,
        )
        if not self._vision.set_screenshot_target_short_side(720):
            logger.error("Failed to set Maa Win32 screenshot short side to 720")
            return False

        vision_job = self._vision.post_connection().wait()
        vision_ok = vision_job.succeeded
        bridge_ok = self._bridge.ping()
        self._connected = vision_ok and bridge_ok
        if self._connected:
            logger.info(
                "UnityWin32Controller connected: MaaWin32=%s, bridge=%s, hwnd=%s",
                vision_ok,
                bridge_ok,
                self._hwnd,
            )
        else:
            logger.error(
                "UnityWin32Controller connect failed: MaaWin32=%s, bridge=%s, "
                "bridge_result=%s",
                vision_ok,
                bridge_ok,
                self._bridge.describe_last_result(),
            )
        return self._connected

    def connected(self) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        """检查是否已连接（返回实际连接状态）。"""
        return bool(
            self._connected
            and self._vision is not None
            and self._vision.connected
        )

    def request_uuid(self) -> str:
        vision_uuid = self._vision.uuid if self._vision is not None else "unconnected"
        return f"unity-bridge-{vision_uuid}-{os.getpid()}"

    def get_custom_info(self) -> dict[str, object]:
        """向同进程 CustomAction 暴露 Bridge 专用能力。"""
        return {
            "unity_bridge": {
                "directory": self._bridge.bridge_dir,
                "protocol_end_hold": True,
            }
        }

    def start_app(self, intent: str) -> bool:
        """查找游戏窗口，找到返回 True（不负责启动游戏进程）。"""
        self._hwnd = find_game_hwnd(self._window_class, self._window_title)
        if self._hwnd:
            logger.info("start_app: found window hwnd=%s", self._hwnd)
            return True
        logger.warning("start_app: game window not found")
        return False

    def stop_app(self, intent: str) -> bool:
        """关闭游戏窗口（WM_CLOSE → TerminateProcess 兜底）。"""
        success, msg = _find_and_close_window()
        if success:
            logger.info("stop_app: %s", msg)
        else:
            logger.error("stop_app: %s", msg)
        return success

    # -- 截图 -----------------------------------------------------------------

    def screencap(self) -> numpy.ndarray:
        """由 MFABD2 原有 MaaFramework Win32/PrintWindow 能力提供截图。"""
        if self._vision is None:
            raise RuntimeError("Maa Win32 vision controller is not connected")
        job = self._vision.post_screencap().wait()
        if not job.succeeded:
            raise RuntimeError("Maa Win32 screencap failed")
        return job.get()

    # -- 输入 -----------------------------------------------------------------

    def click(self, x: int, y: int) -> bool:
        """通过 bridge 的 RaycastAll + PointerClick 派发点击。"""
        ok = self._bridge.click(x, y)
        if not ok:
            logger.warning(
                "click(%d, %d): Unity bridge failed: %s",
                x,
                y,
                self._bridge.describe_last_result(),
            )
        return ok

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        """通过 bridge 的 PointerDown → Drag → PointerUp 派发滑动。"""
        ok = self._bridge.swipe(x1, y1, x2, y2, duration_ms=duration)
        if not ok:
            logger.warning(
                "swipe(%d,%d→%d,%d): Unity bridge failed: %s",
                x1,
                y1,
                x2,
                y2,
                self._bridge.describe_last_result(),
            )
        return ok

    def click_key(self, keycode: int) -> bool:
        """将 VK 码翻译为 Unity Key 名，通过 bridge 虚拟键盘发送。"""
        key_name = vk_to_unity_key(keycode)
        if key_name is None:
            logger.warning("click_key: unknown VK code 0x%02X (%d)", keycode, keycode)
            return False
        ok = self._bridge.press_key([key_name])
        if not ok:
            logger.warning(
                "click_key(0x%02X → %s): Unity bridge failed: %s",
                keycode,
                key_name,
                self._bridge.describe_last_result(),
            )
        return ok

    # -- 不支持的方法（bridge 不支持分离 down/up 语义）------------------------

    def touch_down(self, contact: int, x: int, y: int, pressure: int) -> bool:
        """不支持分离的 touch down/move/up，返回 False 让框架使用 click()/swipe()。"""
        logger.debug("touch_down(%d, %d, %d): not supported, framework will use click/swipe", contact, x, y)
        return False

    def touch_move(self, contact: int, x: int, y: int, pressure: int) -> bool:
        """不支持分离的 touch down/move/up，返回 False 让框架使用 click()/swipe()。"""
        logger.debug("touch_move(%d, %d, %d): not supported", contact, x, y)
        return False

    def touch_up(self, contact: int) -> bool:
        """不支持分离的 touch down/move/up，返回 False 让框架使用 click()/swipe()。"""
        logger.debug("touch_up(%d): not supported", contact)
        return False

    def key_down(self, keycode: int) -> bool:
        """不支持分离的 key down/up，返回 False 让框架使用 click_key()。"""
        logger.debug("key_down(0x%02X): not supported, framework will use click_key", keycode)
        return False

    def key_up(self, keycode: int) -> bool:
        """不支持分离的 key down/up，返回 False 让框架使用 click_key()。"""
        logger.debug("key_up(0x%02X): not supported", keycode)
        return False

    def input_text(self, text: str) -> bool:
        """bridge 不支持任意文本输入。"""
        logger.warning("input_text: not supported by unity-bridge")
        return False

    # -- 特性 -----------------------------------------------------------------

    def get_features(self) -> int:
        """让 MaaFramework 使用 click/click_key，而非分离的 down/up API。"""
        return int(MaaControllerFeatureEnum.Null)
