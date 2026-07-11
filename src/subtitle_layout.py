from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class LayoutContext:
    width: int
    height: int
    available_width: float
    comfort_width: float
    hard_width: float
    font_size: int
    font: Any
    letter_spacing: float
    outline: float
    effect_extent: float
    background_padding: float


def build_layout_context(style: dict[str, Any], width: int, height: int) -> LayoutContext:
    scale = max(0.2, float(style.get("scale", 100)) / 100)
    font_size = max(10, round(float(style.get("font_size", 64)) * height / 1920 * scale))
    font = _find_font(font_size, str(style.get("font_family", "")), bool(style.get("bold", True)))
    margin = max(0.0, float(style.get("margin_x", 80)) * width / 1080)
    center = width / 2 + float(style.get("position_x", 0)) * width / 1080
    available = max(80.0, 2 * min(max(0.0, center - margin), max(0.0, width - margin - center)))
    letter_spacing = float(style.get("letter_spacing", 0)) * width / 1080
    outline = float(style.get("outline_width", 0) if style.get("outline_enabled", True) else 0) * height / 1920
    shadow = float(style.get("shadow_offset", 0) if style.get("shadow_enabled", False) else 0) * height / 1920
    glow = float(style.get("glow_strength", 0) if style.get("glow_enabled", False) else 0) * height / 1920
    padding = float(style.get("background_padding", 0) if style.get("background_enabled", False) else 0) * height / 1920
    return LayoutContext(
        width=width,
        height=height,
        available_width=available,
        comfort_width=available * 0.80,
        hard_width=available * 0.95,
        font_size=font_size,
        font=font,
        letter_spacing=letter_spacing,
        outline=outline,
        effect_extent=max(shadow, glow),
        background_padding=padding,
    )


def measure_text(text: str, context: LayoutContext) -> float:
    text = str(text or "")
    if not text:
        return 0.0
    try:
        width = float(context.font.getlength(text))
    except AttributeError:
        box = context.font.getbbox(text)
        width = float(box[2] - box[0])
    spacing = max(0, len(text) - 1) * context.letter_spacing
    return width + spacing + context.outline * 2 + context.effect_extent + context.background_padding * 2


def text_overflows(text: str, style: dict[str, Any], width: int, height: int) -> bool:
    context = build_layout_context(style, width, height)
    return measure_text(text, context) > context.hard_width


def tokens_from_text(
    text: str,
    start: float,
    end: float,
    *,
    prefix: str,
    timing_source: str = "estimated",
) -> list[dict[str, Any]]:
    pieces = _text_pieces(text)
    if not pieces:
        return []
    weights = [max(0.5, _visual_weight(piece)) for piece in pieces]
    total = sum(weights)
    duration = max(0.001, end - start)
    cursor = start
    result: list[dict[str, Any]] = []
    for index, (piece, weight) in enumerate(zip(pieces, weights), start=1):
        token_end = end if index == len(pieces) else cursor + duration * weight / total
        result.append(
            {
                "id": f"{prefix}-w{index:04d}",
                "text": piece,
                "original_text": piece,
                "start": round(cursor, 3),
                "end": round(max(cursor, token_end), 3),
                "timing_source": timing_source,
                "edited": False,
            }
        )
        cursor = token_end
    return result


def normalize_word_tokens(item: dict[str, Any], utterance_index: int) -> list[dict[str, Any]]:
    utterance_start = _milliseconds(item.get("start_time", item.get("start", 0)))
    utterance_end = _milliseconds(item.get("end_time", item.get("end", utterance_start * 1000)))
    words = item.get("words") if isinstance(item.get("words"), list) else []
    normalized: list[dict[str, Any]] = []
    for word_index, word in enumerate(words, start=1):
        if not isinstance(word, dict):
            continue
        text = str(word.get("text") or word.get("word") or "").strip()
        if not text:
            continue
        start = _milliseconds(word.get("start_time", word.get("start", utterance_start * 1000)))
        end = _milliseconds(word.get("end_time", word.get("end", start * 1000)))
        if end <= start:
            end = max(start + 0.02, utterance_end)
        chars = _text_pieces(text)
        if len(chars) <= 1:
            normalized.append(_token(f"u{utterance_index:04d}-w{word_index:04d}", text, start, end, "asr"))
            continue
        char_duration = (end - start) / len(chars)
        for char_index, char in enumerate(chars, start=1):
            char_start = start + (char_index - 1) * char_duration
            char_end = end if char_index == len(chars) else start + char_index * char_duration
            normalized.append(
                _token(
                    f"u{utterance_index:04d}-w{word_index:04d}-c{char_index:02d}",
                    char,
                    char_start,
                    char_end,
                    "interpolated-within-word",
                )
            )
    if normalized:
        return normalized
    return tokens_from_text(
        str(item.get("text") or ""),
        utterance_start,
        utterance_end,
        prefix=f"u{utterance_index:04d}",
    )


