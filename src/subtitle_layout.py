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


def build_layout_capacity(style: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    """Return a conservative character-unit budget for semantic caption planning."""
    context = build_layout_context(style, width, height)
    fixed_effects = context.outline * 2 + context.effect_extent + context.background_padding * 2
    glyph_samples = "国赢黑霸器警MMWW88AI"
    glyph_width = max(
        1.0,
        max(measure_text(char, context) - fixed_effects for char in glyph_samples),
    )
    usable_comfort = max(glyph_width, context.comfort_width - fixed_effects)
    usable_hard = max(glyph_width, context.hard_width - fixed_effects)
    return {
        "recommended_max_units": max(1, int(usable_comfort // glyph_width)),
        "absolute_max_units": max(1, int(usable_hard // glyph_width)),
        "hard_width_px": round(context.hard_width, 1),
        "comfortable_width_px": round(context.comfort_width, 1),
        "reference_unit_px": round(glyph_width, 2),
        "width": int(width),
        "height": int(height),
        "font_family": str(style.get("font_family") or "Microsoft YaHei"),
        "font_size": int(context.font_size),
        "unit_rule": "中文=1，英文/数字按实际字体通常小于1；absolute_max_units为保险上限，最终仍以hard_width_px实测为准",
    }


def text_overflows(text: str, style: dict[str, Any], width: int, height: int) -> bool:
    context = build_layout_context(style, width, height)
    return measure_text(text, context) > context.hard_width


def wrap_title_text(
    text: str,
    style: dict[str, Any],
    width: int,
    height: int,
    analysis: dict[str, Any] | None = None,
) -> str:
    source_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    for line_index, source in enumerate(source_lines, start=1):
        source = source.strip()
        if not source:
            output.append("")
            continue
        tokens = tokens_from_text(source, 0.0, max(1.0, len(source) * 0.1), prefix=f"title-{line_index}")
        preferred: set[str] = set()
        forbidden: set[str] = set()
        if analysis:
            preferred, semantic_ends, forbidden = analysis_break_sets(tokens, analysis)
            preferred.update(semantic_ends)
        groups = segment_tokens(tokens, style, width, height, preferred, (), forbidden)
        output.extend("".join(str(token.get("text") or "") for token in group).strip() for group in groups)
    return "\n".join(line for line in output if line).strip()


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
    required_breaks: Iterable[str] = (),
    forbidden_breaks: Iterable[str] = (),
) -> list[list[dict[str, Any]]]:
    tokens = [dict(token) for token in tokens if str(token.get("text") or "")]
    if not tokens:
        return []
    context = build_layout_context(style, width, height)
    hints = set(break_hints)
    required_ids = set(required_breaks)
    forbidden_ids = set(forbidden_breaks)
    count = len(tokens)
    required_positions = {
        index + 1 for index, token in enumerate(tokens[:-1]) if str(token.get("id") or "") in required_ids
    }
    best = [math.inf] * (count + 1)
    previous = [-1] * (count + 1)
    best[0] = 0.0
    for start_index in range(count):
        if math.isinf(best[start_index]):
            continue
        text = ""
        next_required = min((position for position in required_positions if position > start_index), default=count)
        for end_index in range(start_index + 1, count + 1):
            if end_index > next_required:
                break
            text += str(tokens[end_index - 1].get("text") or "")
            measured = measure_text(text, context)
            boundary_id = str(tokens[end_index - 1].get("id") or "")
            boundary_allowed = end_index == count or end_index in required_positions or boundary_id not in forbidden_ids
            if not boundary_allowed:
                continue
            cost = _segment_cost(tokens, start_index, end_index, measured, context, hints, end_index == count)
            candidate = best[start_index] + cost
            if candidate < best[end_index]:
                best[end_index] = candidate
                previous[end_index] = start_index
            if measured > context.hard_width and end_index > start_index + 1:
                break
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


def analysis_break_sets(
    tokens: list[dict[str, Any]], analysis: dict[str, Any]
) -> tuple[set[str], set[str], set[str]]:
    token_ids = {str(token.get("id") or "") for token in tokens}
    token_by_id = {str(token.get("id") or ""): token for token in tokens}
    token_position = {str(token.get("id") or ""): index for index, token in enumerate(tokens)}
    preferred = {
        str(item.get("after_token_id") or "")
        for key in ("break_hints", "allowed_breaks")
        for item in analysis.get(key, [])
        if float(item.get("confidence", 0)) >= 0.6 and str(item.get("after_token_id") or "") in token_ids
    }
    forbidden: set[str] = set()
    for item in analysis.get("forbidden_breaks", []):
        if float(item.get("confidence", 0)) < 0.7:
            continue
        span_ids = [str(token_id) for token_id in item.get("token_ids", []) if str(token_id) in token_ids]
        if _valid_semantic_span(span_ids, str(item.get("text") or ""), token_by_id, token_position):
            forbidden.update(span_ids[:-1])
            continue
        after_id = str(item.get("after_token_id") or "")
        if after_id in token_ids:
            forbidden.add(after_id)
    required: set[str] = set()
    for sentence in analysis.get("final_sentences", []):
        ids = [str(item) for item in sentence.get("token_ids", []) if str(item) in token_ids]
        if _valid_semantic_span(ids, str(sentence.get("text") or ""), token_by_id, token_position):
            # Model-proposed sentence boundaries are strong hints, not immutable
            # walls. The layout planner may merge across a poor first-pass
            # boundary when the complete context yields a better subtitle.
            preferred.add(ids[-1])
    for token in tokens:
        token_id = str(token.get("id") or "")
        if token.get("manual_break_after") or token.get("script_sentence_break_after"):
            required.add(token_id)
        elif token.get("script_phrase_break_after"):
            preferred.add(token_id)
    if tokens:
        required.discard(str(tokens[-1].get("id") or ""))
    preferred.update(required)
    forbidden.difference_update(required)
    forbidden.update(_same_word_forbidden_breaks(tokens))
    for span in analysis.get("protected_spans", []):
        ids = [str(item) for item in span.get("token_ids", []) if str(item) in token_ids]
        if float(span.get("confidence", 0)) >= 0.7 and _valid_semantic_span(
            ids, str(span.get("text") or ""), token_by_id, token_position
        ):
            forbidden.update(ids[:-1])
    forbidden.difference_update(required)
    return preferred, required, forbidden


def _valid_semantic_span(
    ids: list[str], text: str, token_by_id: dict[str, dict[str, Any]], token_position: dict[str, int]
) -> bool:
    if not ids or any(item not in token_position for item in ids):
        return False
    positions = [token_position[item] for item in ids]
    if positions != list(range(positions[0], positions[0] + len(positions))):
        return False
    joined = "".join(str(token_by_id[item].get("text") or "") for item in ids)
    normalize = lambda value: re.sub(r"[\s，。！？!?；;：:,]", "", value).casefold()
    return bool(normalize(text)) and normalize(joined) == normalize(text)


def _same_word_forbidden_breaks(tokens: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for left, right in zip(tokens, tokens[1:]):
        left_id = str(left.get("id") or "")
        right_id = str(right.get("id") or "")
        left_word = re.sub(r"-c\d+$", "", left_id)
        right_word = re.sub(r"-c\d+$", "", right_id)
        if left_word and left_word == right_word:
            result.add(left_id)
    return result


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
