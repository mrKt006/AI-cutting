from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


LEXICAL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.#-]*|[\u3400-\u9fff]|[^\s]")
STRONG_PUNCTUATION = set("。！？!?；;")
WEAK_PUNCTUATION = set("，,、：:")


def align_transcript_tokens(
    asr_tokens: list[dict[str, Any]], transcript: str, *, minimum_ratio: float = 0.55
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    copied = [dict(token) for token in asr_tokens]
    script_units, manual_breaks, punctuation = _script_units(transcript)
    asr_units = [str(token.get("text") or "") for token in copied]
    if not copied or not script_units:
        return copied, _empty_report("missing_tokens_or_transcript")

    matcher = SequenceMatcher(
        None,
        [_comparison_text(text) for text in asr_units],
        [_comparison_text(text) for text in script_units],
        autojunk=False,
    )
    ratio = matcher.ratio()
    if ratio < minimum_ratio:
        report = _empty_report("low_coverage")
        report.update(
            {
                "status": "rejected",
                "similarity": round(ratio, 4),
                "asr_token_count": len(asr_units),
                "script_unit_count": len(script_units),
            }
        )
        return copied, report

    script_to_asr: dict[int, int] = {}
    corrections: list[dict[str, Any]] = []
    asr_only: list[dict[str, Any]] = []
    script_only: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    matched = 0

    for tag, a0, a1, s0, s1 in matcher.get_opcodes():
        if tag == "equal":
            for asr_index, script_index in zip(range(a0, a1), range(s0, s1)):
                script_to_asr[script_index] = asr_index
                matched += 1
                if copied[asr_index].get("text") != script_units[script_index]:
                    _apply_text_correction(
                        copied,
                        asr_index,
                        script_units[script_index],
                        corrections,
                        confidence=0.99,
                        reason="逐字稿大小写或书写形式",
                    )
            continue
        if tag == "replace" and a1 - a0 == s1 - s0 and 0 < a1 - a0 <= 4:
            for asr_index, script_index in zip(range(a0, a1), range(s0, s1)):
                script_to_asr[script_index] = asr_index
                _apply_text_correction(
                    copied,
                    asr_index,
                    script_units[script_index],
                    corrections,
                    confidence=0.96,
                    reason="逐字稿与 ASR 小范围等长对齐",
                )
            continue
        if tag in {"delete", "replace"} and a1 > a0:
            asr_only.append(
                {
                    "token_ids": [str(copied[index].get("id") or "") for index in range(a0, a1)],
                    "text": "".join(asr_units[a0:a1]),
                    "reason": "稿外口播" if tag == "delete" else "无法安全自动替换",
                }
            )
        if tag in {"insert", "replace"} and s1 > s0:
            script_only.append(
                {
                    "script_start": s0,
                    "script_end": s1,
                    "text": "".join(script_units[s0:s1]),
                    "reason": "稿件内容未在音频中可靠对齐" if tag == "insert" else "无法安全自动替换",
                }
            )
        if tag == "replace":
            ambiguous.append(
                {
                    "asr_text": "".join(asr_units[a0:a1]),
                    "script_text": "".join(script_units[s0:s1]),
                    "asr_token_ids": [str(copied[index].get("id") or "") for index in range(a0, a1)],
                }
            )

    applied_breaks: list[str] = []
    for script_index in manual_breaks:
        asr_index = script_to_asr.get(script_index)
        if asr_index is None:
            continue
        copied[asr_index]["manual_break_after"] = True
        applied_breaks.append(str(copied[asr_index].get("id") or ""))
    for script_index, mark in punctuation.items():
        asr_index = script_to_asr.get(script_index)
        if asr_index is None:
            continue
        copied[asr_index]["script_punctuation_after"] = mark
        if mark in STRONG_PUNCTUATION:
            copied[asr_index]["script_sentence_break_after"] = True
        elif mark in WEAK_PUNCTUATION:
            copied[asr_index]["script_phrase_break_after"] = True

    return copied, {
        "version": 1,
        "status": "aligned",
        "similarity": round(ratio, 4),
        "asr_token_count": len(asr_units),
        "script_unit_count": len(script_units),
        "exact_match_count": matched,
        "corrections": corrections,
        "asr_only": asr_only,
        "script_only": script_only,
        "ambiguous": ambiguous,
        "manual_break_token_ids": applied_breaks,
    }


def _script_units(transcript: str) -> tuple[list[str], set[int], dict[int, str]]:
    units: list[str] = []
    manual_breaks: set[int] = set()
    punctuation: dict[int, str] = {}
    for match in LEXICAL_PATTERN.finditer(str(transcript or "")):
        text = match.group(0)
        if text == "|":
            if units:
                manual_breaks.add(len(units) - 1)
            continue
        if text in STRONG_PUNCTUATION or text in WEAK_PUNCTUATION:
            if units:
                punctuation[len(units) - 1] = text
            continue
        if _is_ignored_punctuation(text):
            continue
        units.append(text)
    return units, manual_breaks, punctuation


def _comparison_text(value: str) -> str:
    return str(value or "").casefold()


def _is_ignored_punctuation(value: str) -> bool:
    return bool(re.fullmatch(r"[^A-Za-z0-9\u3400-\u9fff]+", value))


def _apply_text_correction(
    tokens: list[dict[str, Any]],
    index: int,
    replacement: str,
    corrections: list[dict[str, Any]],
    *,
    confidence: float,
    reason: str,
) -> None:
    token = tokens[index]
    original = str(token.get("text") or "")
    if original == replacement:
        return
    token.setdefault("original_text", original)
    token["text"] = replacement
    token["edited"] = True
    token["correction_source"] = "transcript"
    token["correction_confidence"] = confidence
    corrections.append(
        {
            "token_ids": [str(token.get("id") or "")],
            "original": original,
            "replacement": replacement,
            "confidence": confidence,
            "reason": reason,
        }
    )


def _empty_report(reason: str) -> dict[str, Any]:
    return {
        "version": 1,
        "status": "skipped",
        "reason": reason,
        "similarity": 0.0,
        "corrections": [],
        "asr_only": [],
        "script_only": [],
        "ambiguous": [],
        "manual_break_token_ids": [],
    }