def segment_tokens(
    tokens: list[dict[str, Any]],
    style: dict[str, Any],
    width: int,
    height: int,
    break_hints: Iterable[str] = (),
) -> list[list[dict[str, Any]]]:
    tokens = [dict(token) for token in tokens if str(token.get("text") or "")]
    if not tokens:
        return []
    context = build_layout_context(style, width, height)
    hints = set(break_hints)
    count = len(tokens)
    best = [math.inf] * (count + 1)
    previous = [-1] * (count + 1)
    best[0] = 0.0
    for start_index in range(count):
        if math.isinf(best[start_index]):
            continue
        text = ""
        for end_index in range(start_index + 1, count + 1):
            text += str(tokens[end_index - 1].get("text") or "")
            measured = measure_text(text, context)
            if measured > context.hard_width and end_index > start_index + 1:
                break
            cost = _segment_cost(tokens, start_index, end_index, measured, context, hints, end_index == count)
            candidate = best[start_index] + cost
            if candidate < best[end_index]:
                best[end_index] = candidate
                previous[end_index] = start_index
    if previous[count] < 0:
        return [[token] for token in tokens]
    ranges: list[tuple[int, int]] = []
    cursor = count
    while cursor > 0:
        start_index = previous[cursor]
        if start_index < 0:
            start_index = cursor - 1
        ranges.append((start_index, cursor))
        cursor = start_index
    ranges.reverse()
    return [tokens[start:end] for start, end in ranges]


def flatten_segment_tokens(segments: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for segment in segments:
        for token in getattr(segment, "tokens", ()) or ():
            result.append(dict(token))
    return result


def _segment_cost(
    tokens: list[dict[str, Any]],
    start: int,
    end: int,
    measured: float,
    context: LayoutContext,
    hints: set[str],
    final: bool,
) -> float:
    ratio = measured / max(1.0, context.comfort_width)
    cost = (ratio - 0.82) ** 2 * 8
    length = end - start
    if length <= 2 and not final:
        cost += 5.0
    if measured > context.hard_width:
        cost += 1000.0
    if end < len(tokens):
        left = tokens[end - 1]
        right = tokens[end]
        pause = max(0.0, float(right.get("start", 0)) - float(left.get("end", 0)))
        if pause >= 0.25:
            cost -= min(5.0, pause * 8)
        elif pause < 0.18:
            cost += 1.2
        if re.search(r"[。！？!?；;，,：:]$", str(left.get("text") or "")):
            cost -= 3.0
        if str(left.get("id") or "") in hints:
            cost -= 4.0
        if _latin_continuation(left, right):
            cost += 1000.0
    return cost


def _latin_continuation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(re.match(r"[A-Za-z0-9]$", str(left.get("text") or ""))) and bool(
        re.match(r"^[A-Za-z0-9]", str(right.get("text") or ""))
    )


def _text_pieces(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_+\-./#]+|\s+|.", str(text or ""))


def _visual_weight(text: str) -> float:
    return sum(0.5 if char.isascii() else 1.0 for char in text if not char.isspace()) or 0.5


def _milliseconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number / 1000 if number > 100 else number


def _token(token_id: str, text: str, start: float, end: float, source: str) -> dict[str, Any]:
    return {
        "id": token_id,
        "text": text,
        "original_text": text,
        "start": round(start, 3),
        "end": round(end, 3),
        "timing_source": source,
        "edited": False,
    }


def _find_font(size: int, family: str, bold: bool):
    from PIL import ImageFont

    family_lower = family.lower()
    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf") if "hei" in family_lower else Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simsun.ttc") if "song" in family_lower else Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf"),
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default(size=size)
