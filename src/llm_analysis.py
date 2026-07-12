from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


SYSTEM_PROMPT = """你是一名专业中文短视频口播剪辑导演。你的目标是直接生成紧凑、自然、信息密度高的自动剪辑决策，不要把决定交回用户确认。
只使用输入中的 token ID，不编造文本、音频或时间。区分：ASR 识别错字只改字幕；说话者卡壳、结巴、半句重说、重复表达和无意义填充词应删除对应视频。
优先保留表达完整、自然、信息更多的一遍。不得删除关键数字、品牌名、产品名、行动指令。删除后必须保持语法和语义连续。
返回严格 JSON 对象：
corrections: [{token_ids:[...],replacement:string,confidence:0-1,reason:string}]
break_hints: [{after_token_id:string,confidence:0-1,reason:string}]
delete_ranges: [{token_ids:[...],type:"stutter|false_start|exact_repeat|semantic_repeat|filler|redundant",confidence:0-1,reason:string}]
repeat_candidates: 与 delete_ranges 中重复相关项目兼容的候选列表。
final_sentences: [{token_ids:[...],text:string}]
示例：原文“我们在广……我们在广西做获客系统”，删除前一段，type=false_start，保留后一段。
示例：原文“每天每天几十个精准进线”，删除第一个“每天”，type=stutter。"""


def analyze_transcript(
    tokens: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not base_url or not model or not api_key or not tokens:
        return _empty_analysis("not_configured")
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    compact_tokens = []
    for index, token in enumerate(tokens):
        next_start = float(tokens[index + 1].get("start", token.get("end", 0))) if index + 1 < len(tokens) else float(token.get("end", 0))
        compact_tokens.append(
            {
                "id": token.get("id"),
                "text": token.get("text"),
                "start": round(float(token.get("start", 0)), 3),
                "end": round(float(token.get("end", 0)), 3),
                "pause_after": round(max(0.0, next_start - float(token.get("end", 0))), 3),
                "timing_source": token.get("timing_source", "unknown"),
            }
        )
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"tokens": compact_tokens}, ensure_ascii=False)},
        ],
    }
    last_error = ""
    for _ in range(2):
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            parsed = _parse_json_content(content)
            return _sanitize_analysis(parsed)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_error = exc.__class__.__name__
    return _empty_analysis(last_error or "invalid_response")


def apply_high_confidence_corrections(
    tokens: list[dict[str, Any]], analysis: dict[str, Any], threshold: float = 0.92
) -> list[dict[str, Any]]:
    token_by_id = {str(token.get("id")): token for token in tokens}
    for correction in analysis.get("corrections", []):
        if float(correction.get("confidence", 0)) < threshold:
            continue
        ids = [str(item) for item in correction.get("token_ids", []) if str(item) in token_by_id]
        replacement = str(correction.get("replacement") or "")
        if not ids or not replacement:
            continue
        first = token_by_id[ids[0]]
        first["original_text"] = str(first.get("original_text") or first.get("text") or "")
        first["text"] = replacement
        first["edited"] = True
        first["correction_reason"] = str(correction.get("reason") or "")
        first["correction_confidence"] = float(correction.get("confidence", 0))
        for token_id in ids[1:]:
            token_by_id[token_id]["text"] = ""
            token_by_id[token_id]["edited"] = True
    return tokens


def _parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("missing JSON object")
    return json.loads(match.group(0))


def _sanitize_analysis(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "corrections": _clean_operations(data.get("corrections"), require_replacement=True),
        "break_hints": _clean_break_hints(data.get("break_hints")),
        "repeat_candidates": _clean_operations(data.get("repeat_candidates"), require_replacement=False),
        "delete_ranges": _clean_delete_ranges(data.get("delete_ranges")),
        "final_sentences": _clean_final_sentences(data.get("final_sentences")),
    }


def _clean_delete_ranges(value: Any) -> list[dict[str, Any]]:
    allowed = {"stutter", "false_start", "exact_repeat", "semantic_repeat", "filler", "redundant"}
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        if not token_ids:
            continue
        kind = str(item.get("type") or "redundant")
        result.append(
            {
                "token_ids": token_ids,
                "type": kind if kind in allowed else "redundant",
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
    return result


def _clean_final_sentences(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        text = str(item.get("text") or "")[:1000]
        if token_ids and text:
            result.append({"token_ids": token_ids, "text": text})
    return result


def _clean_operations(value: Any, require_replacement: bool) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        cleaned = {
            "token_ids": [str(token_id) for token_id in item["token_ids"]],
            "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
            "reason": str(item.get("reason") or "")[:300],
        }
        if require_replacement:
            cleaned["replacement"] = str(item.get("replacement") or "")[:300]
            if not cleaned["replacement"]:
                continue
        result.append(cleaned)
    return result


def _clean_break_hints(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not item.get("after_token_id"):
            continue
        result.append(
            {
                "after_token_id": str(item["after_token_id"]),
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
    return result


def _empty_analysis(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "corrections": [], "break_hints": [], "repeat_candidates": [], "delete_ranges": [], "final_sentences": []}
