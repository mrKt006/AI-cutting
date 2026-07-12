from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from style_presets import DEFAULT_STYLE_PRESETS, subtitle_override, subtitle_to_ass_style
from text_utils import MARK_RE, split_script, strip_keyword_marks


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str
    style: dict[str, Any] | None = None


@dataclass(frozen=True)
class TimingSegment:
    start: float
    end: float
    text: str = ""
    tokens: tuple[dict, ...] = ()


def make_cues(script: str, duration: float, delay: float = 0.0) -> list[SubtitleCue]:
    chunks = split_script(script)
    if not chunks or duration <= 0:
        return []

    delay = max(0.0, min(delay, duration - 0.2))
    spoken_duration = max(0.2, duration - delay)
    weights = [max(4, len(strip_keyword_marks(chunk))) for chunk in chunks]
    total_weight = sum(weights)
    min_duration = min(0.8, spoken_duration / len(chunks))
    durations = [max(min_duration, spoken_duration * weight / total_weight) for weight in weights]
    scale = spoken_duration / sum(durations)
    durations = [item * scale for item in durations]

    cursor = delay
    cues: list[SubtitleCue] = []
    for index, (chunk, cue_duration) in enumerate(zip(chunks, durations), start=1):
        end = min(duration, cursor + cue_duration)
        if index == len(chunks):
            end = duration
        cues.append(SubtitleCue(index=index, start=cursor, end=max(cursor, end), text=chunk))
        cursor = end
    return cues


def make_cues_from_timings(script: str, timings: list[TimingSegment], duration: float, delay: float = 0.0) -> list[SubtitleCue]:
    chunks = split_script(script)
    timings = [item for item in timings if item.end > item.start]
    if not chunks or not timings or duration <= 0:
        return make_cues(script, duration, delay=delay)

    delay = max(0.0, delay)
    if len(chunks) > len(timings):
        return _make_cues_across_speech_window(chunks, timings, duration, delay)

    merged_timings = _fit_timing_count(timings, len(chunks))
    cues: list[SubtitleCue] = []
    for index, (chunk, timing) in enumerate(zip(chunks, merged_timings), start=1):
        start = max(0.0, min(duration, timing.start + delay))
        end = max(start + 0.2, min(duration, timing.end + delay))
        cues.append(SubtitleCue(index=index, start=start, end=end, text=chunk))
    return cues


def make_cues_from_timing_text(
    timings: list[TimingSegment],
    duration: float,
    delay: float = 0.0,
    target_len: int = 12,
    max_len: int = 18,
) -> list[SubtitleCue]:
    timings = [item for item in timings if item.end > item.start and item.text.strip()]
    if not timings or duration <= 0:
        return []

    cues: list[SubtitleCue] = []
    for timing in timings:
        start = max(0.0, min(duration, timing.start + delay))
        end = max(start + 0.2, min(duration, timing.end + delay))
        chunks = split_script(timing.text, target_len=target_len, max_len=max_len) or [timing.text.strip()]
        cues.extend(_spread_chunks(chunks, start, end, start_index=len(cues) + 1))
    return cues


def make_cues_from_segmented_timings(
    timings: list[TimingSegment], duration: float, delay: float = 0.0
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for timing in timings:
        text = timing.text.strip()
        if not text or timing.end <= timing.start:
            continue
        start = max(0.0, min(duration, timing.start + delay))
        end = max(start + 0.05, min(duration, timing.end + delay))
        cues.append(SubtitleCue(index=len(cues) + 1, start=start, end=end, text=text))
    return cues


def _spread_chunks(chunks: list[str], start: float, end: float, start_index: int = 1) -> list[SubtitleCue]:
    duration = max(0.2, end - start)
    weights = [max(4, len(strip_keyword_marks(chunk))) for chunk in chunks]
    total_weight = sum(weights)
    cursor = start
    cues: list[SubtitleCue] = []
    for offset, (chunk, weight) in enumerate(zip(chunks, weights)):
        cue_end = min(end, cursor + duration * weight / total_weight)
        if offset == len(chunks) - 1:
            cue_end = end
        cues.append(SubtitleCue(index=start_index + offset, start=cursor, end=max(cursor, cue_end), text=chunk))
        cursor = cue_end
    return cues


def _make_cues_across_speech_window(
    chunks: list[str],
    timings: list[TimingSegment],
    duration: float,
    delay: float,
) -> list[SubtitleCue]:
    speech_start = max(0.0, min(duration, timings[0].start + delay))
    speech_end = max(speech_start + 0.2, min(duration, timings[-1].end + delay))
    speech_duration = max(0.2, speech_end - speech_start)
    weights = [max(4, len(strip_keyword_marks(chunk))) for chunk in chunks]
    total_weight = sum(weights)
    cursor = speech_start
    cues: list[SubtitleCue] = []
    for index, (chunk, weight) in enumerate(zip(chunks, weights), start=1):
        cue_duration = speech_duration * weight / total_weight
        end = min(speech_end, cursor + cue_duration)
        if index == len(chunks):
            end = speech_end
        cues.append(SubtitleCue(index=index, start=cursor, end=max(cursor, end), text=chunk))
        cursor = end
    return cues


def _fit_timing_count(timings: list[TimingSegment], target_count: int) -> list[TimingSegment]:
    if len(timings) == target_count:
        return timings
    if target_count <= 1:
        return [TimingSegment(timings[0].start, timings[-1].end, " ".join(item.text for item in timings))]

    result: list[TimingSegment] = []
    total_start = timings[0].start
    total_end = timings[-1].end
    total_duration = max(0.2, total_end - total_start)
    for index in range(target_count):
        if target_count > len(timings):
            start = total_start + total_duration * index / target_count
            end = total_start + total_duration * (index + 1) / target_count
            result.append(TimingSegment(start, end, ""))
            continue

        start_i = int(index * len(timings) / target_count)
        end_i = int((index + 1) * len(timings) / target_count)
        end_i = max(start_i + 1, end_i)
        group = timings[start_i:end_i]
        result.append(TimingSegment(group[0].start, group[-1].end, " ".join(item.text for item in group)))
    return result


def write_srt(cues: list[SubtitleCue], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{strip_keyword_marks(cue.text)}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def write_ass(
    cues: list[SubtitleCue],
    path: Path,
    width: int = 1080,
    height: int = 1920,
    style: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    style = style or DEFAULT_STYLE_PRESETS[0]["subtitle"]
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        subtitle_to_ass_style(style, width=width, height=height),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in cues:
        override = subtitle_override(style, cue.start, cue.end, width=width, height=height)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Default,,0,0,0,,{override}{_ass_text(cue.text)}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _ass_time(seconds: float) -> str:
    cs = round(seconds * 100)
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_text(text: str) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")
    text = MARK_RE.sub(lambda match: r"{\c&H00FFFF&}" + match.group(1) + r"{\c&H00FFFFFF&}", text)
    return text
