from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from cut_silence import Segment, auto_cut
from disfluency import detect_repeated_utterances
from ffmpeg_utils import media_duration, require_tool, video_size
from make_cover import make_cover
from make_subtitle import TimingSegment, make_cues_from_timing_text, write_ass, write_srt
from render_video import burn_subtitles
from style_presets import get_style_preset
from text_utils import read_text, strip_keyword_marks
from volc_asr import convert_utterances, extract_wav, query_until_done, submit_audio


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chinese vertical video auto-cut MVP")
    parser.add_argument("--video", default="input/video.mp4", help="input video path")
    parser.add_argument("--script", default="input/script.txt", help="input script path")
    parser.add_argument("--title", default=None, help="optional title text or title file path")
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
    parser.add_argument("--export-subtitles", action="store_true", help="keep subtitle.ass and subtitle.srt in output")
    parser.add_argument("--export-asr-json", action="store_true", help="keep Volcengine segment JSON in output")
    parser.add_argument("--export-report", action="store_true", help="keep edit_report.json in output")
    parser.add_argument("--editor-work-dir", default=None, help="internal directory for visual editor source assets")
    parser.add_argument("--no-cut", action="store_true", help="skip silence cutting")
    parser.add_argument("--detect-disfluency", action="store_true", help="report repeated utterance candidates without cutting them")
    return parser.parse_args()


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
        title = _resolve_title(args.title, script_path, script)
        output_basename = _safe_output_basename(args.output_basename or f"{title}-{datetime.now():%Y%m%d}")
        style_preset = get_style_preset(args.style_preset, args.style_presets_file)
        subtitle_style = style_preset.get("subtitle", {})
        cover_title_style = style_preset.get("cover_title", {})
        output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="ai-cutting-") as tmp:
            working_video = Path(tmp) / "cut.mp4"
            if args.no_cut:
                shutil.copyfile(video, working_video)
                original_duration = media_duration(video)
                output_duration = original_duration
                keep_segments = [Segment(0.0, original_duration)]
                removed_segments: list[Segment] = []
            else:
                keep_segments, removed_segments, original_duration, output_duration = auto_cut(
                    video=video,
                    output=working_video,
                    noise=args.noise,
                    min_duration=args.min_silence,
                    padding=args.padding,
                )

            width, height = video_size(working_video)
            subtitle_duration = max(0.2, output_duration - 0.08)
            external_segments = []
            external_segments = _transcribe_cut_video_with_volcengine(
                working_video=working_video,
                tmp_dir=Path(tmp),
                appid=args.volc_appid,
                token=args.volc_token,
                words_per_line=args.volc_words_per_line,
                max_lines=args.volc_max_lines,
                timeout=args.volc_timeout,
            )
            if args.editor_work_dir:
                editor_work_dir = Path(args.editor_work_dir)
                editor_work_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(working_video, editor_work_dir / "cut_no_subtitles.mp4")
                _write_timing_segments(external_segments, editor_work_dir / "volcengine_segments.json")
            if args.export_asr_json:
                _write_timing_segments(external_segments, output_dir / "volcengine_segments.json")
            cues = make_cues_from_timing_text(
                external_segments,
                subtitle_duration,
                delay=args.subtitle_delay,
                target_len=int(subtitle_style.get("target_len", 12)),
                max_len=int(subtitle_style.get("max_len", 18)),
            )
            subtitle_srt = output_dir / "subtitle.srt"
            subtitle_ass = output_dir / "subtitle.ass" if args.export_subtitles else Path(tmp) / "subtitle.ass"
            if args.export_subtitles:
                write_srt(cues, subtitle_srt)
            write_ass(cues, subtitle_ass, width=width, height=height, style=subtitle_style)
            disfluency_findings = detect_repeated_utterances(external_segments) if args.detect_disfluency and args.export_report else []

            final_video = output_dir / f"{output_basename}.mp4"
            burn_subtitles(working_video, subtitle_ass, final_video)
            final_duration = media_duration(final_video)
            cover_path = output_dir / f"{output_basename}-\u5c01\u9762.jpg"
            make_cover(working_video, title, cover_path, style=cover_title_style)

        report = {
            "input": {
                "video": str(video.resolve()),
                "script": str(script_path.resolve()),
                "script_exists": script_path.exists(),
                "title": title,
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
                "timing_strategy": "cut video transcribed by Volcengine ASR",
            },
            "disfluency": {
                "repeat_candidates": disfluency_findings,
            },
        }
        if args.export_report:
            (output_dir / "edit_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        print(f"Done. Outputs written to {output_dir.resolve()}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


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


def _segment_dict(segment: Segment) -> dict[str, float]:
    return {
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "duration": round(segment.duration, 3),
    }


def _write_timing_segments(segments: list[TimingSegment], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "start_ms": round(segment.start * 1000),
            "end_ms": round(segment.end * 1000),
            "text": segment.text,
        }
        for segment in segments
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _transcribe_cut_video_with_volcengine(
    working_video: Path,
    tmp_dir: Path,
    appid: str | None,
    token: str | None,
    words_per_line: int,
    max_lines: int,
    timeout: float,
) -> list[TimingSegment]:
    import os

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
    return [
        TimingSegment(
            start=float(item["start_ms"]) / 1000,
            end=float(item["end_ms"]) / 1000,
            text=str(item["text"]),
        )
        for item in items
        if item.get("text")
    ]


if __name__ == "__main__":
    raise SystemExit(main())
