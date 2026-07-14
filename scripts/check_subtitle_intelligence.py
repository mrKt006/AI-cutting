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
from ai_layout import layout_tokens_with_ai  # noqa: E402
from subtitle_layout import analysis_break_sets, build_layout_context, measure_text, segment_tokens, tokens_from_text, wrap_title_text  # noqa: E402
from volc_asr import convert_utterances  # noqa: E402
from make_subtitle import SubtitleCue, TimingSegment, write_ass  # noqa: E402
from main import _ai_removal_segments, _assert_token_conservation, _keep_from_removed, _map_retained_tokens_to_cut_timeline, _protect_speech_from_silence  # noqa: E402
from cut_silence import Segment  # noqa: E402
from style_presets import get_style_preset  # noqa: E402
from web.app import _preview_ass, _write_edit_ass  # noqa: E402


def main() -> int:
    protected_tokens = tokens_from_text("起来了不是", 7.16, 8.36, prefix="guard")
    protected_segment = TimingSegment(7.16, 8.36, "起来了不是", tuple(protected_tokens))
    guarded = _protect_speech_from_silence([Segment(7.49, 7.91)], [protected_segment], set())
    assert all(not (item.start < protected_tokens[2]["end"] and item.end > protected_tokens[2]["start"]) for item in guarded)

    title_text = "普通人该如何学习AI才是最好的"
    title_tokens = tokens_from_text(title_text, 0.0, 1.0, prefix="title-1")
    title_analysis = {
        "allowed_breaks": [
            {"after_token_id": title_tokens[5]["id"], "confidence": 0.95},
            {"after_token_id": title_tokens[10]["id"], "confidence": 0.95},
        ]
    }
    wrapped_title = wrap_title_text(title_text, {"font_size": 100, "margin_x": 120, "bold": True}, 1080, 1920, title_analysis)
    assert wrapped_title.replace("\n", "") == title_text
    assert "\n" in wrapped_title

    ai_tokens = tokens_from_text("生意反而做起来了不是他们", 0.0, 2.0, prefix="ai-layout")
    ai_analysis = {
        "final_sentences": [
            {"token_ids": [token["id"] for token in ai_tokens[:8]], "text": "生意反而做起来了"},
            {"token_ids": [token["id"] for token in ai_tokens[8:]], "text": "不是他们"},
        ],
        "forbidden_breaks": [
            {"token_ids": [token["id"] for token in ai_tokens[8:10]], "text": "不是", "confidence": 0.99},
            {"token_ids": [token["id"] for token in ai_tokens[-2:]], "text": "他们", "confidence": 0.99},
        ],
    }
    invalid_layout = {
        "status": "ok",
        "option_ids": ["o000-008"],
    }
    valid_layout = {
        "status": "ok",
        "option_ids": ["o000-008", "o008-012"],
    }
    with patch("ai_layout.decide_line_layout", side_effect=[invalid_layout, valid_layout]) as layout_mock:
        ai_groups, ai_audit = layout_tokens_with_ai(
            ai_tokens,
            {"font_size": 64, "margin_x": 80, "bold": True},
            1080,
            1920,
            ai_analysis,
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
    assert layout_mock.call_count == 2
    assert ["".join(token["text"] for token in group) for group in ai_groups] == ["生意反而做起来了", "不是他们"]
    assert ai_audit["status"] == "ai"

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
        trace = analyzed["decision_traces"][0]
        assert trace["provider"] == "example.test"
        assert trace["model"] == "test"
        assert trace["prompt_version"]
        assert trace["schema_version"]
        assert trace["input"]["token_count"] == len(correction_tokens)
        assert trace["raw_response"]
        assert "secret" not in json.dumps(trace)
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

    restart_tokens = tokens_from_text("我们在广我们在广西做获客系统", 0.0, 3.0, prefix="restart")
    restart_segment = TimingSegment(0.0, 3.0, "我们在广我们在广西做获客系统", tuple(restart_tokens))
    first_restart_ids = [token["id"] for token in restart_tokens[:4]]
    restart_analysis = {
        "delete_ranges": [
            {"token_ids": first_restart_ids, "type": "false_start", "confidence": 0.97, "reason": "半句后重新起句"}
        ]
    }
    restart_removed, restart_removed_ids = _ai_removal_segments([restart_segment], restart_analysis, "standard")
    assert restart_removed and restart_removed_ids == set(first_restart_ids)

    unsafe_analysis = {
        "delete_ranges": [
            {
                "token_ids": [restart_tokens[0]["id"], restart_tokens[2]["id"]],
                "type": "false_start",
                "confidence": 0.99,
                "reason": "不连续范围",
            },
            {
                "token_ids": [restart_tokens[-1]["id"]],
                "type": "stutter",
                "confidence": 0.99,
                "reason": "末尾没有重复证据",
            },
        ]
    }
    unsafe_removed, _ = _ai_removal_segments([restart_segment], unsafe_analysis, "standard")
    assert not unsafe_removed
    assert {item["skip_reason"] for item in unsafe_analysis["skipped_delete_ranges"]} == {
        "non_contiguous_tokens",
        "missing_adjacent_evidence",
    }

    protected_tokens = tokens_from_text("24小时24小时在线", 0.0, 2.0, prefix="protected-delete")
    protected_segment = TimingSegment(0.0, 2.0, "24小时24小时在线", tuple(protected_tokens))
    protected_analysis = {
        "delete_ranges": [
            {
                "token_ids": [protected_tokens[0]["id"]],
                "type": "exact_repeat",
                "confidence": 0.99,
                "reason": "数字属于关键内容",
            }
        ]
    }
    protected_removed, _ = _ai_removal_segments([protected_segment], protected_analysis, "standard")
    assert not protected_removed
    assert protected_analysis["skipped_delete_ranges"][0]["skip_reason"] == "protected_content"

    trailing_tokens = [
        {"id": "tail-1", "text": "起", "start": 7.10, "end": 7.36},
        {"id": "tail-2", "text": "来", "start": 7.36, "end": 7.44},
        {"id": "tail-3", "text": "了", "start": 7.44, "end": 7.86},
        {"id": "next-1", "text": "不", "start": 7.88, "end": 8.08},
        {"id": "next-2", "text": "是", "start": 8.08, "end": 8.28},
    ]
    trailing_segment = TimingSegment(7.10, 8.28, "起来了不是", tokens=tuple(trailing_tokens))
    trailing_keep = [Segment(0.0, 7.491), Segment(7.906, 9.0)]
    trailing_mapped = _map_retained_tokens_to_cut_timeline([trailing_segment], trailing_keep, set())
    assert "".join(token["text"] for token in trailing_mapped[0].tokens) == "起来了不是"
    assert [token["id"] for token in trailing_mapped[0].tokens] == [token["id"] for token in trailing_tokens]

    try:
        _assert_token_conservation(trailing_tokens, trailing_tokens[:-1], set())
    except RuntimeError as exc:
        assert "missing=next-2" in str(exc)
    else:
        raise AssertionError("Token integrity check did not reject a missing subtitle token")

    fully_removed = _map_retained_tokens_to_cut_timeline(
        [trailing_segment],
        [Segment(0.0, 9.0)],
        {token["id"] for token in trailing_tokens},
    )
    assert fully_removed == []

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

    semantic_tokens = tokens_from_text("不是他们突然变勇敢了是因为做视频", 0.0, 3.0, prefix="semantic")
    semantic_analysis = {
        "final_sentences": [
            {"token_ids": [token["id"] for token in semantic_tokens[:10]], "text": "不是他们突然变勇敢了"},
            {"token_ids": [token["id"] for token in semantic_tokens[10:]], "text": "是因为做视频"},
        ],
        "allowed_breaks": [{"after_token_id": semantic_tokens[4]["id"], "confidence": 0.9}],
        "forbidden_breaks": [
            {
                "token_ids": [semantic_tokens[2]["id"], semantic_tokens[3]["id"]],
                "text": "他们",
                "confidence": 0.99,
            }
        ],
    }
    preferred, required, forbidden = analysis_break_sets(semantic_tokens, semantic_analysis)
    semantic_groups = segment_tokens(
        semantic_tokens,
        {**base_style, "font_size": 110, "margin_x": 420},
        1080,
        1920,
        preferred,
        required,
        forbidden,
    )
    semantic_lines = ["".join(token["text"] for token in group) for group in semantic_groups]
    assert not any(line.endswith("他") for line in semantic_lines)
    first_sentence_ids = {token["id"] for token in semantic_tokens[:10]}
    second_sentence_ids = {token["id"] for token in semantic_tokens[10:]}
    assert all(
        not ({token["id"] for token in group} & first_sentence_ids and {token["id"] for token in group} & second_sentence_ids)
        for group in semantic_groups
    )

    static_preview_ass = _preview_ass(
        "内容标题",
        {**get_style_preset("default-white")["video_title"], "animation_in": "fade", "animation_out": "fade"},
        1080,
        1920,
    )
    assert r"\fad" not in static_preview_ass
    assert "内容标题" in static_preview_ass

    with tempfile.TemporaryDirectory(prefix="subtitle-style-") as tmp:
        ass_path = Path(tmp) / "styled.ass"
        cue = SubtitleCue(1, 0.0, 1.0, "独立样式", style={"font_size": 96, "primary_color": "#ff0000", "position_x": 120})
        _write_edit_ass([cue], [], ass_path, width=1080, height=1920, preset=get_style_preset("default-white"))
        ass_text = ass_path.read_text(encoding="utf-8")
        assert "Style: Subtitle1," in ass_text
        assert ",Subtitle1,," in ass_text
        automatic_ass = Path(tmp) / "automatic-title.ass"
        write_ass(
            [SubtitleCue(1, 0.0, 2.0, "字幕")],
            automatic_ass,
            width=1080,
            height=1920,
            style=get_style_preset("default-white")["subtitle"],
            title_text="内容标题",
            title_style={
                **get_style_preset("default-white")["video_title"],
                "enabled": True,
                "font_size": 72,
                "position_y": -620,
            },
            title_end=1.5,
        )
        automatic_text = automatic_ass.read_text(encoding="utf-8")
        assert "Style: ContentTitle," in automatic_text
        assert ",ContentTitle,," in automatic_text
        assert "内容标题" in automatic_text
        assert "0:00:01.50" in automatic_text
        assert get_style_preset("default-white")["video_title"]["enabled"] is False
    print("Subtitle intelligence check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
