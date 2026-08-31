"""Tests for FindAvailableSteal custom recognizer.

Tests the OCR filtering, x-coordinate correlation, and cooldown
skip logic without needing a live MaaFramework context.

Run:
    uv run python -m unittest tests.test_steal_avail -v
    uv run python -m unittest discover -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

# Production code uses `from utils import ...` (agent/ is the package root).
# Add agent/ to sys.path so those imports resolve in the test environment.
_AGENT_ROOT = Path(__file__).resolve().parent.parent / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

# Keep this test process in MaaFramework mode.  Importing a decorated custom
# recognizer normally switches the process-wide binding to AgentServer mode,
# which would make later Resource-based tests fail during test discovery.
from maa.library import Library

Library.version()

from maa.agent.agent_server import AgentServer

_register_custom_recognition = AgentServer.register_custom_recognition
_register_custom_action = AgentServer.register_custom_action
AgentServer.register_custom_recognition = staticmethod(
    lambda name, recognition: True
)
AgentServer.register_custom_action = staticmethod(lambda name, action: True)
try:
    from recognition.steal_avail import FindAvailableSteal
finally:
    AgentServer.register_custom_recognition = staticmethod(_register_custom_recognition)
    AgentServer.register_custom_action = staticmethod(_register_custom_action)


# ── Mock helpers ───────────────────────────────────────


class _MockHit:
    """Single OCR text match."""

    def __init__(self, text: str, x: int, y: int, w: int = 40, h: int = 20):
        self.text = text
        self.box = [x, y, w, h]
        self.score = 0.9


class _MockReco:
    """Simulates MaaFramework recognition result object."""

    def __init__(self, hits: list[_MockHit]):
        self.all_results = hits
        self.filtered_results = hits
        self.hit = bool(hits)


class _MockArgv:
    """Simulates CustomRecognition.AnalyzeArg."""

    def __init__(self, params: dict | None = None):
        self.custom_recognition_param = (
            None if params is None else __import__("json").dumps(params)
        )


class _MockContext:
    """Simulates MaaFramework Context with screenshot + run_recognition.

    run_recognition reads 'expected' from pipeline_override and filters
    all_results to simulate MaaFW's expected filtering, so the custom
    recognizer's two OCR calls (偷窃 vs X天) get different results.
    """

    def __init__(self, ocr_hits: list[_MockHit]):
        self._all_hits = ocr_hits

        class _Waitable:
            def __init__(self, val):
                self._val = val

            def wait(self):
                return self

            def get(self):
                return self._val

        class _Controller:
            def post_screencap(self):
                return _Waitable(b"fake_screenshot")

        class _Tasker:
            controller = _Controller()

        self.tasker = _Tasker()

    def run_recognition(self, node, screenshot, pipeline_override=None):
        import re
        expected = None
        if pipeline_override and node in pipeline_override:
            expected = pipeline_override[node].get("expected")
        if expected:
            patterns = [re.compile(p) for p in expected]
            filtered = [
                h for h in self._all_hits
                if any(
                    p.fullmatch(h.text) or p.search(h.text)
                    for p in patterns
                )
            ]
        else:
            filtered = list(self._all_hits)
        return _MockReco(filtered)


class _MockSequentialContext:
    """Returns one predetermined OCR result for each per-card OCR call."""

    def __init__(self, hit_groups: list[list[_MockHit]]):
        self._hit_groups = list(hit_groups)

    def run_recognition(self, node, screenshot, pipeline_override=None):
        return _MockReco(self._hit_groups.pop(0))


# ── Test cases ─────────────────────────────────────────


class FindAvailableStealTests(unittest.TestCase):
    def setUp(self):
        self.rec = FindAvailableSteal()

    def _analyze(self, hits: list[_MockHit]):
        ctx = _MockContext(hits)
        argv = _MockArgv()
        return self.rec.analyze(ctx, argv)

    def test_detects_contiguous_card_bottom_bands(self):
        screenshot = np.zeros((720, 1280, 3), dtype=np.uint8)
        for x in (86, 168, 250):
            screenshot[640:686, x:x + 80] = 220

        boxes = self.rec._detect_card_boxes(screenshot)

        self.assertEqual([box[0] for box in boxes], [86, 168, 250])

    def test_card_detection_stops_at_first_empty_slot(self):
        screenshot = np.zeros((720, 1280, 3), dtype=np.uint8)
        screenshot[640:686, 86:166] = 220
        screenshot[640:686, 250:330] = 220

        boxes = self.rec._detect_card_boxes(screenshot)

        self.assertEqual([box[0] for box in boxes], [86])

    def test_per_card_ocr_skips_bargain_and_cooldown(self):
        screenshot = np.zeros((720, 1280, 3), dtype=np.uint8)
        card_boxes = [
            [86, 604, 80, 34],
            [168, 604, 80, 34],
            [250, 604, 80, 34],
            [332, 604, 80, 34],
        ]
        context = _MockSequentialContext(
            [
                [_MockHit("砍价", 0, 0)],
                [],
                [_MockHit("偷", 0, 0)],
                [_MockHit("偷窃", 0, 0)],
            ]
        )

        steal, cooldown = self.rec._ocr_cards(context, screenshot, card_boxes)

        self.assertEqual([hit["box"][0] for hit in steal], [250, 332])
        self.assertEqual([hit["box"][0] for hit in cooldown], [168])

    def test_confirm_button_waits_long_enough_for_animation_gate(self):
        pipeline_path = (
            Path(__file__).resolve().parent.parent
            / "assets"
            / "resource"
            / "base"
            / "pipeline"
            / "Steal.json"
        )
        pipeline = __import__("json").loads(
            pipeline_path.read_text(encoding="utf-8")
        )

        self.assertGreaterEqual(
            pipeline["Steal_ConfirmBtn"]["timeout"],
            pipeline["Steal_AnimEnd"]["timeout"],
        )

    # ── 3 characters, all available ──────────────────

    def test_all_available_returns_first_steal(self):
        """3 steal characters, no cooldown → click the first (leftmost)."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("偷窃", x=620, y=590),
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.box[0], 170)

    # ── First on cooldown, second available ──────────

    def test_first_on_cooldown_returns_second(self):
        """First character has 'X天', second is available → click second."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("1天", x=175, y=500),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("偷窃", x=620, y=590),
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.box[0], 395)

    # ── All on cooldown ───────────────────────────────

    def test_all_on_cooldown_returns_none(self):
        """All 3 steal characters have 'X天' → recognition fails (None)."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("1天", x=175, y=500),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("3天", x=400, y=500),
            _MockHit("偷窃", x=620, y=590),
            _MockHit("7天", x=625, y=500),
        ]
        result = self._analyze(hits)

        self.assertIsNone(result)

    # ── No steal characters ──────────────────────────

    def test_no_steal_characters_returns_none(self):
        """Panel visible but no character has steal skill → None."""
        hits = [
            _MockHit("砍价", x=170, y=590),
            _MockHit("砍价", x=395, y=590),
        ]
        result = self._analyze(hits)

        self.assertIsNone(result)

    # ── Mixed skills: 砍价 + 偷窃 ─────────────────────

    def test_mixed_skills_skips_bargain(self):
        """4 characters: 1 砍价 + 3 偷窃, no cooldown → click first 偷窃."""
        hits = [
            _MockHit("砍价", x=170, y=590),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("偷窃", x=620, y=590),
            _MockHit("偷窃", x=845, y=590),
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.box[0], 395)

    # ── 4 characters, middle on cooldown ──────────────

    def test_four_chars_middle_cooldown(self):
        """4 characters, 2nd 偷窃 on cooldown → click 1st 偷窃."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("2天", x=400, y=500),
            _MockHit("偷窃", x=620, y=590),
            _MockHit("偷窃", x=845, y=590),
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.box[0], 170)

    # ── Cooldown edge: X天 far from any 偷窃 ──────────

    def test_cooldown_far_away_ignored(self):
        """A cooldown match far from 偷窃 is ignored."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("5天", x=800, y=500),  # 630px away from the only 偷窃
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.box[0], 170)

    # ── Empty panel ───────────────────────────────────

    def test_empty_panel_returns_none(self):
        """No OCR hits at all → None."""
        result = self._analyze([])

        self.assertIsNone(result)

    # ── Detail dict has useful info ───────────────────

    def test_detail_contains_counts(self):
        """Result detail should include steal/cooldown counts for logging."""
        hits = [
            _MockHit("偷窃", x=170, y=590),
            _MockHit("偷窃", x=395, y=590),
            _MockHit("1天", x=175, y=500),
        ]
        result = self._analyze(hits)

        self.assertIsNotNone(result)
        self.assertEqual(result.detail["total_steal"], 2)
        self.assertEqual(result.detail["cooldown_count"], 1)


if __name__ == "__main__":
    unittest.main()
