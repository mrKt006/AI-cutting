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

from llm_analysis import _analysis_validation, _materialize_holistic_index_result, _materialize_holistic_markup_result, _materialize_semantic_ranges, _validate_deletion_survival, analyze_transcript, apply_high_confidence_corrections, build_markup_request  # noqa: E402
from ai_layout import layout_tokens_with_ai  # noqa: E402
from subtitle_layout import analysis_break_sets, build_layout_capacity, build_layout_context, measure_text, segment_tokens, tokens_from_text, wrap_title_text  # noqa: E402
from volc_asr import convert_utterances  # noqa: E402
from make_subtitle import SubtitleCue, TimingSegment, write_ass  # noqa: E402
from main import _ai_removal_segments, _assert_token_conservation, _build_automatic_quality_gate, _keep_from_removed, _map_retained_tokens_to_cut_timeline, _protect_speech_from_silence  # noqa: E402
from cut_silence import Segment  # noqa: E402
from style_presets import get_style_preset  # noqa: E402
from web.app import _is_unedited_placeholder_project, _preview_ass, _write_edit_ass  # noqa: E402


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
    with patch("ai_layout.decide_line_layout", side_effect=[invalid_layout, valid_layout]) as layout_mock, patch(
        "ai_layout.review_line_layout", return_value={"status": "ok", "approved": True, "issues": []}
    ) as review_mock:
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
    assert review_mock.call_count == 1
    assert ["".join(token["text"] for token in group) for group in ai_groups] == ["生意反而做起来了", "不是他们"]
    assert ai_audit["status"] == "ai"

    budget_tokens = tokens_from_text("春夏秋冬东西南北天地山河", 0.0, 2.0, prefix="layout-budget")
    with patch("ai_layout.decide_line_layout", return_value={"status": "ok", "option_ids": []}) as budget_mock:
        budget_groups, budget_audit = layout_tokens_with_ai(
            budget_tokens,
            {"font_size": 64, "margin_x": 80, "bold": True},
            1080,
            1920,
            {},
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="test-key",
            max_calls=1,
        )
    assert budget_mock.call_count == 1
    assert "".join(token["text"] for group in budget_groups for token in group) == "春夏秋冬东西南北天地山河"
    assert budget_audit["status"] == "mixed"
    assert any(chunk.get("reason") == "call_budget_exhausted" for chunk in budget_audit["chunks"])
    assert all(chunk.get("status") == "fallback" for chunk in budget_audit["chunks"])

    # A physically valid break must still be rejected when it leaves an unfinished
    # possessive/attributive phrase at the end of a subtitle line.
    possessive_tokens = tokens_from_text("你的工厂你的服务告诉AI", 0.0, 2.0, prefix="semantic-review")
    token_count = len(possessive_tokens)
    with patch(
        "ai_layout.decide_line_layout",
        side_effect=[
            {"status": "ok", "line_ends": [6, token_count]},
            {"status": "ok", "line_ends": [4, token_count]},
        ],
    ) as semantic_layout_mock, patch(
        "ai_layout.review_line_layout",
        side_effect=[
            {"status": "ok", "approved": False, "issues": ["第1行以未完成的领属结构结尾"]},
            {"status": "ok", "approved": True, "issues": []},
        ],
    ) as semantic_review_mock:
        semantic_groups, semantic_audit = layout_tokens_with_ai(
            possessive_tokens,
            {"font_size": 64, "margin_x": 80, "bold": True},
            1080,
            1920,
            {},
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
    assert semantic_layout_mock.call_count == 2
    assert semantic_review_mock.call_count == 2
    assert ["".join(token["text"] for token in group) for group in semantic_groups] == ["你的工厂", "你的服务告诉AI"]
    assert semantic_audit["status"] == "ai"

    # A deterministic physical proposal is acceptable only after the independent
    # semantic reviewer approves the exact final lines.
    verified_local_tokens = tokens_from_text("今年有一批从来不敢出镜的老板", 0.0, 2.0, prefix="verified-local")
    with patch(
        "ai_layout.decide_line_layout",
        return_value={"status": "ok", "line_ends": [5, len(verified_local_tokens)]},
    ), patch(
        "ai_layout.review_line_layout",
        side_effect=[
            {"status": "ok", "approved": False, "issues": ["模型方案断句不完整"]},
            {"status": "ok", "approved": False, "issues": ["重试方案仍不完整"]},
            {"status": "ok", "approved": True, "issues": []},
        ],
    ):
        verified_local_groups, verified_local_audit = layout_tokens_with_ai(
            verified_local_tokens,
            {"font_size": 64, "margin_x": 80, "bold": True},
            1080,
            1920,
            {},
            base_url="https://example.invalid/v1",
            model="test-model",
            api_key="test-key",
        )
    assert "".join(token["text"] for group in verified_local_groups for token in group) == "今年有一批从来不敢出镜的老板"
    assert verified_local_audit["status"] == "ai"
    assert verified_local_audit["chunks"][0].get("source") == "ai_verified_local"

    oversized_tokens = tokens_from_text("完整句子不能冒充不可拆词组", 0.0, 1.0, prefix="oversized-span")
    oversized_ids = [token["id"] for token in oversized_tokens]
    oversized_validation = _analysis_validation(
        oversized_tokens,
        {
            "forbidden_breaks": [{"token_ids": oversized_ids, "text": "完整句子不能冒充不可拆词组"}],
            "final_sentences": [{"token_ids": oversized_ids, "text": "完整句子不能冒充不可拆词组"}],
        },
        require_coverage=True,
    )
    assert oversized_validation["valid"] is False
    assert any("超过 8 个 token" in error for error in oversized_validation["errors"])

    indexed_semantics = _materialize_semantic_ranges(
        {
            "forbidden_ranges": [{"start_i": 1, "end_i": 2, "confidence": 0.99, "reason": "固定词组"}],
            "protected_ranges": [],
            "sentence_ends": [3, len(oversized_tokens) - 1],
        },
        oversized_tokens,
    )
    assert indexed_semantics["forbidden_breaks"][0]["token_ids"] == oversized_ids[1:3]
    assert [token_id for row in indexed_semantics["final_sentences"] for token_id in row["token_ids"]] == oversized_ids
    try:
        _materialize_semantic_ranges(
            {"forbidden_ranges": [], "protected_ranges": [], "sentence_ends": [2]}, oversized_tokens
        )
    except ValueError as exc:
        assert "最后一个值" in str(exc)
    else:
        raise AssertionError("semantic range materializer accepted incomplete coverage")

    placeholder_title = "自动字幕任务"
    placeholder_project = {
        "sentences": [{
            "original_text": placeholder_title,
            "text": placeholder_title,
            "enabled": True,
            "remove_video": False,
            "edited": False,
            "tokens": [{"text": char, "timing_source": "estimated", "edited": False} for char in placeholder_title],
        }]
    }
    assert _is_unedited_placeholder_project(
        placeholder_project, {"title": placeholder_title}, {"params": {"title": placeholder_title}}
    )
    placeholder_project["sentences"][0]["edited"] = True
    assert not _is_unedited_placeholder_project(
        placeholder_project, {"title": placeholder_title}, {"params": {"title": placeholder_title}}
    )

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
    timeline_request, _ = build_markup_request([
        token for segment in converted for token in segment["tokens"]
    ])
    assert timeline_request["transcript"] == "繁體Claude没有逐字信息"
    assert len(timeline_request["speech_timeline"]) == 2
    assert timeline_request["speech_timeline"][0]["text"] == "繁體Claude"
    assert timeline_request["speech_timeline"][0]["pause_after_ms"] == 200
    assert timeline_request["speech_timeline"][0]["boundary_after"] == "asr_utterance"

    base_style = {"font_family": "Microsoft YaHei", "font_size": 64, "bold": True, "margin_x": 80}
    large_style = {**base_style, "font_size": 92, "outline_enabled": True, "outline_width": 8}
    base_context = build_layout_context(base_style, 1080, 1920)
    large_context = build_layout_context(large_style, 1080, 1920)
    assert measure_text("同一个字幕预设测试", large_context) > measure_text("同一个字幕预设测试", base_context)
    base_capacity = build_layout_capacity(base_style, 1080, 1920)
    large_capacity = build_layout_capacity(large_style, 1080, 1920)
    wide_capacity = build_layout_capacity(base_style, 1920, 1920)
    assert large_capacity["absolute_max_units"] < base_capacity["absolute_max_units"]
    assert wide_capacity["absolute_max_units"] > base_capacity["absolute_max_units"]

    # A conservative per-token index hint must never override the authoritative
    # full-string pixel measurement when the complete caption still fits.
    advisory_tokens = tokens_from_text("真实宽度", 0.0, 1.0, prefix="advisory-width")
    for token in advisory_tokens:
        token["hard_end_i"] = 0
    advisory_result = _materialize_holistic_index_result(
        {
            "corrections": [],
            "deletions": [],
            "captions": [{"start_i": 0, "end_i": len(advisory_tokens) - 1}],
            "allowed_breaks": [],
        },
        advisory_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert advisory_result["validation"]["pixel_overflows"] == 0
    assert advisory_result["validation"]["advisory_index_overruns"]

    ai_break_tokens = tokens_from_text("你的工厂你的服务", 0.0, 1.0, prefix="ai-break-repair")
    ai_break_style = {**base_style, "font_size": 80, "margin_x": 350}
    ai_break_capacity = build_layout_capacity(ai_break_style, 1080, 1920)
    ai_break_result = _materialize_holistic_index_result(
        {
            "corrections": [],
            "deletions": [],
            "captions": [{"start_i": 0, "end_i": len(ai_break_tokens) - 1}],
            "allowed_breaks": [3],
        },
        ai_break_tokens,
        ai_break_capacity,
        style=ai_break_style,
        width=1080,
        height=1920,
    )
    assert [caption["text"] for caption in ai_break_result["captions"]] == ["你的工厂", "你的服务"]
    assert ai_break_result["validation"]["ai_break_repairs"]

    local_wrap_result = _materialize_holistic_index_result(
        {
            "corrections": [],
            "deletions": [],
            "captions": [{"start_i": 0, "end_i": len(ai_break_tokens) - 1}],
            "allowed_breaks": [],
        },
        ai_break_tokens,
        ai_break_capacity,
        style=ai_break_style,
        width=1080,
        height=1920,
    )
    assert local_wrap_result["layout_decision"]["status"] == "validated_local"
    assert local_wrap_result["validation"]["validated_local_repairs"]

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

    def llm_payload(content: dict) -> dict:
        return {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}

    success_payload = llm_payload(
        {
            "corrections": [{"start_i": 3, "end_i": 3, "replacement": "类", "confidence": 0.96, "reason": "同音词纠正"}],
            "deletions": [{"start_i": 0, "end_i": 0, "type": "stutter", "confidence": 0.97, "reason": "重复起音"}],
            "caption_ends": [len(correction_tokens) - 1],
            "allowed_breaks": [],
        }
    )
    with patch("urllib.request.urlopen", return_value=FakeResponse(success_payload)) as holistic_call:
        analyzed = analyze_transcript(correction_tokens, base_url="https://example.test/v1", model="test", api_key="secret")
        assert analyzed["status"] == "ok"
        assert analyzed["pipeline_version"] == "holistic-transcript-v1"
        assert analyzed["api_calls"] == 1
        assert holistic_call.call_count == 1
        assert analyzed["delete_ranges"][0]["type"] == "stutter"
        assert analyzed["captions"][0]["text"] == "为三类"
        assert analyzed["validation"]["caption_text_source"] == "local_token_reconstruction"
        trace = analyzed["decision_traces"][0]
        assert trace["provider"] == "example.test"
        assert trace["model"] == "test"
        assert trace["prompt_version"]
        assert trace["schema_version"]
        assert trace["input"]["token_count"] == len(correction_tokens)
        assert trace["raw_response"]
        assert trace["attempt_count"] == 1
        assert trace["retry_errors"] == []
        assert "secret" not in json.dumps(trace)

    # DeepSeek chooses only semantic ends. The backend constructs all starts,
    # closes ranges around deletions, and therefore cannot orphan one token.
    false_start_tokens = tokens_from_text("最高的打法在广我们在广西", 0.0, 2.0, prefix="caption-ends")
    false_start_result = _materialize_holistic_index_result(
        {
            "corrections": [],
            "deletions": [{"start_i": 5, "end_i": 6, "type": "false_start", "confidence": 0.98, "reason": "未完成后重说"}],
            "caption_ends": [4, len(false_start_tokens) - 1],
            "allowed_breaks": [4],
        },
        false_start_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert false_start_result["validation"]["caption_coverage"] == 1.0
    assert [caption["source_indices"] for caption in false_start_result["captions"]] == [
        [0, 4], [7, len(false_start_tokens) - 1]
    ]

    markup_tokens = tokens_from_text("这是一个一个示范文案货客系统", 0.0, 2.0, prefix="markup")
    markup_result = _materialize_holistic_markup_result(
        "/这是/[-一个-]/一个示范文案[货客=>获客]/系统/",
        markup_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert markup_result["validation"]["source_projection_exact"] is True
    assert markup_result["validation"]["caption_coverage"] == 1.0
    assert [caption["text"] for caption in markup_result["captions"]] == ["这是一个", "一个示范文案获客系统"]
    assert markup_result["validation"]["caption_coverage_basis"] == "all_source_tokens_before_verified_deletion"
    assert markup_result["layout_decision"]["source"] == "deepseek_semantics_deterministic_pixel_optimizer"
    assert markup_result["delete_ranges"][0]["type"] == "exact_repeat"
    assert len(markup_result["delete_ranges"][0]["token_ids"]) == 2
    assert markup_result["corrections"][0]["replacement"] == "获客"
    omission_tokens = tokens_from_text("我们在广西", 0.0, 1.0, prefix="markup-omission")
    omission_result = _materialize_holistic_markup_result(
        "/在广西/",
        omission_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert omission_result["captions"][0]["text"] == "我们在广西"
    assert omission_result["validation"]["markup_repairs"][0]["restored_text"] == "我们"
    repeated_day_tokens = tokens_from_text("每天每天几十个", 0.0, 1.0, prefix="markup-repeat-right")
    repeated_day_result = _materialize_holistic_markup_result(
        "/每天/[-每天-]/几十个/",
        repeated_day_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    repeated_day_result["pipeline_version"] = "holistic-markup-v1"
    repeated_day_segment = TimingSegment(0.0, 1.0, "每天每天几十个", tuple(repeated_day_tokens))
    repeated_day_removed, repeated_day_ids = _ai_removal_segments(
        [repeated_day_segment], repeated_day_result, "standard"
    )
    assert repeated_day_removed and len(repeated_day_ids) == 2
    assert repeated_day_result["applied_delete_ranges"][0]["validation"] == "adjacent_exact_repeat_left"

    # AI semantic spans are advisory. Even when it returns one physically
    # impossible long span, the deterministic optimizer must find a complete,
    # pixel-safe path without another model request.
    optimizer_tokens = tokens_from_text("你的工厂你的服务告诉人工智能", 0.0, 2.0, prefix="markup-optimizer")
    optimizer_result = _materialize_holistic_markup_result(
        "/你的工厂|你的服务告诉人工智能/",
        optimizer_tokens,
        ai_break_capacity,
        style=ai_break_style,
        width=1080,
        height=1920,
    )
    assert optimizer_result["validation"]["caption_coverage"] == 1.0
    assert all(caption["width_px"] <= ai_break_capacity["hard_width_px"] for caption in optimizer_result["captions"])
    assert "".join(caption["text"] for caption in optimizer_result["captions"]) == "你的工厂你的服务告诉人工智能"
    assert optimizer_result["validation"]["semantic_boundary_optimizer"]["status"] == "validated"
    semantic_priority_result = _materialize_holistic_markup_result(
        "/你的工厂/你的服务|告诉人工智能/",
        optimizer_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert semantic_priority_result["captions"][0]["text"] == "你的工厂"
    assert semantic_priority_result["validation"]["semantic_boundary_optimizer"]["used_ai_boundaries"][0]["strength"] == "strong"
    possessive_source = "你只需要把你自己的产品信息你的工厂你的服务告诉AIAI能直接工作"
    possessive_tokens = tokens_from_text(possessive_source, 0.0, 4.0, prefix="markup-possessive")
    possessive_result = _materialize_holistic_markup_result(
        "/你只需要把你自己的产品信息你的工厂你的服务/告诉AIAI能直接工作/",
        possessive_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    possessive_captions = [caption["text"] for caption in possessive_result["captions"]]
    assert not any(text.startswith("告诉") for text in possessive_captions)
    assert "你的工厂" in possessive_captions
    assert any(text.startswith("你的服务告诉AI") for text in possessive_captions)
    assert any(
        row.get("source") == "parallel_possessive_heuristic"
        for row in possessive_result["validation"]["semantic_boundary_optimizer"]["used_ai_boundaries"]
    )
    ascii_repeat_tokens = [
        {"id": "ascii-w1", "text": "告诉", "start": 0.0, "end": 0.4},
        {"id": "ascii-w2", "text": "AI", "start": 0.4, "end": 0.7},
        {"id": "ascii-w3", "text": "AI", "start": 0.7, "end": 1.0},
        {"id": "ascii-w4", "text": "能直接工作", "start": 1.0, "end": 1.8},
    ]
    ascii_repeat_result = _materialize_holistic_markup_result(
        "/告诉AIAI能直接工作/",
        ascii_repeat_tokens,
        base_capacity,
        style=base_style,
        width=1080,
        height=1920,
    )
    assert [caption["text"] for caption in ascii_repeat_result["captions"]] == ["告诉AI", "AI能直接工作"]
    try:
        _materialize_holistic_markup_result(
            "/这是一个示范文/",
            markup_tokens,
            base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
    except ValueError as exc:
        assert "不一致" in str(exc) or "没有覆盖完整原文" in str(exc)
    else:
        raise AssertionError("Incomplete markup transcript was accepted")

    markup_payload = {
        "choices": [{"message": {"content": "/这是/[-一个-]/一个示范文案[货客=>获客]/系统/"}}]
    }
    with patch("urllib.request.urlopen", return_value=FakeResponse(markup_payload)):
        markup_analysis = analyze_transcript(
            markup_tokens,
            base_url="https://example.test/v1",
            model="markup-test",
            api_key="secret",
            layout=base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
    assert markup_analysis["status"] == "ok"
    assert markup_analysis["pipeline_version"] == "holistic-markup-v1"
    assert markup_analysis["validation"]["source_projection_exact"] is True
    invalid_payload = llm_payload({"corrections": [], "deletions": [], "captions": []})
    with patch(
        "urllib.request.urlopen",
        side_effect=[FakeResponse(invalid_payload), FakeResponse(success_payload)],
    ) as retry_call:
        retried = analyze_transcript(
            correction_tokens,
            base_url="https://example.test/v1",
            model="retry-test",
            api_key="secret",
            layout=base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
        assert retried["status"] == "ok"
        assert retried["api_calls"] == 2
        assert retry_call.call_count == 2
        assert retried["validation"]["caption_coverage"] == 1.0
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
        failed = analyze_transcript(correction_tokens, base_url="https://example.test/v1", model="test", api_key="secret")
        assert failed["status"] == "skipped"
        assert "secret" not in json.dumps(failed)

    holistic_target_tokens = tokens_from_text("你的工厂你的服务告诉AI", 0.0, 2.0, prefix="holistic-target")
    target_payload = llm_payload({
        "corrections": [],
        "deletions": [],
        "captions": [
            {"start_i": 0, "end_i": 3},
            {"start_i": 4, "end_i": len(holistic_target_tokens) - 1},
        ],
        "allowed_breaks": [3],
    })
    with patch("urllib.request.urlopen", return_value=FakeResponse(target_payload)) as target_call:
        target_analysis = analyze_transcript(
            holistic_target_tokens,
            base_url="https://example.test/v1",
            model="target-test",
            api_key="secret",
            layout=base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
    assert target_analysis["status"] == "ok"
    assert target_analysis["api_calls"] == 1
    assert target_call.call_count == 1
    assert [caption["text"] for caption in target_analysis["captions"]] == ["你的工厂", "你的服务告诉AI"]
    target_request = target_call.call_args.args[0]
    target_request_body = json.loads(target_request.data.decode("utf-8"))
    assert "[-删除原文-]" in target_request_body["messages"][0]["content"]
    target_request_input = target_request_body["messages"][1]["content"]
    assert "你的工厂你的服务告诉AI" in target_request_input
    assert "排版参考容量" in target_request_input
    assert "真实字体全局选择" in target_request_input
    assert "带时间信息的只读口播视图" in target_request_input
    assert "ASR 话语段" in target_request_input
    assert "hard_end_i" not in target_request_input
    assert "response_format" not in target_request_body

    with patch("urllib.request.urlopen", return_value=FakeResponse(target_payload)) as flash_call:
        flash_analysis = analyze_transcript(
            holistic_target_tokens,
            base_url="https://example.test/v1",
            model="deepseek-v4-flash",
            api_key="secret",
            layout=base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
    assert flash_analysis["status"] == "ok"
    flash_request = flash_call.call_args.args[0]
    flash_request_body = json.loads(flash_request.data.decode("utf-8"))
    assert flash_request_body["thinking"] == {"type": "disabled"}

    with patch("urllib.request.urlopen", return_value=FakeResponse(target_payload)) as pro_call:
        pro_analysis = analyze_transcript(
            holistic_target_tokens,
            base_url="https://example.test/v1",
            model="deepseek-v4-pro",
            api_key="secret",
            layout=base_capacity,
            style=base_style,
            width=1080,
            height=1920,
        )
    assert pro_analysis["status"] == "ok"
    pro_request = pro_call.call_args.args[0]
    pro_request_body = json.loads(pro_request.data.decode("utf-8"))
    assert pro_request_body["thinking"] == {"type": "enabled"}
    assert pro_request_body["reasoning_effort"] == "high"
    assert pro_request_body["max_tokens"] == 32768
    assert "temperature" not in pro_request_body

    length_payload = {
        "choices": [{"message": {"content": "", "reasoning_content": "很长的推理"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292},
    }
    with patch("urllib.request.urlopen", return_value=FakeResponse(length_payload)):
        length_failure = analyze_transcript(
            holistic_target_tokens,
            base_url="https://example.test/v1",
            model="deepseek-v4-flash",
            api_key="secret",
            max_attempts=1,
        )
    assert length_failure["status"] == "skipped"
    assert "token 上限" in length_failure["reason"]
    failed_gate = _build_automatic_quality_gate(length_failure, [])
    assert len(failed_gate["blocking_reasons"]) == 1
    assert "token 上限" in failed_gate["blocking_reasons"][0]

    survival_tokens = tokens_from_text(
        "何必纠缠如果当初没有放弃我自己开的美甲店那我将不会进入美业",
        0.0,
        4.0,
        prefix="survival",
    )
    unsafe_survival = {
        "delete_ranges": [
            {
                "token_ids": [token["id"] for token in survival_tokens[4:20]],
                "source_indices": [4, 19],
                "type": "false_start",
                "confidence": 0.99,
            }
        ]
    }
    try:
        _validate_deletion_survival(unsafe_survival, survival_tokens)
    except ValueError as exc:
        assert "失去前置条件" in str(exc)
    else:
        raise AssertionError("Orphaned consequence deletion was accepted")

    safe_restart_tokens = tokens_from_text(
        "如果当初没如果当初没有放弃我自己开的美甲店那我将不会进入美业",
        0.0,
        4.0,
        prefix="safe-survival",
    )
    safe_survival = {
        "delete_ranges": [
            {
                "token_ids": [token["id"] for token in safe_restart_tokens[:6]],
                "source_indices": [0, 5],
                "type": "false_start",
                "confidence": 0.99,
            }
        ]
    }
    _validate_deletion_survival(safe_survival, safe_restart_tokens)

    copied_text_payload = llm_payload({
        "corrections": [],
        "deletions": [],
        "captions": [{"start_i": 0, "end_i": len(holistic_target_tokens) - 1, "display_text": "少抄一个字"}],
        "allowed_breaks": [],
    })
    with patch("urllib.request.urlopen", return_value=FakeResponse(copied_text_payload)):
        copied_text_analysis = analyze_transcript(
            holistic_target_tokens,
            base_url="https://example.test/v1",
            model="copied-text-test",
            api_key="secret",
        )
    assert copied_text_analysis["status"] == "skipped"
    assert any("只允许返回索引" in error for error in copied_text_analysis["retry_errors"])

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

    redundant_tokens = tokens_from_text("前面这个最后版本可以正常保留完整内容", 0.0, 2.0, prefix="redundant")
    redundant_segment = TimingSegment(0.0, 2.0, "前面这个最后版本可以正常保留完整内容", tuple(redundant_tokens))
    high_confidence_redundant = {
        "delete_ranges": [
            {
                "token_ids": [token["id"] for token in redundant_tokens[:4]],
                "type": "redundant",
                "confidence": 0.95,
                "reason": "前一版赘余",
            }
        ]
    }
    redundant_removed, redundant_ids = _ai_removal_segments(
        [redundant_segment], high_confidence_redundant, "standard"
    )
    assert redundant_removed and redundant_ids == {token["id"] for token in redundant_tokens[:4]}
    low_confidence_redundant = {
        "delete_ranges": [
            {
                "token_ids": [token["id"] for token in redundant_tokens[:4]],
                "type": "redundant",
                "confidence": 0.94,
                "reason": "置信度不足",
            }
        ]
    }
    low_redundant_removed, _ = _ai_removal_segments(
        [redundant_segment], low_confidence_redundant, "standard"
    )
    assert not low_redundant_removed

    dangerous_condition_tokens = tokens_from_text(
        "如果当初没有放弃美甲店那我将不会进入美业",
        0.0,
        3.0,
        prefix="dangerous-redundant",
    )
    dangerous_condition = {
        "delete_ranges": [
            {
                "token_ids": [token["id"] for token in dangerous_condition_tokens[:11]],
                "type": "redundant",
                "confidence": 0.99,
                "reason": "错误删除完整条件句",
            }
        ]
    }
    dangerous_removed, _ = _ai_removal_segments(
        [TimingSegment(0.0, 3.0, "", tuple(dangerous_condition_tokens))],
        dangerous_condition,
        "standard",
    )
    assert not dangerous_removed

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
    assert semantic_tokens[9]["id"] in preferred
    assert semantic_tokens[9]["id"] not in required
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
