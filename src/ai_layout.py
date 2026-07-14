from __future__ import annotations

from pathlib import Path
from typing import Any

from llm_analysis import decide_line_layout
from subtitle_layout import analysis_break_sets, build_layout_context, measure_text, segment_tokens


def layout_tokens_with_ai(
    tokens: list[dict[str, Any]],
    style: dict[str, Any],
    width: int,
    height: int,
    analysis: dict[str, Any],
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
    cache_dir: str | Path | None = None,
    max_calls: int = 48,
    _retry_depth: int = 0,
    _budget: dict[str, int] | None = None,
) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    preferred, required, forbidden = analysis_break_sets(tokens, analysis)
    fallback = lambda rows: segment_tokens(rows, style, width, height, preferred, required, forbidden)
    if not base_url or not model or not api_key or not tokens:
        return fallback(tokens), {"status": "fallback", "reason": "not_configured", "chunks": []}

    context = build_layout_context(style, width, height)
    budget = _budget if _budget is not None else {"calls": 0, "max_calls": max(1, int(max_calls))}
    token_by_id = {str(token.get("id") or ""): token for token in tokens}
    groups: list[list[dict[str, Any]]] = []
    audits: list[dict[str, Any]] = []
    for chunk in _layout_chunks(tokens, preferred, required):
        payload_tokens = _layout_payload_tokens(chunk, context)
        line_options = _line_options(chunk, context, preferred, required, forbidden)
        constraints = {
            "token_count": len(chunk),
            "line_options": [
                {
                    "id": option["option_id"],
                    "start": option["start_index"],
                    "end": option["end_index"],
                    "fill": option["fill"],
                    "natural": option["natural"],
                }
                for option in line_options
            ],
        }
        if budget["calls"] >= budget["max_calls"]:
            local_groups = fallback(chunk)
            groups.extend(local_groups)
            audits.append({"status": "fallback", "reason": "call_budget_exhausted", "errors": [], "sentences": []})
            continue
        budget["calls"] += 1
        response = decide_line_layout(
            payload_tokens,
            constraints,
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            cache_dir=cache_dir,
        )
        decision_traces = [response.get("decision_trace")] if response.get("decision_trace") else []
        response = _materialize_options(response, line_options)
        valid, errors, selected = _validate_layout(response, chunk, context, required, forbidden)
        if not valid and budget["calls"] < budget["max_calls"]:
            budget["calls"] += 1
            response = decide_line_layout(
                payload_tokens,
                constraints,
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=timeout,
                previous=response,
                validation_errors=errors,
                cache_dir=cache_dir,
            )
            if response.get("decision_trace"):
                decision_traces.append(response["decision_trace"])
            response = _materialize_options(response, line_options)
            valid, errors, selected = _validate_layout(response, chunk, context, required, forbidden)
        if valid:
            groups.extend([[token_by_id[token_id] for token_id in ids] for ids in selected])
            audits.append(
                {
                    "status": "ai",
                    "sentences": response.get("sentences", []),
                    "errors": [],
                    "cached": bool(response.get("cached")),
                    "usage": response.get("usage", {}),
                    "decision_traces": decision_traces,
                }
            )
        else:
            split_chunks = _split_failed_chunk(chunk, preferred, required, forbidden) if _retry_depth < 2 else []
            if split_chunks:
                for subchunk in split_chunks:
                    subgroups, subaudit = layout_tokens_with_ai(
                        subchunk,
                        style,
                        width,
                        height,
                        analysis,
                        base_url=base_url,
                        model=model,
                        api_key=api_key,
                        timeout=timeout,
                        cache_dir=cache_dir,
                        max_calls=max_calls,
                        _retry_depth=_retry_depth + 1,
                        _budget=budget,
                    )
                    groups.extend(subgroups)
                    audits.extend(subaudit.get("chunks", []))
                continue
            local_groups = fallback(chunk)
            groups.extend(local_groups)
            audits.append(
                {
                    "status": "fallback",
                    "reason": response.get("reason") or "validation_failed",
                    "errors": errors,
                    "sentences": [
                        {"token_ids": [str(token.get("id") or "") for token in group], "text": _token_text(group)}
                        for group in local_groups
                    ],
                    "decision_traces": decision_traces,
                }
            )
    status = "ai" if audits and all(item["status"] == "ai" for item in audits) else "mixed"
    usage = {
        key: sum(int(item.get("usage", {}).get(key, 0)) for item in audits)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return groups, {
        "status": status,
        "chunks": audits,
        "usage": usage,
        "api_calls": budget["calls"],
        "cache_hits": sum(1 for item in audits if item.get("cached")),
    }


def _split_failed_chunk(
    tokens: list[dict[str, Any]], preferred: set[str], required: set[str], forbidden: set[str]
) -> list[list[dict[str, Any]]]:
    if len(tokens) < 12:
        return []
    midpoint = len(tokens) // 2
    candidates = [
        index + 1
        for index, token in enumerate(tokens[:-1])
        if str(token.get("id") or "") in required and str(token.get("id") or "") not in forbidden
    ]
    if not candidates:
        candidates = [
            index + 1
            for index, token in enumerate(tokens[:-1])
            if str(token.get("id") or "") in preferred and str(token.get("id") or "") not in forbidden
        ]
    split_at = min(candidates, key=lambda value: abs(value - midpoint)) if candidates else midpoint
    if split_at <= 0 or split_at >= len(tokens):
        return []
    return [tokens[:split_at], tokens[split_at:]]


def _layout_chunks(
    tokens: list[dict[str, Any]], preferred: set[str], required: set[str], limit: int = 45
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    start = 0
    while start < len(tokens):
        hard_end = min(len(tokens), start + limit)
        if hard_end == len(tokens):
            end = hard_end
        else:
            candidates = [
                index + 1
                for index in range(start + max(1, limit // 2), hard_end)
                if str(tokens[index].get("id") or "") in required
            ]
            if not candidates:
                candidates = [
                    index + 1
                    for index in range(start + max(1, limit // 2), hard_end)
                    if str(tokens[index].get("id") or "") in preferred
                    or float(tokens[index + 1].get("start", 0)) - float(tokens[index].get("end", 0)) >= 0.25
                ]
            end = candidates[-1] if candidates else hard_end
        chunks.append(tokens[start:end])
        start = end
    return chunks


def _layout_payload_tokens(tokens: list[dict[str, Any]], context: Any) -> list[dict[str, Any]]:
    result = []
    for index, token in enumerate(tokens):
        text = str(token.get("text") or "")
        next_start = float(tokens[index + 1].get("start", token.get("end", 0))) if index + 1 < len(tokens) else float(token.get("end", 0))
        result.append(
            {
                "i": index,
                "text": text,
                "pause_ms": round(max(0.0, next_start - float(token.get("end", 0))) * 1000),
            }
        )
    return result


def _line_end_limits(tokens: list[dict[str, Any]], context: Any, required: set[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for start in range(len(tokens)):
        comfortable_end = ""
        maximum_end = ""
        for end in range(start + 1, len(tokens) + 1):
            text = _token_text(tokens[start:end])
            measured = measure_text(text, context)
            end_id = str(tokens[end - 1].get("id") or "")
            if measured <= context.comfort_width:
                comfortable_end = end_id
            if measured <= context.hard_width:
                maximum_end = end_id
            else:
                break
            if end_id in required:
                break
        result.append(
            {
                "start_token_id": str(tokens[start].get("id") or ""),
                "comfortable_end_token_id": comfortable_end or maximum_end or str(tokens[start].get("id") or ""),
                "maximum_end_token_id": maximum_end or str(tokens[start].get("id") or ""),
            }
        )
    return result


def _line_options(
    tokens: list[dict[str, Any]],
    context: Any,
    preferred: set[str],
    required: set[str],
    forbidden: set[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for start in range(len(tokens)):
        candidates: list[dict[str, Any]] = []
        for end in range(start + 1, len(tokens) + 1):
            end_id = str(tokens[end - 1].get("id") or "")
            text = _token_text(tokens[start:end])
            measured = measure_text(text, context)
            if measured > context.hard_width + 0.5:
                break
            if end_id not in forbidden or end == len(tokens):
                pause = (
                    max(0.0, float(tokens[end].get("start", 0)) - float(tokens[end - 1].get("end", 0)))
                    if end < len(tokens)
                    else 0.0
                )
                candidates.append(
                    {
                        "option_id": f"o{start:03d}-{end:03d}",
                        "start_index": start,
                        "end_index": end,
                        "start_token_id": str(tokens[start].get("id") or ""),
                        "end_token_id": end_id,
                        "token_ids": [str(token.get("id") or "") for token in tokens[start:end]],
                        "text": text,
                        "width_px": round(measured, 1),
                        "fill": round(measured / max(1.0, context.comfort_width), 3),
                        "natural": end_id in preferred or end_id in required or pause >= 0.18,
                    }
                )
            if end_id in required:
                break
        if not candidates:
            token = tokens[start]
            candidates.append(
                {
                    "option_id": f"o{start:03d}-{start + 1:03d}",
                    "start_index": start,
                    "end_index": start + 1,
                    "start_token_id": str(token.get("id") or ""),
                    "end_token_id": str(token.get("id") or ""),
                    "token_ids": [str(token.get("id") or "")],
                    "text": str(token.get("text") or ""),
                    "width_px": round(measure_text(str(token.get("text") or ""), context), 1),
                    "fill": round(
                        measure_text(str(token.get("text") or ""), context) / max(1.0, context.comfort_width), 3
                    ),
                    "natural": False,
                }
            )
        ranked = sorted(candidates, key=lambda item: abs(float(item["width_px"]) - context.comfort_width))
        keep_ids = {item["option_id"] for item in ranked[:4]}
        keep_ids.add(candidates[-1]["option_id"])
        keep_ids.update(item["option_id"] for item in candidates if item["natural"])
        result.extend(item for item in candidates if item["option_id"] in keep_ids)
    return result


def _materialize_options(response: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
    option_ids = response.get("option_ids") if isinstance(response, dict) else None
    if not isinstance(option_ids, list):
        return response
    by_id = {str(option["option_id"]): option for option in options}
    sentences = []
    for option_id in option_ids:
        option = by_id.get(str(option_id))
        if option:
            sentences.append(
                {
                    "token_ids": list(option["token_ids"]),
                    "text": str(option["text"]),
                    "option_id": str(option["option_id"]),
                }
            )
    return {**response, "sentences": sentences}


def _validate_layout(
    response: dict[str, Any],
    tokens: list[dict[str, Any]],
    context: Any,
    required: set[str],
    forbidden: set[str],
) -> tuple[bool, list[str], list[list[str]]]:
    expected = [str(token.get("id") or "") for token in tokens]
    token_by_id = {str(token.get("id") or ""): token for token in tokens}
    selected: list[list[str]] = []
    errors: list[str] = []
    for index, sentence in enumerate(response.get("sentences", []) if isinstance(response, dict) else [], start=1):
        ids = [str(token_id) for token_id in sentence.get("token_ids", []) if str(token_id)]
        if not ids:
            errors.append(f"第 {index} 句没有 token_ids")
            continue
        selected.append(ids)
        actual_text = "".join(str(token_by_id.get(token_id, {}).get("text") or "") for token_id in ids)
        if actual_text != str(sentence.get("text") or ""):
            errors.append(f"第 {index} 句 text 与 token_ids 不一致")
        if measure_text(actual_text, context) > context.hard_width + 0.5:
            errors.append(f"第 {index} 句宽度 {measure_text(actual_text, context):.1f}px 超过 {context.hard_width:.1f}px")
    flattened = [token_id for ids in selected for token_id in ids]
    if flattened != expected:
        errors.append("token_ids 必须完整、连续且与输入顺序一致，不能丢字、加字、重复或换序")
    line_ends = {ids[-1] for ids in selected if ids}
    required_in_chunk = {token_id for token_id in expected[:-1] if token_id in required}
    missing_semantic = required_in_chunk - line_ends
    if missing_semantic:
        errors.append(f"跨越了完整语义句边界：{sorted(missing_semantic)}")
    invalid_ends = {token_id for token_id in line_ends if token_id in forbidden and token_id != expected[-1]}
    if invalid_ends:
        errors.append(f"从不可拆词组中间断开：{sorted(invalid_ends)}")
    return not errors and bool(selected), errors, selected


def _token_text(tokens: list[dict[str, Any]]) -> str:
    return "".join(str(token.get("text") or "") for token in tokens)
