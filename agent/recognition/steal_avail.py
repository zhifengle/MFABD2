"""FindAvailableSteal — 在天赋技能面板中找到第一个可用（非冷却）的偷窃角色。

先从卡片底部的浅色区域检测连续角色卡，再逐卡放大 OCR 技能名。NPC
偷窃面板中的卡片均为偷窃候选；冷却遮罩会使技能字不可读，而可用卡至少能稳定
读出“偷”或“窃”。逐卡识别避免 Maa OCR 将多个小字、箭头合并成一段乱码。
返回第一个可读卡片的整卡技能区，供 pipeline 的 Click action 点击。

Pipeline 用法:
    "Steal_AvailCheck": {
        "recognition": "Custom",
        "custom_recognition": "FindAvailableSteal",
        "action": "Click",
        "next": ["Steal_ConfirmDlg", "Steal_Done"]
    }
"""

import json

import numpy as np

from maa.custom_recognition import CustomRecognition
from maa.agent.agent_server import AgentServer
from utils import mfaalog

# ── ROI（1280×720，复用 Arbitrage PC 标定）──────────
# 技能名行：与 Arbitrage_Bargaining_SkillList PC [60,590,905,60] 一致
_STEAL_ROI = [60, 590, 905, 60]
# 头像区：技能名行上方，"X天"冷却文字所在区域
_COOLDOWN_ROI = [60, 450, 905, 140]
# 同一角色卡的识别结果中心容差；必须小于 82px 卡片间距的一半。
_X_PROXIMITY = 30
# PC 1280x720 实测卡片几何。卡片从左向右连续排列；第一个空位即列表结束。
_CARD_START_X = 86
_CARD_PITCH = 82
_CARD_WIDTH = 80
_MAX_VISIBLE_CARDS = 10
_CARD_BOTTOM_ROI = (640, 686)
_CARD_BRIGHT_RATIO = 0.75
_SKILL_Y = 604
_SKILL_HEIGHT = 34
_TEXT_INSET_X = 15
_TEXT_WIDTH = 50
_OCR_SCALE = 4
# OCR 虚拟节点名（复用 pipeline 中的 _Steal_Panel_Scan，通过 override 改 roi/expected）
_PANEL_SCAN_NODE = "_Steal_Panel_Scan"
@AgentServer.custom_recognition("FindAvailableSteal")
class FindAvailableSteal(CustomRecognition):
    def analyze(self, context, argv):
        try:
            params = (
                json.loads(argv.custom_recognition_param)
                if getattr(argv, "custom_recognition_param", None)
                else {}
            )
            x_proximity = params.get("x_proximity", _X_PROXIMITY)

            screenshot = context.tasker.controller.post_screencap().wait().get()
            if screenshot is None:
                mfaalog.error("[FindAvailableSteal] 截图失败")
                return None

            card_boxes = self._detect_card_boxes(screenshot)
            if card_boxes:
                steal_hits, cooldown_hits = self._ocr_cards(
                    context, screenshot, card_boxes
                )
            else:
                # 保留兼容路径，也便于在非 1280x720 的截图上给出诊断信息。
                steal_hits = self._ocr_steal(context, screenshot)
                cooldown_hits = self._ocr_cooldown(context, screenshot)

            mfaalog.info(
                f"[FindAvailableSteal] 偷窃={len(steal_hits)} "
                f"冷却={len(cooldown_hits)}"
            )

            if not steal_hits:
                mfaalog.info(
                    "[FindAvailableSteal] 面板中未找到'偷窃'文字"
                    "（无偷窃角色或面板不可见）"
                )
                return None

            if not cooldown_hits:
                box = steal_hits[0]["box"]
                mfaalog.info(
                    f"[FindAvailableSteal] 无冷却，直接使用第一个偷窃"
                    f" (x={steal_hits[0]['cx']})"
                )
                return CustomRecognition.AnalyzeResult(
                    box=box,
                    detail={
                        "msg": "无冷却，点击第一个偷窃",
                        "total_steal": len(steal_hits),
                        "cooldown_count": 0,
                    },
                )

            for steal in steal_hits:
                sx = steal["cx"]
                on_cd = any(
                    abs(cd["cx"] - sx) <= x_proximity
                    for cd in cooldown_hits
                )
                if not on_cd:
                    mfaalog.info(
                        f"[FindAvailableSteal] 找到可用偷窃角色 (x={sx})"
                        f"，跳过 {len(cooldown_hits)} 个冷却"
                    )
                    return CustomRecognition.AnalyzeResult(
                        box=steal["box"],
                        detail={
                            "msg": "点击可用偷窃角色",
                            "total_steal": len(steal_hits),
                            "cooldown_count": len(cooldown_hits),
                            "clicked_x": sx,
                        },
                    )

            mfaalog.info(
                f"[FindAvailableSteal] {len(steal_hits)} 个偷窃角色"
                f"全部在冷却中"
            )
            return None

        except Exception as e:
            import traceback
            mfaalog.error(f"[FindAvailableSteal] 异常: {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                mfaalog.error(f"[FindAvailableSteal]   {line}")
            return None

    @staticmethod
    def _detect_card_boxes(screenshot):
        """Detect the contiguous PC talent cards from their pale bottom band."""
        if not isinstance(screenshot, np.ndarray) or screenshot.ndim < 2:
            return []

        height, width = screenshot.shape[:2]
        y1, y2 = _CARD_BOTTOM_ROI
        if height < y2:
            return []

        boxes = []
        for index in range(_MAX_VISIBLE_CARDS):
            x = _CARD_START_X + index * _CARD_PITCH
            if x + _CARD_WIDTH > width:
                break
            band = screenshot[y1:y2, x:x + _CARD_WIDTH]
            if band.size == 0:
                break
            if band.ndim == 2:
                value = band
                spread = np.zeros_like(band)
            else:
                rgb = band[..., :3].astype(np.int16)
                value = rgb.max(axis=2)
                spread = rgb.max(axis=2) - rgb.min(axis=2)
            pale_ratio = float(np.mean((value > 180) & (spread < 45)))
            if pale_ratio < _CARD_BRIGHT_RATIO:
                break
            boxes.append([x, _SKILL_Y, _CARD_WIDTH, _SKILL_HEIGHT])
        return boxes

    def _ocr_cards(self, context, screenshot, card_boxes):
        """OCR each detected card independently after 4x nearest scaling."""
        steal_hits = []
        cooldown_hits = []
        for index, skill_box in enumerate(card_boxes, 1):
            x = skill_box[0]
            skill_image = self._scaled_crop(
                screenshot,
                x + _TEXT_INSET_X,
                _SKILL_Y,
                _TEXT_WIDTH,
                _SKILL_HEIGHT,
            )
            recognized = self._ocr_node(
                context,
                skill_image,
                _PANEL_SCAN_NODE,
                [0, 0, skill_image.shape[1], skill_image.shape[0]],
                [r"偷|窃", r"砍价|討價還價"],
            )
            is_steal = any(
                "偷" in hit["text"] or "窃" in hit["text"]
                for hit in recognized
            )
            is_bargain = any(
                "砍价" in hit["text"] or "討價還價" in hit["text"]
                for hit in recognized
            )
            if is_steal:
                steal_hits.append(
                    {"box": skill_box, "cx": x + _CARD_WIDTH // 2, "text": "偷窃"}
                )

            if not is_steal and not is_bargain:
                cooldown_hits.append(
                    {
                        "box": skill_box,
                        "cx": x + _CARD_WIDTH // 2,
                        "text": "技能字被冷却遮罩抑制",
                    }
                )
            if is_steal:
                state = "偷窃可用"
            elif is_bargain:
                state = "砍价（跳过）"
            else:
                state = "不可读（冷却）"
            mfaalog.info(
                f"[FindAvailableSteal] 卡片{index}: {state}"
            )
        return steal_hits, cooldown_hits

    @staticmethod
    def _scaled_crop(screenshot, x, y, width, height):
        crop = screenshot[y:y + height, x:x + width]
        return np.repeat(np.repeat(crop, _OCR_SCALE, axis=0), _OCR_SCALE, axis=1)

    def _ocr_steal(self, context, screenshot):
        """OCR 技能名行，返回 text=='偷窃' 的命中列表。"""
        return self._ocr_node(
            context, screenshot,
            _PANEL_SCAN_NODE, _STEAL_ROI, ["偷窃"],
        )

    def _ocr_cooldown(self, context, screenshot):
        """OCR 头像区，返回 text 匹配 \\d+天 的命中列表。"""
        return self._ocr_node(
            context, screenshot,
            _PANEL_SCAN_NODE, _COOLDOWN_ROI, [r"^\d+天$"],
        )

    @staticmethod
    def _ocr_node(context, screenshot, node_name, roi, expected):
        """通用 OCR 辅助：pipeline_override 创建临时节点，返回过滤后的命中。

        MaaFW 的 expected 做 regex 过滤，filtered_results 只含匹配项。
        生产环境靠 MaaFW 过滤；测试环境 mock 不做 expected 过滤，
        故 Python 侧用同样的 regex 再过滤一次（双重保障，逻辑一致）。
        """
        import re as _re
        patterns = [_re.compile(p) for p in expected]

        hits = []
        try:
            reco = context.run_recognition(
                node_name, screenshot,
                pipeline_override={
                    node_name: {
                        "recognition": "OCR",
                        "roi": roi,
                        "expected": expected,
                        "only_rec": True,
                    }
                },
            )
        except Exception as e:
            mfaalog.error(f"[FindAvailableSteal] OCR 异常 ({node_name}): {e}")
            return hits

        if not reco:
            mfaalog.warning(f"[FindAvailableSteal] reco=None (node={node_name})")
            return hits

        all_r = getattr(reco, "all_results", None) or []
        filt_r = getattr(reco, "filtered_results", None) or []
        mfaalog.info(
            f"[FindAvailableSteal] {node_name}: "
            f"hit={getattr(reco, 'hit', bool(filt_r or all_r))} "
            f"all={len(all_r)} filt={len(filt_r)} roi={roi} expected={expected}"
        )
        for r in (all_r[:5] if all_r else filt_r[:5]):
            t = getattr(r, "text", "") or ""
            b = getattr(r, "box", None)
            mfaalog.info(f"[FindAvailableSteal]   raw: text='{t}' box={b}")

        results = filt_r or all_r or []
        for r in results:
            box = getattr(r, "box", None)
            text = (getattr(r, "text", "") or "").strip()
            if not box or not text:
                continue
            # Python 侧 expected 过滤（双重保障）
            if not any(p.fullmatch(text) or p.search(text) for p in patterns):
                continue
            x, y, w, h = (int(v) for v in box)
            cx = x + w // 2
            hits.append({"box": [x, y, w, h], "cx": cx, "text": text})

        hits.sort(key=lambda m: m["cx"])
        return hits
