from __future__ import annotations

import json
import http.client
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from safe_json import loads_json


SYSTEM_PROMPT = """你是一名专业中文短视频口播剪辑导演。你的目标是直接生成紧凑、自然、信息密度高的自动剪辑决策，不要把决定交回用户确认。
只使用输入中的 token ID，不编造文本、音频或时间。区分：ASR 识别错字只改字幕；说话者卡壳、结巴、半句重说、重复表达和无意义填充词应删除对应视频。
优先保留表达完整、自然、信息更多的一遍。不得删除关键数字、品牌名、产品名、行动指令。删除后必须保持语法和语义连续。
返回严格 JSON 对象：
corrections: [{token_ids:[...],replacement:string,confidence:0-1,reason:string}]
break_hints: [{after_token_id:string,confidence:0-1,reason:string}]
allowed_breaks: [{after_token_id:string,confidence:0-1,reason:string}]。标注长句内部所有自然短语边界，供字幕宽度不足时选择。
forbidden_breaks: [{token_ids:[...],text:string,confidence:0-1,reason:string}]。每个不能拆开的词组、否定短语、代词、数字单位、品牌名和专有名词必须返回完整且连续的 token IDs；禁止只返回一个边界 ID；text 必须与 token 拼接结果一致。
delete_ranges: [{token_ids:[...],type:"stutter|false_start|exact_repeat|semantic_repeat|filler|redundant",confidence:0-1,reason:string}]
repeat_candidates: 与 delete_ranges 中重复相关项目兼容的候选列表。
final_sentences: [{token_ids:[...],text:string}]
示例：原文“我们在广……我们在广西做获客系统”，删除前一段，type=false_start，保留后一段。
示例：原文“每天每天几十个精准进线”，删除第一个“每天”，type=stutter。"""

LAYOUT_PROMPT = """你是中文短视频字幕排版导演。系统已经生成全部合法候选句，你只负责选择最终组合，不要自己创建句子，不要解释。
返回严格 JSON：{"option_ids":["o000-006","o006-012"]}。
规则：
1. 只能从 constraints.line_options 选择 option_id，禁止返回自创文本或 token_ids。
2. 第一项必须从第一个 token 开始；后一项 start_token_id 必须紧接前一项 end_token_id；最后一项必须覆盖最后一个 token。
3. 不得漏选、重复、交叉或换序。所有候选已经通过最大宽度、完整语义句和禁断词组校验。
4. 优先选择 natural=true 且宽度接近 comfortable_width_px 的候选，同时避免只有一两个字的孤行。
5. 断句应口语自然、语义完整，不能把否定词、代词、数字单位、英文单词和固定搭配拆散。"""


COMPACT_LAYOUT_PROMPT = """
你是中文短视频字幕排版导演。系统已生成所有合法候选句，你只选择最终组合。
返回严格 JSON：{"option_ids":["o000-006","o006-012"]}。
规则：
1. 只能选择 constraints.line_options 中的 id。
2. start/end 是左闭右开 token 索引；第一项 start=0，相邻项必须无缝衔接，最后一项覆盖全部 token。
3. 不得遗漏、重复、交叉或换序。
4. fill 表示相对舒适宽度；优先 natural=true、fill 接近 1 的句子，同时避免孤立一两个字。
5. tokens 中的 text 只用于语义判断，不要返回文本。
"""


