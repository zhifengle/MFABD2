"""Offline assertions for the effective base -> pc -> bridge pipeline."""

from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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
        cls.pc_resource = Resource()
        register_standalone_extensions(cls.pc_resource)
        for bundle_name in ("base", "pc"):
            bundle = PROJECT_ROOT / "assets" / "resource" / bundle_name
            job = cls.pc_resource.post_bundle(str(bundle)).wait()
            if not job.succeeded:
                raise RuntimeError(f"资源加载失败: {bundle}")

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

    def test_quick_hunt_map_swipes_preserve_end_hold_through_bridge_action(self) -> None:
        expected_points = {
            "QuickHunt_CollectMap_SwipLeft": ([556, 32], [624, 580]),
            "QuickHunt_CollectMap_SwipNoth": ([974, 26], [161, 570]),
        }

        self.assertIn("UnityBridgeSwipe", self.resource.custom_action_list)
        for node_name, (expected_begin, expected_end) in expected_points.items():
            with self.subTest(node=node_name):
                action = self.action(self.node(node_name))
                self.assertEqual(action["type"], "Custom")
                self.assertEqual(action["param"]["custom_action"], "UnityBridgeSwipe")
                params = action["param"]["custom_action_param"]
                self.assertEqual(params["begin"], expected_begin)
                self.assertEqual(params["end"], expected_end)
                self.assertEqual(params["duration"], 800)
                self.assertEqual(params["end_hold"], 600)

    def test_every_direct_end_hold_swipe_is_migrated_to_bridge_action(self) -> None:
        source_nodes: dict[str, dict[str, Any]] = {}
        for node_name in self.pc_resource.node_list:
            action = self.action(self.pc_resource.get_node_data(node_name))
            params = action["param"]
            if action["type"] == "Swipe" and params.get("end_hold"):
                source_nodes[node_name] = params

        self.assertEqual(len(source_nodes), 32)
        for node_name, source in source_nodes.items():
            with self.subTest(node=node_name):
                self.assertEqual(len(source["end"]), 1)
                self.assertEqual(len(source["duration"]), 1)
                self.assertEqual(len(source["end_hold"]), 1)

                action = self.action(self.node(node_name))
                self.assertEqual(action["type"], "Custom")
                self.assertEqual(action["param"]["custom_action"], "UnityBridgeSwipe")
                params = action["param"]["custom_action_param"]
                self.assertEqual(params["begin"], source["begin"][:2])
                self.assertEqual(params["end"], source["end"][0][:2])
                self.assertEqual(params["duration"], source["duration"][0])
                self.assertEqual(params["end_hold"], source["end_hold"][0])

        for node_name in self.resource.node_list:
            action = self.action(self.node(node_name))
            self.assertFalse(
                action["type"] == "Swipe" and action["param"].get("end_hold"),
                f"Unity Bridge 仍有原生 end_hold Swipe: {node_name}",
            )

    def test_dynamic_swipe_override_is_converted_only_for_bridge(self) -> None:
        from action.unity_bridge_swipe import bridge_swipe_override

        bridge_context = SimpleNamespace(
            tasker=SimpleNamespace(
                controller=SimpleNamespace(
                    info={
                        "unity_bridge": {
                            "directory": "C:/bridge",
                            "protocol_end_hold": True,
                        }
                    }
                )
            )
        )
        source = {
            "action": "Swipe",
            "begin": [10, 20],
            "end": [30, 40],
            "duration": 500,
            "end_hold": 800,
            "post_delay": 1000,
        }
        converted = bridge_swipe_override(bridge_context, source)
        self.assertIsNotNone(converted)
        self.assertEqual(converted["action"], "Custom")
        self.assertEqual(converted["custom_action"], "UnityBridgeSwipe")
        self.assertEqual(converted["post_delay"], 1000)
        self.assertEqual(converted["custom_action_param"]["end_hold"], 800)
        self.assertEqual(source["action"], "Swipe")

        pc_context = SimpleNamespace(
            tasker=SimpleNamespace(controller=SimpleNamespace(info={}))
        )
        self.assertIsNone(bridge_swipe_override(pc_context, source))

    def test_bridge_swipe_accepts_native_rect_and_choice_shapes(self) -> None:
        from action.unity_bridge_swipe import _parse_non_negative_int, _parse_point

        with patch("action.unity_bridge_swipe.random.randrange", side_effect=[12, 24]):
            self.assertEqual(
                _parse_point({"begin": [10, 20, 5, 6]}, "begin"),
                (12, 24),
            )
        with patch(
            "action.unity_bridge_swipe.random.choice",
            side_effect=[[30, 40, 1, 1], 800],
        ):
            self.assertEqual(
                _parse_point({"end": [[30, 40, 1, 1], [50, 60, 1, 1]]}, "end"),
                (30, 40),
            )
            self.assertEqual(
                _parse_non_negative_int({"duration": [500, 800]}, "duration", 500),
                800,
            )

    def test_bridge_restores_complete_smart_action_swipe_overrides(self) -> None:
        for node_name in ("Activities_Slip", "Arbitrage_Select_Swip", "Setup_Main_Swip"):
            with self.subTest(node=node_name):
                action = self.action(self.node(node_name))
                params = action["param"]["custom_action_param"]
                proxy = params["proxy_override"]
                self.assertEqual(proxy["action"], "Swipe")
                self.assertGreater(proxy["end_hold"], 0)
                self.assertEqual(proxy["post_delay"], 1000)

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
