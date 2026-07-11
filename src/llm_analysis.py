from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


SYSTEM_PROMPT = """你是中文口播字幕校对器。只依据给定文本工作，不编造音频和时间。
返回严格 JSON 对象，字段为 corrections、break_hints、repeat_candidates。
corrections: [{token_ids:[...],replacement:string,confidence:0-1,reason:string}]
break_hints: [{after_token_id:string,confidence:0-1,reason:string}]
repeat_candidates: [{token_ids:[...],confidence:0-1,reason:string}]
只修正明显错别字、专有词和识别错误，不润色措辞。重复和口误仅标记，不删除。"""


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
    compact_tokens = [{"id": token.get("id"), "text": token.get("text")} for token in tokens]
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
    }


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
    return {"status": "skipped", "reason": reason, "corrections": [], "break_hints": [], "repeat_candidates": []}
