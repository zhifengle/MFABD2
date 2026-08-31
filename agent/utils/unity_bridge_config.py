"""Typed TOML configuration for the standalone Unity Bridge runners."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]


class TomlConfigError(ValueError):
    """Raised when a TOML file is valid TOML but has an invalid schema."""


@dataclass(frozen=True)
class SingleRunConfig:
    bridge_dir: str | None = None
    resource_root: str | None = None
    task_name: str | None = None
    account_id: str | None = None
    timeout: float | None = None
    options: tuple[str, ...] = ()
    click: tuple[float, float] | None = None
    after_click: float | None = None


@dataclass(frozen=True)
class BatchTaskConfig:
    name: str
    enabled: bool = True
    timeout: float | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchRunConfig:
    bridge_dir: str | None = None
    resource_root: str | None = None
    account_id: str = "0"
    default_timeout: float = 1200.0
    continue_on_error: bool = True
    tasks: tuple[BatchTaskConfig, ...] = field(default_factory=tuple)


def _load_document(config_path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise TomlConfigError(f"配置文件不存在: {path}")

    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except tomllib.TOMLDecodeError as error:
        raise TomlConfigError(f"TOML 语法错误: {error}") from error
    except OSError as error:
        raise TomlConfigError(f"无法读取配置文件 {path}: {error}") from error

    return document, path.parent


def _reject_unknown(table: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        fields = "、".join(unknown)
        raise TomlConfigError(f"{location} 包含未知字段: {fields}")


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise TomlConfigError(f"{key} 必须是 TOML 表")
    return value


def _optional_string(
    table: dict[str, Any], key: str, location: str, *, empty_is_none: bool = False
) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str):
        raise TomlConfigError(f"{location}.{key} 必须是字符串")
    value = value.strip()
    if not value and not empty_is_none:
        raise TomlConfigError(f"{location}.{key} 不能为空")
    return value or None


def _optional_bool(table: dict[str, Any], key: str, location: str) -> bool | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, bool):
        raise TomlConfigError(f"{location}.{key} 必须是布尔值 true 或 false")
    return value


def _optional_timeout(table: dict[str, Any], key: str, location: str) -> float | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TomlConfigError(f"{location}.{key} 必须是非负数")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise TomlConfigError(f"{location}.{key} 必须是非负有限数")
    return result


def _account_id(table: dict[str, Any], key: str, location: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TomlConfigError(f"{location}.{key} 必须是字符串或整数")
    result = str(value).strip()
    if not result:
        raise TomlConfigError(f"{location}.{key} 不能为空")
    return result


def _options(value: Any, location: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TomlConfigError(f"{location} 必须是选项表数组")

    result: list[str] = []
    for index, option in enumerate(value, 1):
        item_location = f"{location}[{index}]"
        if not isinstance(option, dict):
            raise TomlConfigError(f"{item_location} 必须是 TOML 表")
        _reject_unknown(option, {"name", "value"}, item_location)
        name = _optional_string(option, "name", item_location)
        option_value = _optional_string(option, "value", item_location)
        if name is None or option_value is None:
            raise TomlConfigError(f"{item_location} 必须同时包含 name 和 value")
        result.append(f"{name}={option_value}")
    return tuple(result)


def _resolved_path(value: str | None, config_dir: Path) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return str(path.resolve())


def load_single_run_config(config_path: str | Path) -> SingleRunConfig:
    """Load and validate a single-task/click TOML configuration."""
    document, config_dir = _load_document(config_path)
    _reject_unknown(document, {"bridge", "task", "click", "resource_root"}, "根配置")

    bridge = _table(document, "bridge")
    task = _table(document, "task")
    click = _table(document, "click")
    _reject_unknown(bridge, {"dir"}, "bridge")
    _reject_unknown(task, {"name", "account_id", "timeout", "options"}, "task")
    _reject_unknown(click, {"x", "y", "after_click"}, "click")

    bridge_dir = _optional_string(bridge, "dir", "bridge", empty_is_none=True)
    task_name = _optional_string(task, "name", "task")
    account_id = _account_id(task, "account_id", "task")
    timeout = _optional_timeout(task, "timeout", "task")
    options = _options(task.get("options"), "task.options")
    if task and task_name is None:
        raise TomlConfigError("task.name 是 task 表的必填字段")

    click_point: tuple[float, float] | None = None
    has_x = "x" in click
    has_y = "y" in click
    if has_x != has_y:
        raise TomlConfigError("click.x 和 click.y 必须同时配置")
    if has_x:
        x = _optional_timeout(click, "x", "click")
        y = _optional_timeout(click, "y", "click")
        assert x is not None and y is not None
        click_point = (x, y)
    after_click = _optional_timeout(click, "after_click", "click")
    if click and click_point is None:
        raise TomlConfigError("click.x 和 click.y 是 click 表的必填字段")

    if task_name is not None and click_point is not None:
        raise TomlConfigError("task.name 与 click.x/click.y 不能同时配置")

    resource_root = document.get("resource_root")
    if resource_root is not None and not isinstance(resource_root, str):
        raise TomlConfigError("resource_root 必须是字符串")
    if isinstance(resource_root, str) and not resource_root.strip():
        raise TomlConfigError("resource_root 不能为空")

    normalized_resource_root = resource_root.strip() if resource_root else None
    return SingleRunConfig(
        bridge_dir=_resolved_path(bridge_dir, config_dir),
        resource_root=_resolved_path(normalized_resource_root, config_dir),
        task_name=task_name,
        account_id=account_id,
        timeout=timeout,
        options=options,
        click=click_point,
        after_click=after_click,
    )


def load_batch_run_config(config_path: str | Path) -> BatchRunConfig:
    """Load and validate a serial batch TOML configuration."""
    document, config_dir = _load_document(config_path)
    _reject_unknown(document, {"bridge", "global", "tasks", "resource_root"}, "根配置")

    bridge = _table(document, "bridge")
    global_config = _table(document, "global")
    _reject_unknown(bridge, {"dir"}, "bridge")
    _reject_unknown(
        global_config,
        {"account_id", "default_timeout", "continue_on_error"},
        "global",
    )

    bridge_dir = _optional_string(bridge, "dir", "bridge", empty_is_none=True)
    account_id = _account_id(global_config, "account_id", "global") or "0"
    default_timeout = (
        _optional_timeout(global_config, "default_timeout", "global")
        if "default_timeout" in global_config
        else 1200.0
    )
    continue_on_error = _optional_bool(
        global_config, "continue_on_error", "global"
    )

    raw_tasks = document.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise TomlConfigError("配置文件必须包含至少一个 [[tasks]]")

    tasks: list[BatchTaskConfig] = []
    for index, raw_task in enumerate(raw_tasks, 1):
        location = f"tasks[{index}]"
        if not isinstance(raw_task, dict):
            raise TomlConfigError(f"{location} 必须是 TOML 表")
        _reject_unknown(raw_task, {"name", "enabled", "timeout", "options"}, location)
        name = _optional_string(raw_task, "name", location)
        if name is None:
            raise TomlConfigError(f"{location}.name 是必填字段")
        enabled = _optional_bool(raw_task, "enabled", location)
        tasks.append(
            BatchTaskConfig(
                name=name,
                enabled=True if enabled is None else enabled,
                timeout=_optional_timeout(raw_task, "timeout", location),
                options=_options(raw_task.get("options"), f"{location}.options"),
            )
        )

    resource_root = document.get("resource_root")
    if resource_root is not None and not isinstance(resource_root, str):
        raise TomlConfigError("resource_root 必须是字符串")
    if isinstance(resource_root, str) and not resource_root.strip():
        raise TomlConfigError("resource_root 不能为空")

    normalized_resource_root = resource_root.strip() if resource_root else None
    assert default_timeout is not None
    return BatchRunConfig(
        bridge_dir=_resolved_path(bridge_dir, config_dir),
        resource_root=_resolved_path(normalized_resource_root, config_dir),
        account_id=account_id,
        default_timeout=default_timeout,
        continue_on_error=(
            True if continue_on_error is None else continue_on_error
        ),
        tasks=tuple(tasks),
    )
