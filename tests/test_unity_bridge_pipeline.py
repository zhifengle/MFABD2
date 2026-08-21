"""Offline assertions for the effective base -> pc -> bridge pipeline."""

from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_ROOT = PROJECT_ROOT / "agent"
for path in (PROJECT_ROOT, AGENT_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from maa.resource import Resource

from agent.utils.unity_bridge import vk_to_unity_key
from utils.interface_options import build_pipeline_override
from utils.standalone_runtime import register_standalone_extensions


class UnityBridgePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.resource = Resource()
        register_standalone_extensions(cls.resource)
        for bundle_name in ("base", "pc", "bridge"):
            bundle = PROJECT_ROOT / "assets" / "resource" / bundle_name
            job = cls.resource.post_bundle(str(bundle)).wait()
            if not job.succeeded:
                raise RuntimeError(f"资源加载失败: {bundle}")

    @classmethod
    def node(cls, name: str) -> dict[str, Any]:
        return cls.resource.get_node_data(name)

    @staticmethod
    def action(node: dict[str, Any]) -> dict[str, Any]:
        value = node.get("action")
        return value if isinstance(value, dict) else {"type": value, "param": {}}

    @classmethod
    def next_names(cls, node: dict[str, Any]) -> list[str]:
        return [
            item["name"] if isinstance(item, dict) else item
            for item in node.get("next") or []
        ]

    def test_story_category_selects_directly_without_reset(self) -> None:
        self.assertEqual(
            self.next_names(self.node("Collect_FeatureSwitch_StoryPack")),
            ["Collect_QuickCart_SelectType_StoryPack"],
        )

    def test_story_cards_use_expected_shortcuts_and_range(self) -> None:
        expected_vks = [*range(49, 58), 48, *range(112, 121)]
        expected_keys = [
            *(f"digit{index}" for index in range(1, 10)),
            "digit0",
            *(f"f{index}" for index in range(1, 10)),
        ]

        for index, (vk, unity_key) in enumerate(
            zip(expected_vks, expected_keys), 1
        ):
            with self.subTest(story=index):
                card = self.node(f"Collect_Pack_Story_{index}")
                key_name = (
                    f"Collect_Pack_KeyClick_{index}"
                    if index <= 8
                    else f"Collect_Pack_KeyClick_Story_{index}"
                )
                key_node = self.node(key_name)

                self.assertIn(key_name, self.next_names(card))
                action = self.action(key_node)
                self.assertEqual(action["type"], "ClickKey")
                self.assertEqual(action["param"]["key"], [vk])
                self.assertEqual(vk_to_unity_key(vk), unity_key)

        self.assertFalse(self.node("Collect_Pack_Story_20")["enabled"])
        self.assertNotIn(
            "Collect_Pack_KeyClick_Story_20", self.resource.node_list
        )

    def test_character_and_event_cards_use_digit_shortcuts_and_ranges(self) -> None:
        for category, count, key_prefix in (
            ("Character", 7, "Collect_Pack_KeyClick_"),
            ("Event", 5, "Collect_Pack_KeyClick_Event_"),
        ):
            for index in range(1, count + 1):
                with self.subTest(category=category, card=index):
                    card = self.node(f"Collect_Pack_{category}_{index}")
                    key_name = f"{key_prefix}{index}"
                    self.assertIn(key_name, self.next_names(card))
                    action = self.action(self.node(key_name))
                    self.assertEqual(action["type"], "ClickKey")
                    self.assertEqual(action["param"]["key"], [48 + index])
                    self.assertEqual(vk_to_unity_key(48 + index), f"digit{index}")

        self.assertFalse(self.node("Collect_Pack_Character_8")["enabled"])

    def test_no_right_page_template_fits_pc_roi(self) -> None:
        node = self.node("Collect_OperationsEnd_NORight")
        roi = node["recognition"]["param"]["roi"]
        template = (
            PROJECT_ROOT
            / "assets"
            / "resource"
            / "pc"
            / "image"
            / "MapOlayLeftButt.png"
        )
        png = template.read_bytes()
        width, height = struct.unpack(">II", png[16:24])

        self.assertGreaterEqual(roi[2], width)
        self.assertGreaterEqual(roi[3], height)

    def test_pc_map_right_is_a_single_click_without_looping(self) -> None:
        node = self.node("Collect_MapRight")
        params = self.action(node)["param"]["custom_action_param"]

        self.assertEqual(params["on_changed"], "next")
        self.assertEqual(params["on_unchanged"], "next")
        self.assertEqual(params["unchanged_streak"], 1)
        self.assertNotIn("loop_limit", params)

    def test_character_three_terminal_requires_unique_map_and_no_right_page(self) -> None:
        terminal = self.node("Collect_Character3_Battle3_End")
        all_of = terminal["recognition"]["param"]["all_of"]
        self.assertEqual(
            all_of,
            [
                "Cpt_Collect_Character3_Battle3_End_Ocr",
                "Cpt_Collect_Character3_Battle3_NoRight",
            ],
        )

        no_right = self.node("Cpt_Collect_Character3_Battle3_NoRight")
        self.assertTrue(no_right["inverse"])

        name_check = self.node("Cpt_Collect_Character3_Battle3_End_Ocr")
        expected = name_check["recognition"]["param"]["expected"]
        self.assertIn("战[斗鬥]", expected[0])
        self.assertIn("拍[卖賣]仓[库庫]", expected[0])

        for node_name in (
            "Collect_TargetMap1-2",
            "Collect_TargetMap2",
            "Collect_TargetMap3",
            "Collect_TargetMap4",
        ):
            with self.subTest(node=node_name):
                next_names = self.next_names(self.node(node_name))
                terminal_index = next_names.index("Collect_Character3_Battle3_End")
                no_active_index = next_names.index("Collect_ClickTelepor_ButNotActive")
                self.assertLess(terminal_index, no_active_index)

    def test_event_one_battle_menu_accepts_simplified_pc_text(self) -> None:
        node = self.node("Collect_IM_OnFoot_NviMap_E01_A2_Move_Sub")
        expected = node["recognition"]["param"]["expected"]
        self.assertIn("战斗区", expected)
        self.assertIn("戰鬥區域", expected)

    def test_event_one_battle_menu_retries_locally_once_before_full_reset(self) -> None:
        entry = self.node("Collect_IM_OnFoot_NviMap_E01_A2_Move_Sub")
        retry_name = "Collect_IM_OnFoot_NviMap_E01_A2_Move_Sub_Retry"
        self.assertEqual(
            self.next_names({"next": entry["on_error"]}),
            [retry_name, "Collect_IM_OnFoot_NviMap_Sub_Err", "Global_Null"],
        )

        retry = self.node(retry_name)
        self.assertEqual(retry["max_hit"], 1)
        self.assertEqual(retry["action"]["type"], "Click")
        self.assertIn("战斗区", retry["recognition"]["param"]["expected"])
        self.assertEqual(
            self.next_names({"next": retry["on_error"]}),
            ["Collect_IM_OnFoot_NviMap_Sub_Err", "Global_Null"],
        )

    def test_simple_skip_checks_category_before_entering_first_card(self) -> None:
        for node_name, marker in (
            ("Collect_FeatureSwitch_StoryPack", "Pack_Story_SimpleDone"),
            ("Collect_FeatureSwitch_CharacterPack", "Pack_Character_SimpleDone"),
            ("Collect_FeatureSwitch_EvenPack", "Pack_Event_SimpleDone"),
        ):
            with self.subTest(node=node_name):
                node = self.node(node_name)
                recognition = node["recognition"]
                self.assertEqual(recognition["type"], "Custom")
                self.assertEqual(
                    recognition["param"]["custom_recognition"],
                    "CheckCoolDown",
                )
                self.assertEqual(
                    recognition["param"]["custom_recognition_param"]["card_name"],
                    marker,
                )

        interface = json.loads(
            (PROJECT_ROOT / "assets" / "interface.json").read_text(
                encoding="utf-8"
            )
        )
        simple_skip = interface["option"]["简单类跳过"]
        disabled_override = next(
            case for case in simple_skip["cases"] if case["name"] == "No"
        )["pipeline_override"]
        for node_name in (
            "Collect_FeatureSwitch_StoryPack",
            "Collect_FeatureSwitch_CharacterPack",
            "Collect_FeatureSwitch_EvenPack",
        ):
            self.assertEqual(disabled_override[node_name]["recognition"], "DirectHit")

        task = next(
            task
            for task in interface["task"]
            if task["name"] == "[执行]地图采集[完整]"
        )
        default_override = build_pipeline_override(task, interface["option"], {})
        enabled_override = build_pipeline_override(
            task,
            interface["option"],
            {"简单类跳过": "Yes"},
        )
        for node_name in (
            "Collect_FeatureSwitch_StoryPack",
            "Collect_FeatureSwitch_CharacterPack",
            "Collect_FeatureSwitch_EvenPack",
        ):
            self.assertEqual(default_override[node_name]["recognition"], "DirectHit")
            self.assertNotIn(node_name, enabled_override)

    def test_category_completion_returns_to_dispatcher(self) -> None:
        for marker in (
            "Collect_LocatePack_SimpleSkip_Story",
            "Collect_LocatePack_SimpleSkip_Character",
            "Collect_LocatePack_SimpleSkip_Event",
        ):
            self.assertEqual(self.next_names(self.node(marker)), ["Collect_QuickCart_Menu"])

if __name__ == "__main__":
    unittest.main()
