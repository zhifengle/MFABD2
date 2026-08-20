"""Offline assertions for the effective base -> pc -> bridge pipeline."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
