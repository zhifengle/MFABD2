"""Project Interface 任务选项与 pipeline_override 合并工具。

供独立 CLI 解析 Project Interface 并生成 pipeline override。

- ``switch`` / ``select`` / ``cases-only``：按所选 case 取 ``pipeline_override``，
  递归处理该 case 的 ``option`` 子选项。
- ``input``：用 ``inputs`` 的值替换 ``pipeline_override`` 中的 ``{占位名}``。
- ``checkbox``：``default_case`` 列出默认勾选项，未勾选的 case 其
  ``pipeline_override`` 会被「取反」——即对每个覆盖键追加 ``"enabled": false``。

合并规则：后出现的覆盖键深合并进已有字典（嵌套 dict 递归合并，非 dict 直接覆盖）。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# interface.json 中 option 的五种类型
_OPTION_TYPES = {"switch", "select", "input", "checkbox"}
# type 字段缺失时视为 cases-only（如「前置助手」「章节图策略」）

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def load_interface(interface_path: str | Path) -> dict[str, Any]:
    """加载 interface.json，返回完整字典。"""
    with open(interface_path, "r", encoding="utf-8") as f:
        return json.load(f)


# -- 深合并 ------------------------------------------------------------------


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """将 src 深合并进 dst（原地修改并返回 dst）。

    - 两边都是 dict → 递归合并
    - 否则 src 覆盖 dst
    """
    for key, src_val in src.items():
        if (
            key in dst
            and isinstance(dst[key], dict)
            and isinstance(src_val, dict)
        ):
            _deep_merge(dst[key], src_val)
        else:
            dst[key] = copy.deepcopy(src_val)
    return dst


# -- 占位符替换 --------------------------------------------------------------


def _substitute_placeholders(
    obj: Any,
    values: dict[str, Any],
    value_types: dict[str, str] | None = None,
) -> Any:
    """递归替换字符串中的 ``{占位名}`` 为 ``values`` 中对应的值。

    整个值仅为一个占位符时，按照 input 的 ``pipeline_type`` 保留类型；
    占位符嵌在其他文本中时仍进行字符串替换。未找到时保持原样。
    """
    value_types = value_types or {}
    if isinstance(obj, str):
        full_match = _PLACEHOLDER_RE.fullmatch(obj)
        if full_match:
            name = full_match.group(1)
            if name not in values:
                return obj
            value = values[name]
            pipeline_type = value_types.get(name, "string")
            if pipeline_type == "int":
                return int(value)
            if pipeline_type == "string":
                return str(value)
            return value

        def _repl(m: re.Match) -> str:
            name = m.group(1)
            return str(values.get(name, m.group(0)))

        return _PLACEHOLDER_RE.sub(_repl, obj)
    if isinstance(obj, list):
        return [
            _substitute_placeholders(item, values, value_types) for item in obj
        ]
    if isinstance(obj, dict):
        return {
            k: _substitute_placeholders(v, values, value_types)
            for k, v in obj.items()
        }
    return obj


# -- 单选项 override 提取 ----------------------------------------------------


def _extract_case_override(
    option_def: dict[str, Any],
    selected_case: str,
) -> dict[str, Any]:
    """提取单个 case 的 pipeline_override（不含子选项）。

    checkbox 类型的 ``default_case`` 仅用于前端默认勾选；override 提取
    与 switch/select 一致——取 case 的 ``pipeline_override``。
    """
    cases = option_def.get("cases", [])
    for case in cases:
        if case.get("name") == selected_case:
            return copy.deepcopy(case.get("pipeline_override", {}))
    return {}


def _resolve_option(
    option_name: str,
    options: dict[str, Any],
    selections: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """递归解析单个选项，将合并后的 override 写入 ``result``。

    Args:
        option_name: 选项名
        options: interface.json 的 ``option`` 字典
        selections: 用户选择，``{选项名: 值}``。switch/select/checkbox 的值
            是 str 或 list[str]；input 的值是 ``{input_name: str}``。
        result: 累积合并的 pipeline_override 字典（原地修改）
    """
    option_def = options.get(option_name)
    if option_def is None:
        return

    opt_type = option_def.get("type", "cases-only")
    selected = selections.get(option_name)

    if opt_type == "input":
        # input: pipeline_override 中的 {占位名} 替换为用户填入的值
        if selected is None:
            return
        # selected 可以是 {input_name: value} 或单个字符串
        if isinstance(selected, str):
            # 取第一个 input 的 name 作为占位名
            inputs = option_def.get("inputs", [])
            if inputs:
                values = {inputs[0]["name"]: selected}
            else:
                values = {}
        else:
            values = selected

        value_types = {
            item.get("name", ""): item.get("pipeline_type", "string")
            for item in option_def.get("inputs", [])
        }
        base_override = option_def.get("pipeline_override", {})
        merged = _substitute_placeholders(
            copy.deepcopy(base_override), values, value_types
        )
        _deep_merge(result, merged)
        return

    if opt_type == "checkbox":
        # checkbox: selected 是 list[str]（勾选的 case 名）
        if selected is None:
            selected = option_def.get("default_case", [])
        if isinstance(selected, str):
            selected = [selected]

        cases = option_def.get("cases", [])
        selected_set = set(selected)
        for case in cases:
            case_name = case.get("name")
            case_override = case.get("pipeline_override", {})
            if case_name in selected_set:
                # 勾选 → 取该 case 的 override
                _deep_merge(result, copy.deepcopy(case_override))
                # 只处理勾选 case 的子选项
                sub_options = case.get("option", [])
                for sub in sub_options:
                    _resolve_option(sub, options, selections, result)
            else:
                # 未勾选 → 对 override 中的每个键设 enabled=false（取反）
                _apply_checkbox_off(result, case_override)
        return

    # switch / select / cases-only: selected 是 str（case 名）
    if selected is None:
        # 取第一个 case 作为默认（MFAAvalonia 行为）
        cases = option_def.get("cases", [])
        if cases:
            selected = cases[0].get("name")
        else:
            return

    case_override = _extract_case_override(option_def, selected)
    _deep_merge(result, case_override)

    # 递归该 case 的子选项
    cases = option_def.get("cases", [])
    for case in cases:
        if case.get("name") == selected:
            sub_options = case.get("option", [])
            for sub in sub_options:
                _resolve_option(sub, options, selections, result)
            break


def _apply_checkbox_off(
    result: dict[str, Any], case_override: dict[str, Any]
) -> None:
    """对 checkbox 未勾选的 case，将其 override 键以 ``enabled: false`` 写入。

    MFAAvalonia 的 checkbox 语义：case 的 override 通常是
    ``{"NodeName": {"enabled": true}}``，未勾选时该节点应禁用。
    """
    for node_name, patch in case_override.items():
        if isinstance(patch, dict):
            merged_patch = copy.deepcopy(patch)
            merged_patch["enabled"] = False
            _deep_merge(result, {node_name: merged_patch})
        else:
            # 非 dict 值无法"取反"，记录警告并跳过
            logger.warning(
                "checkbox 取反：节点 %s 的 override 不是 dict (%s)，跳过",
                node_name, type(patch).__name__
            )


# -- 公开 API ----------------------------------------------------------------


def build_pipeline_override(
    task_def: dict[str, Any],
    options: dict[str, Any],
    selections: dict[str, Any],
) -> dict[str, Any]:
    """根据任务和用户选择构建完整的 pipeline_override 字典。

    Args:
        task_def: interface.json 中的单个 task 字典
        options: interface.json 的 ``option`` 字典
        selections: 用户的选项选择，``{选项名: 值}``

    Returns:
        合并后的 pipeline_override 字典，可直接传给
        ``Tasker.post_task(entry, pipeline_override=...)``
    """
    result: dict[str, Any] = {}
    task_options = task_def.get("option", [])
    for opt_name in task_options:
        _resolve_option(opt_name, options, selections, result)
    return result


def get_task_options(
    task_def: dict[str, Any], options: dict[str, Any]
) -> list[dict[str, Any]]:
    """获取任务关联的选项定义列表（含递归子选项），供前端渲染表单。

    返回列表中的每个元素是 ``{"name": ..., "def": option_def}``，
    子选项紧跟其父选项的对应 case 之后。
    """
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        opt_def = options.get(name)
        if opt_def is None:
            return
        result.append({"name": name, "def": opt_def})
        # 子选项：所有 case 的 option 字段
        for case in opt_def.get("cases", []):
            for sub in case.get("option", []):
                _collect(sub)

    for name in task_def.get("option", []):
        _collect(name)

    return result


def default_selections(
    task_def: dict[str, Any], options: dict[str, Any]
) -> dict[str, Any]:
    """生成任务的默认选项选择（每个选项取第一个 case / input default）。"""
    result: dict[str, Any] = {}
    task_options = get_task_options(task_def, options)
    for item in task_options:
        name = item["name"]
        opt_def = item["def"]
        opt_type = opt_def.get("type", "cases-only")

        if opt_type == "input":
            inputs = opt_def.get("inputs", [])
            result[name] = {
                inp["name"]: inp.get("default", "")
                for inp in inputs
            }
        elif opt_type == "checkbox":
            result[name] = list(opt_def.get("default_case", []))
        else:
            cases = opt_def.get("cases", [])
            if cases:
                result[name] = cases[0].get("name")
    return result