def analyze_transcript(
    tokens: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    cache_dir: str | Path | None = None,
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
    analyses: list[dict[str, Any]] = []
    errors: list[str] = []
    for chunk in _analysis_chunks(compact_tokens):
        cache_key = _cache_key("analysis", model, {"tokens": chunk})
        result = _read_cache(cache_dir, cache_key)
        if result is None:
            result = _request_analysis(chunk, endpoint=endpoint, model=model, api_key=api_key, timeout=timeout)
            if result.get("status") == "ok":
                _write_cache(cache_dir, cache_key, result)
        else:
            result = {**result, "cached": True, "usage": _empty_usage()}
        if result.get("status") == "ok":
            analyses.append(result)
        else:
            errors.append(str(result.get("reason") or "invalid_response"))
    if not analyses:
        return _empty_analysis(errors[0] if errors else "invalid_response")
    merged = {key: [] for key in _analysis_list_keys()}
    for result in analyses:
        for key in merged:
            merged[key].extend(result.get(key, []))
    return {
        "status": "ok",
        **merged,
        "warnings": errors,
        "usage": _sum_usage(analyses),
        "cache_hits": sum(1 for result in analyses if result.get("cached")),
    }


def decide_line_layout(
    tokens: list[dict[str, Any]],
    constraints: dict[str, Any],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    previous: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    if not base_url or not model or not api_key or not tokens:
        return {"status": "skipped", "reason": "not_configured", "sentences": []}
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    user_payload: dict[str, Any] = {"tokens": tokens, "constraints": constraints}
    if previous is not None:
        user_payload["previous_output"] = previous
        user_payload["validation_errors"] = validation_errors or []
        user_payload["instruction"] = "上一次结果未通过系统校验，请逐项修正后返回完整 JSON。"
    cache_key = _cache_key("layout", model, user_payload)
    cached = _read_cache(cache_dir, cache_key)
    if cached is not None:
        return {**cached, "cached": True, "usage": _empty_usage()}
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": COMPACT_LAYOUT_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    }
    last_error = ""
    for _ in range(1):
        try:
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            parsed = _parse_json_content(body["choices"][0]["message"]["content"])
            usage = _normalize_usage(body.get("usage"))
            option_ids = parsed.get("option_ids")
            if isinstance(option_ids, list):
                result = {"status": "ok", "option_ids": [str(option_id) for option_id in option_ids if str(option_id)], "usage": usage}
                _write_cache(cache_dir, cache_key, result)
                return result
            sentences = _clean_final_sentences(parsed.get("sentences"))
            result = {"status": "ok", "sentences": sentences, "usage": usage}
            _write_cache(cache_dir, cache_key, result)
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = exc.__class__.__name__
    return {"status": "failed", "reason": last_error or "invalid_response", "sentences": []}


def _analysis_chunks(tokens: list[dict[str, Any]], limit: int = 100) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    start = 0
    while start < len(tokens):
        hard_end = min(len(tokens), start + limit)
        end = hard_end
        if hard_end < len(tokens):
            search_start = min(hard_end - 1, start + max(1, limit // 2))
            candidates = [index for index in range(search_start, hard_end) if float(tokens[index].get("pause_after", 0)) >= 0.25]
            if candidates:
                end = candidates[-1] + 1
        chunks.append(tokens[start:end])
        start = end
    return chunks


def _request_analysis(
    tokens: list[dict[str, Any]], *, endpoint: str, model: str, api_key: str, timeout: float
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"tokens": tokens}, ensure_ascii=False)},
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
            result = _sanitize_analysis(_parse_json_content(body["choices"][0]["message"]["content"]))
            result["usage"] = _normalize_usage(body.get("usage"))
            return result
        except (
            KeyError, IndexError, TypeError, ValueError, RuntimeError, json.JSONDecodeError,
            urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError,
        ) as exc:
            last_error = exc.__class__.__name__
    return _empty_analysis(last_error or "invalid_response")


PROMPT_VERSION = "2026-07-13-compact-layout-v1"


def _cache_key(kind: str, model: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        {"version": PROMPT_VERSION, "kind": kind, "model": model, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _read_cache(cache_dir: str | Path | None, key: str) -> dict[str, Any] | None:
    if not cache_dir:
        return None
    path = Path(cache_dir) / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "ok" else None


def _write_cache(cache_dir: str | Path | None, key: str, value: dict[str, Any]) -> None:
    if not cache_dir:
        return
    path = Path(cache_dir) / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def _empty_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _normalize_usage(value: Any) -> dict[str, int]:
    data = value if isinstance(value, dict) else {}
    return {
        "prompt_tokens": max(0, int(data.get("prompt_tokens") or 0)),
        "completion_tokens": max(0, int(data.get("completion_tokens") or 0)),
        "total_tokens": max(0, int(data.get("total_tokens") or 0)),
    }


def _sum_usage(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(int(result.get("usage", {}).get(key, 0)) for result in results)
        for key in _empty_usage()
    }


def _analysis_list_keys() -> tuple[str, ...]:
    return (
        "corrections", "break_hints", "allowed_breaks", "forbidden_breaks", "protected_spans",
        "repeat_candidates", "delete_ranges", "final_sentences",
    )


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
    parsed = loads_json(match.group(0), source="LLM analysis response")
    if not isinstance(parsed, dict):
        raise ValueError("LLM analysis response must be a JSON object")
    return parsed


def _sanitize_analysis(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "corrections": _clean_operations(data.get("corrections"), require_replacement=True),
        "break_hints": _clean_break_hints(data.get("break_hints")),
        "allowed_breaks": _clean_break_hints(data.get("allowed_breaks")),
        "forbidden_breaks": _clean_forbidden_breaks(data.get("forbidden_breaks")),
        "protected_spans": _clean_protected_spans(data.get("protected_spans")),
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


def _clean_protected_spans(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("token_ids"), list):
            continue
        token_ids = [str(token_id) for token_id in item["token_ids"] if str(token_id)]
        text = str(item.get("text") or "")[:300]
        if len(token_ids) < 2 or not text:
            continue
        result.append(
            {
                "token_ids": token_ids,
                "text": text,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0)))),
                "reason": str(item.get("reason") or "")[:300],
            }
        )
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


def _clean_forbidden_breaks(value: Any) -> list[dict[str, Any]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        reason = str(item.get("reason") or "")[:300]
        token_ids = [str(token_id) for token_id in item.get("token_ids", []) if str(token_id)]
        text = str(item.get("text") or "")[:300]
        if len(token_ids) >= 2 and text:
            result.append({"token_ids": token_ids, "text": text, "confidence": confidence, "reason": reason})
        elif item.get("after_token_id"):
            result.append(
                {
                    "after_token_id": str(item["after_token_id"]),
                    "confidence": confidence,
                    "reason": reason,
                }
            )
    return result


def _empty_analysis(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason, "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": [], "delete_ranges": [], "final_sentences": []}
