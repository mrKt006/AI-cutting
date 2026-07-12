from __future__ import annotations

import sys
import io
import json
import urllib.error
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from llm_analysis import analyze_transcript, apply_high_confidence_corrections  # noqa: E402
from subtitle_layout import build_layout_context, measure_text, segment_tokens, tokens_from_text  # noqa: E402
from volc_asr import convert_utterances  # noqa: E402
from make_subtitle import SubtitleCue, TimingSegment  # noqa: E402
from main import _ai_removal_segments, _keep_from_removed, _map_retained_tokens_to_cut_timeline  # noqa: E402
from style_presets import get_style_preset  # noqa: E402
from web.app import _write_edit_ass  # noqa: E402


def main() -> int:
    response = {
        "utterances": [
            {
                "start_time": 0,
                "end_time": 1000,
                "text": "繁體Claude",
                "words": [
                    {"start_time": 0, "end_time": 400, "text": "繁體"},
                    {"start_time": 400, "end_time": 1000, "text": "Claude"},
                ],
            },
            {"start_time": 1200, "end_time": 2000, "text": "没有逐字信息"},
        ]
    }
    converted = convert_utterances(response)
    assert converted[0]["text"] == "繁體Claude"
    assert converted[0]["tokens"][0]["timing_source"] == "interpolated-within-word"
    assert converted[0]["tokens"][-1]["text"] == "Claude"
    assert converted[1]["tokens"][0]["timing_source"] == "estimated"

    base_style = {"font_family": "Microsoft YaHei", "font_size": 64, "bold": True, "margin_x": 80}
    large_style = {**base_style, "font_size": 92, "outline_enabled": True, "outline_width": 8}
    base_context = build_layout_context(base_style, 1080, 1920)
    large_context = build_layout_context(large_style, 1080, 1920)
    assert measure_text("同一个字幕预设测试", large_context) > measure_text("同一个字幕预设测试", base_context)

    tokens = tokens_from_text("自己研究Claude研究Codex然后生成视频", 0.0, 3.0, prefix="test")
    groups = segment_tokens(tokens, large_style, 1080, 1920)
    assert "".join(token["text"] for group in groups for token in group) == "自己研究Claude研究Codex然后生成视频"
    assert any(token["text"] == "Claude" for group in groups for token in group)

    correction_tokens = tokens_from_text("分为三内", 0.0, 1.0, prefix="fix")
    target_id = correction_tokens[-1]["id"]
    analysis = {
        "corrections": [{"token_ids": [target_id], "replacement": "类", "confidence": 0.96, "reason": "语境纠错"}],
        "repeat_candidates": [{"token_ids": [correction_tokens[0]["id"]], "confidence": 0.99, "reason": "候选"}],
    }
    apply_high_confidence_corrections(correction_tokens, analysis)
    assert "".join(token["text"] for token in correction_tokens) == "分为三类"
    assert not any(token.get("remove_video") for token in correction_tokens)

    class FakeResponse:
        def __init__(self, payload: dict):
            self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.buffer.read()

    delete_id = correction_tokens[0]["id"]
    success_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "corrections": [],
                            "break_hints": [],
                            "repeat_candidates": [],
                            "delete_ranges": [
                                {
                                    "token_ids": [delete_id],
                                    "type": "stutter",
                                    "confidence": 0.97,
                                    "reason": "重复起音",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=FakeResponse(success_payload)):
        analyzed = analyze_transcript(correction_tokens, base_url="https://example.test/v1", model="test", api_key="secret")
        assert analyzed["status"] == "ok"
        assert analyzed["delete_ranges"][0]["type"] == "stutter"
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        failed = analyze_transcript(correction_tokens, base_url="https://example.test/v1", model="test", api_key="secret")
        assert failed["status"] == "skipped"
        assert "secret" not in json.dumps(failed)

    edit_tokens = tokens_from_text("我我们开始", 0.0, 2.0, prefix="edit")
    edit_segment = TimingSegment(0.0, 2.0, "我我们开始", tokens=tuple(edit_tokens))
    deletion = {
        "delete_ranges": [
            {
                "token_ids": [edit_tokens[0]["id"]],
                "type": "stutter",
                "confidence": 0.98,
                "reason": "重复起音",
            }
        ]
    }
    removed, removed_ids = _ai_removal_segments([edit_segment], deletion, "standard")
    assert removed and edit_tokens[0]["id"] in removed_ids
    keep = _keep_from_removed(2.0, removed)
    mapped = _map_retained_tokens_to_cut_timeline([edit_segment], keep, removed_ids)
    assert edit_tokens[0]["id"] not in {token["id"] for token in mapped[0].tokens}
    assert mapped[0].end < 2.0

    conservative = {
        "delete_ranges": [
            {
                "token_ids": [edit_tokens[-1]["id"]],
                "type": "semantic_repeat",
                "confidence": 0.95,
                "reason": "语义重复",
            }
        ]
    }
    conservative_removed, _ = _ai_removal_segments([edit_segment], conservative, "conservative")
    assert not conservative_removed

    with tempfile.TemporaryDirectory(prefix="subtitle-style-") as tmp:
        ass_path = Path(tmp) / "styled.ass"
        cue = SubtitleCue(1, 0.0, 1.0, "独立样式", style={"font_size": 96, "primary_color": "#ff0000", "position_x": 120})
        _write_edit_ass([cue], [], ass_path, width=1080, height=1920, preset=get_style_preset("default-white"))
        ass_text = ass_path.read_text(encoding="utf-8")
        assert "Style: Subtitle1," in ass_text
        assert ",Subtitle1,," in ass_text
    print("Subtitle intelligence check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
