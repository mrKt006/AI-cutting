from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from cut_silence import Segment, build_keep_segments, cut_video, detect_silences
from ai_layout import layout_tokens_with_ai
from disfluency import detect_repeated_utterances
from ffmpeg_utils import media_duration, require_tool, video_size
from make_cover import make_cover
from llm_analysis import analyze_transcript, apply_high_confidence_corrections
from make_subtitle import TimingSegment, make_cues_from_segmented_timings, write_ass, write_srt
from render_video import burn_subtitles
from style_presets import get_style_preset
from subtitle_layout import flatten_segment_tokens, tokens_from_text, wrap_title_text
from text_utils import read_text, strip_keyword_marks
from transcript_alignment import align_transcript_tokens
from volc_asr import convert_utterances, extract_wav, query_until_done, submit_audio


LLM_CACHE_DIR = Path(__file__).resolve().parents[1] / "web" / ".cache" / "llm"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chinese vertical video auto-cut MVP")
    parser.add_argument("--video", default="input/video.mp4", help="input video path")
    parser.add_argument("--script", default="input/script.txt", help="input script path")
    parser.add_argument("--title", default=None, help="optional title text or title file path")
    parser.add_argument("--content-title", default=None, help="video content title text or file path")
    parser.add_argument("--cover-title", default=None, help="cover title text or file path")
    parser.add_argument("--output-dir", default="output", help="output directory")
    parser.add_argument("--output-basename", default=None, help="output file basename without extension")
    parser.add_argument("--noise", default="-30dB", help="silence threshold, e.g. -30dB")
    parser.add_argument("--min-silence", type=float, default=0.45, help="minimum silence duration in seconds")
    parser.add_argument("--padding", type=float, default=0.12, help="seconds kept around cut points")
    parser.add_argument("--subtitle-delay", type=float, default=0.0, help="delay subtitles by N seconds")
    parser.add_argument("--style-preset", default=None, help="subtitle/title style preset id")
    parser.add_argument("--style-presets-file", default=None, help="style preset JSON path")
    parser.add_argument("--subtitle-source", choices=["volcengine"], default="volcengine", help="subtitle text source")
    parser.add_argument("--volc-appid", default=None, help="Volcengine APP ID, or env VOLC_APP_ID")
    parser.add_argument("--volc-token", default=None, help="Volcengine Access Token, or env VOLC_ACCESS_TOKEN")
    parser.add_argument("--volc-words-per-line", type=int, default=15, help="Volc subtitle words_per_line parameter")
    parser.add_argument("--volc-max-lines", type=int, default=1, help="Volc subtitle max_lines parameter")
    parser.add_argument("--volc-timeout", type=float, default=600.0, help="max seconds to wait for Volcengine ASR")
    parser.add_argument("--llm-enabled", action="store_true", help="analyze transcript with an OpenAI-compatible model")
    parser.add_argument("--llm-base-url", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--llm-model", default=None, help="OpenAI-compatible model name")
    parser.add_argument("--llm-timeout", type=float, default=60.0, help="LLM request timeout")
    parser.add_argument("--auto-edit-mode", choices=["conservative", "standard", "aggressive"], default="standard", help="automatic AI editing strength")
    parser.add_argument("--export-subtitles", action="store_true", help="keep subtitle.ass and subtitle.srt in output")
    parser.add_argument("--export-asr-json", action="store_true", help="keep Volcengine segment JSON in output")
    parser.add_argument("--export-report", action="store_true", help="keep edit_report.json in output")
    parser.add_argument("--editor-work-dir", default=None, help="internal directory for visual editor source assets")
    parser.add_argument("--control-file", default=None, help="cooperative task control JSON path")
    parser.add_argument("--checkpoint-dir", default=None, help="persistent stage checkpoint directory")
    parser.add_argument("--no-cut", action="store_true", help="skip silence cutting")
    parser.add_argument("--detect-disfluency", action="store_true", help="report repeated utterance candidates without cutting them")
    return parser.parse_args()


class PauseRequested(RuntimeError):
    pass


class CancelRequested(RuntimeError):
    pass


def main() -> int:
    args = parse_args()
    video = Path(args.video)
    script_path = Path(args.script)
    output_dir = Path(args.output_dir)

    try:
        require_tool("ffmpeg")
        require_tool("ffprobe")
        _require_file(video, "video")
        script = read_text(script_path) if script_path.exists() else ""
        legacy_title = _resolve_title(args.title, script_path, script)
        content_title = _resolve_optional_text(args.content_title) or legacy_title
        cover_title = _resolve_optional_text(args.cover_title) or content_title
        output_basename = _safe_output_basename(args.output_basename or f"{cover_title}-{datetime.now():%Y%m%d}")
        style_preset = get_style_preset(args.style_preset, args.style_presets_file)
        subtitle_style = style_preset.get("subtitle", {})
        video_title_style = style_preset.get("video_title", {})
        cover_title_style = style_preset.get("cover_title", {})
        output_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_dir = Path(args.checkpoint_dir) if args.checkpoint_dir else None
        _stage_checkpoint(args, "validated")

        with tempfile.TemporaryDirectory(prefix="ai-cutting-") as tmp:
            working_video = Path(tmp) / "cut.mp4"
            original_duration = media_duration(video)
            asr_checkpoint = _load_checkpoint(checkpoint_dir, "asr")
            if asr_checkpoint:
                raw_segments = _timing_segments_from_data(asr_checkpoint.get("segments"))
                raw_asr = asr_checkpoint.get("response") or {}
            else:
                raw_segments, raw_asr = _transcribe_cut_video_with_volcengine(
                    working_video=video,
                    tmp_dir=Path(tmp),
                    appid=args.volc_appid,
                    token=args.volc_token,
                    words_per_line=args.volc_words_per_line,
                    max_lines=args.volc_max_lines,
                    timeout=args.volc_timeout,
                )
                _save_checkpoint(checkpoint_dir, "asr", {"segments": _timing_segments_data(raw_segments), "response": raw_asr})
            _stage_checkpoint(args, "asr")

            alignment_checkpoint = _load_checkpoint(checkpoint_dir, "alignment")
            if alignment_checkpoint:
                aligned_segments = _timing_segments_from_data(alignment_checkpoint.get("segments"))
                transcript_alignment = alignment_checkpoint.get("report") or {}
            else:
                aligned_segments, transcript_alignment = _align_segments_to_script(raw_segments, script)
                _save_checkpoint(
                    checkpoint_dir,
                    "alignment",
                    {"segments": _timing_segments_data(aligned_segments), "report": transcript_alignment},
                )
            _stage_checkpoint(args, "alignment")

            analysis = _load_checkpoint(checkpoint_dir, "analysis")
            if not analysis:
                analysis = _run_transcript_analysis(aligned_segments, args)
                _save_checkpoint(checkpoint_dir, "analysis", analysis)
            analysis["transcript_alignment_status"] = transcript_alignment.get("status")
            _stage_checkpoint(args, "analysis")
            ai_removed, removed_token_ids = _ai_removal_segments(aligned_segments, analysis, args.auto_edit_mode)
            if args.no_cut:
                removed_segments = []
                keep_segments = [Segment(0.0, original_duration)]
            else:
                silences = detect_silences(video, args.noise, args.min_silence)
                _, silence_removed = build_keep_segments(original_duration, silences, args.padding)
                silence_removed = _protect_speech_from_silence(silence_removed, aligned_segments, removed_token_ids)
                removed_segments = _merge_removed_segments([*silence_removed, *ai_removed], original_duration)
                keep_segments = _keep_from_removed(original_duration, removed_segments)
            _stage_checkpoint(args, "edit_plan")
            cut_video(video, working_video, keep_segments)
            output_duration = media_duration(working_video)
            width, height = video_size(working_video)
            title_checkpoint = _load_checkpoint(checkpoint_dir, "titles")
            if title_checkpoint:
                content_title_analysis = title_checkpoint.get("content_analysis") or {}
                cover_title_analysis = title_checkpoint.get("cover_analysis") or {}
                video_title_text = str(title_checkpoint.get("video_text") or content_title)
                cover_title_text = str(title_checkpoint.get("cover_text") or cover_title)
                video_title_layout = title_checkpoint.get("video_layout") or {}
                cover_title_layout = title_checkpoint.get("cover_layout") or {}
            else:
                content_title_analysis = _analyze_title(content_title, args, prefix="content-title")
                cover_title_analysis = (
                    content_title_analysis
                    if cover_title == content_title
                    else _analyze_title(cover_title, args, prefix="cover-title")
                )
                video_title_text, video_title_layout = _intelligent_title(
                    content_title, video_title_style, width, height, content_title_analysis, args
                )
                cover_title_text, cover_title_layout = _intelligent_title(
                    cover_title, cover_title_style, width, height, cover_title_analysis, args
                )
                _save_checkpoint(
                    checkpoint_dir,
                    "titles",
                    {
                        "content_analysis": content_title_analysis,
                        "cover_analysis": cover_title_analysis,
                        "video_text": video_title_text,
                        "cover_text": cover_title_text,
                        "video_layout": video_title_layout,
                        "cover_layout": cover_title_layout,
                    },
                )
            _stage_checkpoint(args, "title_layout")
            subtitle_duration = max(0.2, output_duration - 0.08)
            external_segments = _map_retained_tokens_to_cut_timeline(aligned_segments, keep_segments, removed_token_ids)
            external_segments = _intelligent_segments(
                external_segments,
                subtitle_style,
                width,
                height,
                analysis,
                args,
            )
            if args.editor_work_dir:
                editor_work_dir = Path(args.editor_work_dir)
                editor_work_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(working_video, editor_work_dir / "cut_no_subtitles.mp4")
                _write_timing_segments(external_segments, editor_work_dir / "volcengine_segments.json")
                (editor_work_dir / "volcengine_response.json").write_text(
                    json.dumps(raw_asr, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (editor_work_dir / "transcript_analysis.json").write_text(
                    json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (editor_work_dir / "ai_decisions.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "transcript_analysis": analysis.get("decision_traces", []),
                            "subtitle_layout": analysis.get("layout_decision", {}),
                            "content_title_analysis": content_title_analysis.get("decision_traces", []),
                            "content_title_layout": video_title_layout,
                            "cover_title_analysis": cover_title_analysis.get("decision_traces", []),
                            "cover_title_layout": cover_title_layout,
                            "applied_deletions": analysis.get("applied_delete_ranges", []),
                            "rejected_deletions": analysis.get("skipped_delete_ranges", []),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                (editor_work_dir / "transcript_alignment.json").write_text(
                    json.dumps(transcript_alignment, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (editor_work_dir / "title_layout.json").write_text(
                    json.dumps(
                        {
                            "source_text": legacy_title,
                            "content_source_text": content_title,
                            "cover_source_text": cover_title,
                            "video_text": video_title_text,
                            "cover_text": cover_title_text,
                            "content_analysis": content_title_analysis,
                            "cover_analysis": cover_title_analysis,
                            "video_layout": video_title_layout,
                            "cover_layout": cover_title_layout,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                _write_timing_segments(raw_segments, editor_work_dir / "raw_transcript_segments.json")
                _write_timing_segments(aligned_segments, editor_work_dir / "aligned_transcript_segments.json")
                (editor_work_dir / "auto_edit_plan.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "auto_edit_mode": args.auto_edit_mode,
                            "original_duration": round(original_duration, 3),
                            "output_duration": round(output_duration, 3),
                            "keep_segments": [_segment_dict(segment) for segment in keep_segments],
                            "removed_segments": [_segment_dict(segment) for segment in removed_segments],
                            "ai_delete_ranges": analysis.get("applied_delete_ranges", []),
                            "skipped_ai_delete_ranges": analysis.get("skipped_delete_ranges", []),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            if args.export_asr_json:
                _write_timing_segments(external_segments, output_dir / "volcengine_segments.json")
            cues = make_cues_from_segmented_timings(external_segments, subtitle_duration, delay=args.subtitle_delay)
            subtitle_srt = output_dir / "subtitle.srt"
            subtitle_ass = output_dir / "subtitle.ass" if args.export_subtitles else Path(tmp) / "subtitle.ass"
            if args.export_subtitles:
                write_srt(cues, subtitle_srt)
            title_end = output_duration
            if video_title_style.get("display_mode") == "intro":
                title_end = min(output_duration, max(0.2, float(video_title_style.get("display_duration", 3.0))))
            write_ass(
                cues,
                subtitle_ass,
                width=width,
                height=height,
                style=subtitle_style,
                title_text=video_title_text,
                title_style=video_title_style,
                title_end=title_end,
            )
            disfluency_findings = detect_repeated_utterances(external_segments) if args.detect_disfluency and args.export_report else []

            final_video = output_dir / f"{output_basename}.mp4"
            burn_subtitles(working_video, subtitle_ass, final_video)
            final_duration = media_duration(final_video)
            _stage_checkpoint(args, "video_rendered")
            cover_path = output_dir / f"{output_basename}-\u5c01\u9762.jpg"
            make_cover(working_video, cover_title_text, cover_path, style=cover_title_style)
            _stage_checkpoint(args, "cover_rendered")

        report = {
            "input": {
                "video": str(video.resolve()),
                "script": str(script_path.resolve()),
                "script_exists": script_path.exists(),
                "title": legacy_title,
                "content_title": content_title,
                "cover_title": cover_title,
            },
            "outputs": {
                "final_video": str(final_video.resolve()),
                "cover": str(cover_path.resolve()),
                "subtitle_ass": str((output_dir / "subtitle.ass").resolve()) if args.export_subtitles else None,
                "subtitle_srt": str((output_dir / "subtitle.srt").resolve()) if args.export_subtitles else None,
                "volcengine_segments": str((output_dir / "volcengine_segments.json").resolve()) if args.export_asr_json else None,
            },
            "durations": {
                "original_seconds": round(original_duration, 3),
                "cut_seconds": round(output_duration, 3),
                "output_seconds": round(final_duration, 3),
                "subtitle_timeline_seconds": round(subtitle_duration, 3),
                "removed_seconds": round(max(0.0, original_duration - output_duration), 3),
            },
            "segments": {
                "kept": [_segment_dict(segment) for segment in keep_segments],
                "removed": [_segment_dict(segment) for segment in removed_segments],
            },
            "parameters": {
                "noise": args.noise,
                "min_silence": args.min_silence,
                "padding": args.padding,
                "subtitle_delay": args.subtitle_delay,
                "subtitle_source": "volcengine",
                "output_basename": output_basename,
                "style_preset": style_preset.get("id"),
                "style_preset_name": style_preset.get("name"),
                "volc_words_per_line": args.volc_words_per_line,
                "volc_max_lines": args.volc_max_lines,
                "volc_timeout": args.volc_timeout,
                "export_subtitles": args.export_subtitles,
                "export_asr_json": args.export_asr_json,
                "export_report": args.export_report,
                "external_segment_count": len(external_segments),
                "no_cut": args.no_cut,
                "detect_disfluency": args.detect_disfluency,
                "timing_strategy": "original video ASR followed by AI and silence edit decision mapping",
                "llm_enabled": bool(args.llm_enabled),
                "llm_status": analysis.get("status"),
                "auto_edit_mode": args.auto_edit_mode,
            },
            "disfluency": {
                "repeat_candidates": disfluency_findings,
            },
            "analysis": analysis,
            "transcript_alignment": transcript_alignment,
        }
        if args.export_report:
            (output_dir / "edit_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(f"Done. Outputs written to {output_dir.resolve()}")
        return 0
    except PauseRequested as exc:
        print(f"Paused: {exc}")
        return 75
    except CancelRequested as exc:
        print(f"Cancelled: {exc}")
        return 76
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def _stage_checkpoint(args: argparse.Namespace, stage: str) -> None:
    checkpoint_dir = Path(args.checkpoint_dir) if args.checkpoint_dir else None
    if checkpoint_dir:
        _save_checkpoint(checkpoint_dir, "state", {"completed_stage": stage, "updated_at": datetime.now().isoformat(timespec="seconds")})
    if not args.control_file:
        return
    control_path = Path(args.control_file)
    if not control_path.exists():
        return
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if control.get("cancel_requested"):
        raise CancelRequested(f"已在 {stage} 阶段结束后安全取消")
    if control.get("pause_requested"):
        raise PauseRequested(f"已在 {stage} 阶段结束后安全暂停")


def _checkpoint_path(checkpoint_dir: Path | None, name: str) -> Path | None:
    return checkpoint_dir / f"{name}.json" if checkpoint_dir else None


def _save_checkpoint(checkpoint_dir: Path | None, name: str, payload: object) -> None:
    path = _checkpoint_path(checkpoint_dir, name)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp.json")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _load_checkpoint(checkpoint_dir: Path | None, name: str) -> dict:
    path = _checkpoint_path(checkpoint_dir, name)
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")


def _resolve_title(title_arg: str | None, script_path: Path, script: str) -> str:
    if title_arg:
        candidate = Path(title_arg)
        if candidate.exists():
            return strip_keyword_marks(read_text(candidate)).strip()
        return strip_keyword_marks(title_arg).strip()

    title_file = script_path.with_name("title.txt")
    if title_file.exists():
        return strip_keyword_marks(read_text(title_file)).strip()

    for line in script.splitlines():
        line = strip_keyword_marks(line).strip()
        if line:
            return line
    return "未命名视频"


def _resolve_optional_text(value: str | None) -> str:
    if not value:
        return ""
    candidate = Path(value)
    if candidate.exists():
        return strip_keyword_marks(read_text(candidate)).strip()
    return strip_keyword_marks(value).strip()


def _analyze_title(title: str, args: argparse.Namespace, prefix: str) -> dict:
    lines = [line.strip() for line in title.replace("\r", "").split("\n") if line.strip()]
    tokens = [
        token
        for line_index, line in enumerate(lines or [title], start=1)
        for token in tokens_from_text(line, 0.0, 1.0, prefix=f"{prefix}-{line_index}")
    ]
    return _run_transcript_analysis([TimingSegment(0.0, 1.0, title, tuple(tokens))], args)


def _segment_dict(segment: Segment) -> dict[str, float]:
    return {
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "duration": round(segment.duration, 3),
    }


def _write_timing_segments(segments: list[TimingSegment], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_timing_segments_data(segments), ensure_ascii=False, indent=2), encoding="utf-8")


def _timing_segments_data(segments: list[TimingSegment]) -> list[dict]:
    return [
        {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "start_ms": round(segment.start * 1000),
            "end_ms": round(segment.end * 1000),
            "text": segment.text,
            "tokens": list(segment.tokens),
        }
        for segment in segments
    ]


def _timing_segments_from_data(data: object) -> list[TimingSegment]:
    if not isinstance(data, list):
        return []
    segments: list[TimingSegment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            start_value = item.get("start")
            end_value = item.get("end")
            start = float(start_value) if start_value is not None else float(item.get("start_ms", 0)) / 1000
            end = float(end_value) if end_value is not None else float(item.get("end_ms", 0)) / 1000
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        raw_tokens = item.get("tokens")
        tokens = tuple(token for token in raw_tokens if isinstance(token, dict)) if isinstance(raw_tokens, list) else ()
        segments.append(TimingSegment(start=start, end=end, text=str(item.get("text") or ""), tokens=tokens))
    return segments


def _align_segments_to_script(
    segments: list[TimingSegment], script: str
) -> tuple[list[TimingSegment], dict]:
    source_tokens = flatten_segment_tokens(segments)
    if not script.strip() or not source_tokens:
        return segments, {
            "version": 1,
            "status": "skipped",
            "reason": "missing_transcript" if not script.strip() else "missing_asr_tokens",
            "corrections": [],
            "asr_only": [],
            "script_only": [],
            "ambiguous": [],
            "manual_break_token_ids": [],
        }
    aligned_tokens, report = align_transcript_tokens(source_tokens, script)
    result: list[TimingSegment] = []
    cursor = 0
    for segment in segments:
        token_count = len(segment.tokens)
        segment_tokens = aligned_tokens[cursor : cursor + token_count]
        cursor += token_count
        if not segment_tokens:
            result.append(segment)
            continue
        result.append(
            TimingSegment(
                start=segment.start,
                end=segment.end,
                text="".join(str(token.get("text") or "") for token in segment_tokens),
                tokens=tuple(segment_tokens),
            )
        )
    return result, report


def _safe_output_basename(value: str) -> str:
    value = strip_keyword_marks(value).strip()
    forbidden = '<>:"/\\|?*'
    cleaned = "".join("-" if char in forbidden or ord(char) < 32 else char for char in value)
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:80] or f"\u672a\u547d\u540d\u89c6\u9891-{datetime.now():%Y%m%d}"


def _map_segments_to_cut_timeline(segments: list[TimingSegment], keep_segments: list[Segment]) -> list[TimingSegment]:
    if not segments or not keep_segments:
        return segments

    mapped: list[TimingSegment] = []
    for segment in segments:
        start = _map_time_to_cut_timeline(segment.start, keep_segments, prefer_next=True)
        end = _map_time_to_cut_timeline(segment.end, keep_segments, prefer_next=False)
        if end <= start:
            end = start + min(0.6, max(0.2, segment.end - segment.start))
        mapped.append(TimingSegment(start=start, end=end, text=segment.text))
    return mapped


def _map_time_to_cut_timeline(time_value: float, keep_segments: list[Segment], prefer_next: bool) -> float:
    elapsed = 0.0
    for segment in keep_segments:
        if segment.start <= time_value <= segment.end:
            return elapsed + max(0.0, time_value - segment.start)
        if time_value < segment.start:
            return elapsed if prefer_next else max(0.0, elapsed)
        elapsed += segment.duration
    return elapsed


def _ai_removal_segments(
    segments: list[TimingSegment], analysis: dict, mode: str
) -> tuple[list[Segment], set[str]]:
    tokens = flatten_segment_tokens(segments)
    token_index = {str(token.get("id")): index for index, token in enumerate(tokens)}
    policies = {
        "conservative": ({"stutter", "false_start", "exact_repeat"}, 0.9),
        "standard": ({"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler"}, 0.82),
        "aggressive": ({"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler", "redundant"}, 0.72),
    }
    allowed_types, threshold = policies.get(mode, policies["standard"])
    type_thresholds = {
        "stutter": threshold,
        "false_start": max(threshold, 0.86),
        "exact_repeat": max(threshold, 0.88),
        "semantic_repeat": 0.94 if mode == "standard" else threshold,
        "filler": max(threshold, 0.9),
        "redundant": max(threshold, 0.9),
    }
    protected_ids = _protected_delete_token_ids(tokens, analysis)
    removed_ids: set[str] = set()
    removed: list[Segment] = []
    applied: list[dict] = []
    skipped: list[dict] = []
    for operation in analysis.get("delete_ranges", []):
        kind = str(operation.get("type") or "redundant")
        confidence = float(operation.get("confidence", 0))
        ids = [str(item) for item in operation.get("token_ids", []) if str(item) in token_index]
        if kind not in allowed_types or confidence < type_thresholds.get(kind, threshold) or not ids:
            skipped.append({**operation, "skip_reason": "policy_or_confidence"})
            continue
        indices = sorted({token_index[token_id] for token_id in ids})
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            skipped.append({**operation, "skip_reason": "non_contiguous_tokens"})
            continue
        run_ids = [str(tokens[index].get("id")) for index in indices]
        if set(run_ids) & protected_ids:
            skipped.append({**operation, "skip_reason": "protected_content"})
            continue
        if set(run_ids) & removed_ids:
            skipped.append({**operation, "skip_reason": "overlapping_delete"})
            continue
        evidence = _delete_evidence(kind, indices, tokens, mode)
        if not evidence["accepted"]:
            skipped.append({**operation, "skip_reason": evidence["reason"]})
            continue
        if len(removed_ids) + len(run_ids) > max(1, int(len(tokens) * 0.4)):
            skipped.append({**operation, "skip_reason": "deletion_budget_exceeded"})
            continue
        first = tokens[indices[0]]
        last = tokens[indices[-1]]
        start = max(0.0, float(first.get("start", 0)) - 0.025)
        end = max(start + 0.04, float(last.get("end", start)) + 0.025)
        removed.append(Segment(start, end))
        removed_ids.update(run_ids)
        applied.append(
            {
                **operation,
                "token_ids": run_ids,
                "start": round(start, 3),
                "end": round(end, 3),
                "validation": evidence["reason"],
            }
        )
    analysis["auto_edit_mode"] = mode
    analysis["applied_delete_ranges"] = applied
    analysis["skipped_delete_ranges"] = skipped
    return removed, removed_ids


def _protected_delete_token_ids(tokens: list[dict], analysis: dict) -> set[str]:
    protected = {
        str(token_id)
        for span in analysis.get("protected_spans", [])
        if float(span.get("confidence", 0)) >= 0.8
        for token_id in span.get("token_ids", [])
    }
    for token in tokens:
        text = str(token.get("text") or "")
        if re.search(r"\d", text) or re.fullmatch(r"[A-Za-z][A-Za-z0-9._+-]*", text):
            protected.add(str(token.get("id") or ""))
    return protected


def _delete_evidence(kind: str, indices: list[int], tokens: list[dict], mode: str) -> dict[str, object]:
    deleted = "".join(str(tokens[index].get("text") or "") for index in indices).strip()
    right_tokens = tokens[indices[-1] + 1 : indices[-1] + 1 + max(6, len(indices) + 2)]
    right = "".join(str(token.get("text") or "") for token in right_tokens).strip()
    if not deleted or not right and kind not in {"filler", "redundant"}:
        return {"accepted": False, "reason": "missing_adjacent_evidence"}
    if kind == "stutter":
        repeated = right.startswith(deleted) or right.startswith(deleted[-1])
        return {"accepted": repeated, "reason": "adjacent_stutter_match" if repeated else "stutter_not_repeated"}
    if kind == "exact_repeat":
        repeated = right.startswith(deleted)
        return {"accepted": repeated, "reason": "adjacent_exact_repeat" if repeated else "exact_repeat_not_found"}
    if kind == "false_start":
        shared = _common_prefix_length(deleted, right)
        accepted = shared >= min(2, len(deleted))
        return {"accepted": accepted, "reason": "adjacent_restart_match" if accepted else "restart_prefix_not_found"}
    if kind == "filler":
        accepted = deleted in {"嗯", "啊", "呃", "额", "那个", "这个", "就是"}
        return {"accepted": accepted, "reason": "known_filler" if accepted else "unknown_filler"}
    if kind == "semantic_repeat":
        return {"accepted": mode in {"standard", "aggressive"}, "reason": "high_confidence_semantic_repeat"}
    if kind == "redundant":
        return {"accepted": mode == "aggressive", "reason": "aggressive_redundancy" if mode == "aggressive" else "redundancy_requires_aggressive"}
    return {"accepted": False, "reason": "unsupported_delete_type"}


def _common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def _merge_removed_segments(segments: list[Segment], duration: float) -> list[Segment]:
    ordered = sorted(
        (Segment(max(0.0, item.start), min(duration, item.end)) for item in segments if item.end > item.start),
        key=lambda item: item.start,
    )
    merged: list[Segment] = []
    for segment in ordered:
        if merged and segment.start <= merged[-1].end + 0.04:
            merged[-1] = Segment(merged[-1].start, max(merged[-1].end, segment.end))
        else:
            merged.append(segment)
    return [item for item in merged if item.duration >= 0.04]


def _protect_speech_from_silence(
    silences: list[Segment], segments: list[TimingSegment], removed_token_ids: set[str]
) -> list[Segment]:
    protected = [
        Segment(max(0.0, float(token.get("start", 0)) - 0.015), float(token.get("end", 0)) + 0.015)
        for token in flatten_segment_tokens(segments)
        if str(token.get("id") or "") not in removed_token_ids
        and float(token.get("end", 0)) > float(token.get("start", 0))
    ]
    result: list[Segment] = []
    for silence in silences:
        pieces = [silence]
        for speech in protected:
            if speech.end <= silence.start or speech.start >= silence.end:
                continue
            next_pieces: list[Segment] = []
            for piece in pieces:
                if speech.end <= piece.start or speech.start >= piece.end:
                    next_pieces.append(piece)
                    continue
                if speech.start > piece.start:
                    next_pieces.append(Segment(piece.start, min(piece.end, speech.start)))
                if speech.end < piece.end:
                    next_pieces.append(Segment(max(piece.start, speech.end), piece.end))
            pieces = next_pieces
            if not pieces:
                break
        result.extend(piece for piece in pieces if piece.duration >= 0.04)
    return result


def _keep_from_removed(duration: float, removed: list[Segment]) -> list[Segment]:
    keep: list[Segment] = []
    cursor = 0.0
    for segment in removed:
        if segment.start > cursor + 0.04:
            keep.append(Segment(cursor, segment.start))
        cursor = max(cursor, segment.end)
    if cursor < duration - 0.04:
        keep.append(Segment(cursor, duration))
    return keep or [Segment(0.0, duration)]


def _map_retained_tokens_to_cut_timeline(
    segments: list[TimingSegment], keep_segments: list[Segment], removed_token_ids: set[str]
) -> list[TimingSegment]:
    source_tokens = flatten_segment_tokens(segments)
    mapped_tokens: list[dict] = []
    for token in source_tokens:
        token_id = str(token.get("id") or "")
        if token_id in removed_token_ids:
            continue
        start = float(token.get("start", 0))
        end = float(token.get("end", start))
        overlaps = [
            Segment(max(start, segment.start), min(end, segment.end))
            for segment in keep_segments
            if min(end, segment.end) > max(start, segment.start)
        ]
        if not overlaps:
            continue
        mapped = dict(token)
        mapped["source_start"] = start
        mapped["source_end"] = end
        mapped["start"] = _map_time_to_cut_timeline(overlaps[0].start, keep_segments, prefer_next=True)
        mapped["end"] = _map_time_to_cut_timeline(overlaps[-1].end, keep_segments, prefer_next=False)
        if mapped["end"] <= mapped["start"]:
            mapped["end"] = mapped["start"] + max(0.04, min(0.3, end - start))
        mapped_tokens.append(mapped)
    _assert_token_conservation(source_tokens, mapped_tokens, removed_token_ids)
    if not mapped_tokens:
        return [] if source_tokens else _map_segments_to_cut_timeline(segments, keep_segments)
    return [
        TimingSegment(
            start=float(mapped_tokens[0]["start"]),
            end=float(mapped_tokens[-1]["end"]),
            text="".join(str(token.get("text") or "") for token in mapped_tokens),
            tokens=tuple(mapped_tokens),
        )
    ]


def _assert_token_conservation(
    source_tokens: list[dict], mapped_tokens: list[dict], removed_token_ids: set[str]
) -> None:
    source_ids = [str(token.get("id") or "") for token in source_tokens if str(token.get("id") or "")]
    if not source_ids:
        return
    expected_ids = [token_id for token_id in source_ids if token_id not in removed_token_ids]
    mapped_ids = [str(token.get("id") or "") for token in mapped_tokens if str(token.get("id") or "")]
    if mapped_ids == expected_ids:
        return

    expected_set = set(expected_ids)
    mapped_set = set(mapped_ids)
    missing = [token_id for token_id in expected_ids if token_id not in mapped_set]
    unexpected = [token_id for token_id in mapped_ids if token_id not in expected_set]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for token_id in mapped_ids:
        if token_id in seen:
            duplicates.add(token_id)
        seen.add(token_id)
    order_changed = not missing and not unexpected and not duplicates and mapped_ids != expected_ids
    details = []
    if missing:
        details.append(f"missing={','.join(missing[:12])}")
    if unexpected:
        details.append(f"unexpected={','.join(unexpected[:12])}")
    if duplicates:
        details.append(f"duplicates={','.join(sorted(duplicates)[:12])}")
    if order_changed:
        details.append("order_changed=true")
    raise RuntimeError("Subtitle token integrity check failed: " + "; ".join(details))


def _transcribe_cut_video_with_volcengine(
    working_video: Path,
    tmp_dir: Path,
    appid: str | None,
    token: str | None,
    words_per_line: int,
    max_lines: int,
    timeout: float,
) -> tuple[list[TimingSegment], dict]:

    appid = appid or os.environ.get("VOLC_APP_ID")
    token = token or os.environ.get("VOLC_ACCESS_TOKEN")
    if not appid:
        raise RuntimeError("Missing Volcengine APP ID. Set VOLC_APP_ID or pass --volc-appid")
    if not token:
        raise RuntimeError("Missing Volcengine Access Token. Set VOLC_ACCESS_TOKEN or pass --volc-token")

    audio_path = tmp_dir / "volc_audio.wav"
    extract_wav(working_video, audio_path)
    task_id = submit_audio(
        audio_path=audio_path,
        appid=appid,
        token=token,
        words_per_line=words_per_line,
        max_lines=max_lines,
    )
    result = query_until_done(
        appid=appid,
        token=token,
        task_id=task_id,
        blocking=1,
        poll_interval=2.0,
        timeout=timeout,
    )
    items = convert_utterances(result)
    segments = [
        TimingSegment(
            start=float(item["start_ms"]) / 1000,
            end=float(item["end_ms"]) / 1000,
            text=str(item["text"]),
            tokens=tuple(item.get("tokens") or ()),
        )
        for item in items
        if item.get("text")
    ]
    return segments, result


def _run_transcript_analysis(segments: list[TimingSegment], args: argparse.Namespace) -> dict:
    if not args.llm_enabled:
        return {"status": "skipped", "reason": "disabled", "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": [], "delete_ranges": [], "final_sentences": []}
    return analyze_transcript(
        flatten_segment_tokens(segments),
        base_url=str(args.llm_base_url or os.environ.get("AI_CUTTING_LLM_BASE_URL") or ""),
        model=str(args.llm_model or os.environ.get("AI_CUTTING_LLM_MODEL") or ""),
        api_key=str(os.environ.get("AI_CUTTING_LLM_API_KEY") or ""),
        timeout=float(args.llm_timeout),
        cache_dir=LLM_CACHE_DIR,
    )


def _intelligent_segments(
    segments: list[TimingSegment],
    style: dict,
    width: int,
    height: int,
    analysis: dict,
    args: argparse.Namespace,
) -> list[TimingSegment]:
    tokens = flatten_segment_tokens(segments)
    apply_high_confidence_corrections(tokens, analysis)
    groups, layout_audit = layout_tokens_with_ai(
        tokens,
        style,
        width,
        height,
        analysis,
        base_url=str(args.llm_base_url or os.environ.get("AI_CUTTING_LLM_BASE_URL") or ""),
        model=str(args.llm_model or os.environ.get("AI_CUTTING_LLM_MODEL") or ""),
        api_key=str(os.environ.get("AI_CUTTING_LLM_API_KEY") or ""),
        timeout=float(args.llm_timeout),
        cache_dir=LLM_CACHE_DIR,
    )
    analysis["layout_decision"] = layout_audit
    result: list[TimingSegment] = []
    for group in groups:
        visible = [token for token in group if str(token.get("text") or "")]
        if not visible:
            continue
        result.append(
            TimingSegment(
                start=float(visible[0].get("start", 0)),
                end=float(visible[-1].get("end", visible[0].get("start", 0))),
                text="".join(str(token.get("text") or "") for token in visible).strip(),
                tokens=tuple(visible),
            )
        )
    return result or segments


def _intelligent_title(
    title: str,
    style: dict,
    width: int,
    height: int,
    analysis: dict,
    args: argparse.Namespace,
) -> tuple[str, dict]:
    lines: list[str] = []
    audits: list[dict] = []
    source_lines = [line.strip() for line in str(title or "").replace("\r", "").split("\n") if line.strip()]
    for line_index, line in enumerate(source_lines or [str(title or "")], start=1):
        tokens = tokens_from_text(line, 0.0, 1.0, prefix=f"title-{line_index}")
        groups, audit = layout_tokens_with_ai(
            tokens,
            style,
            width,
            height,
            analysis,
            base_url=str(args.llm_base_url or os.environ.get("AI_CUTTING_LLM_BASE_URL") or ""),
            model=str(args.llm_model or os.environ.get("AI_CUTTING_LLM_MODEL") or ""),
            api_key=str(os.environ.get("AI_CUTTING_LLM_API_KEY") or ""),
            timeout=float(args.llm_timeout),
            cache_dir=LLM_CACHE_DIR,
        )
        lines.extend("".join(str(token.get("text") or "") for token in group) for group in groups)
        audits.append(audit)
    return "\n".join(lines).strip() or wrap_title_text(title, style, width, height, analysis), {"status": "ai", "chunks": audits}


if __name__ == "__main__":
    raise SystemExit(main())
