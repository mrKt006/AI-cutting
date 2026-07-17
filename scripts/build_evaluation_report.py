from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXED_SET_TARGET = 10


def build_report(jobs_dir: Path, gold_dir: Path | None = None) -> dict[str, Any]:
    gold_dir = gold_dir or jobs_dir.parent / "evaluation" / "gold"
    samples: list[dict[str, Any]] = []
    totals = {
        "corrections": 0,
        "applied_deletions": 0,
        "reviewed_applied_deletions": 0,
        "rejected_deletions": 0,
        "restored_deletions": 0,
        "user_deletions": 0,
        "text_edits": 0,
        "split_edits": 0,
        "merge_edits": 0,
        "title_edits": 0,
        "cover_edits": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "source_seconds": 0.0,
        "pipeline_seconds": 0.0,
        "manual_refinement_seconds": 0.0,
        "quality_gate_passed": 0,
        "quality_gate_blocked": 0,
        "layout_fallbacks": 0,
        "unverified_ai_fallbacks": 0,
        "gold_samples_scored": 0,
        "critical_term_errors": 0,
        "word_internal_breaks": 0,
        "false_deletions": 0,
        "false_deletion_seconds": 0.0,
        "expected_deletions": 0,
        "predicted_deletions": 0,
        "matched_deletions": 0,
        "expected_boundaries": 0,
        "predicted_boundaries": 0,
        "matched_boundaries": 0,
        "publish_ready_without_edits": 0,
    }
    for job_path in sorted(jobs_dir.glob("*/job.json")) if jobs_dir.exists() else []:
        job = _read_json(job_path)
        if not job:
            continue
        for item in job.get("params", {}).get("items", []):
            item_id = str(item.get("id") or "001")
            work_dir = job_path.parent / "work" / item_id
            analysis = _read_json(work_dir / "transcript_analysis.json")
            decisions = _read_json(work_dir / "ai_decisions.json")
            feedback = _read_json(work_dir / "training_feedback.json")
            review_complete = _feedback_review_complete(feedback)
            changes = feedback.get("user_changes", {}) if feedback else {}
            media = _read_json(work_dir / "checkpoints" / "media.json")
            validated_stage = _read_json(work_dir / "checkpoints" / "stage_validated.json")
            completed_stage = _read_json(work_dir / "checkpoints" / "stage_completed.json")
            baseline = _read_json(work_dir / "auto_edit_baseline.json")
            source = Path(str(item.get("video") or ""))
            source_name = str(item.get("source_name") or source.name)
            gold = _read_json(gold_dir / f"{Path(source_name).stem}.json")
            gold_reviewed = (gold.get("review") or {}).get("status") == "completed"
            eligible_item = bool(item.get("status") == "done" and source.is_file() and item.get("outputs"))
            source_seconds = float(media.get("original_duration") or 0.0)
            pipeline_seconds = _elapsed_seconds(validated_stage.get("updated_at"), completed_stage.get("updated_at"))
            refinement_seconds = _elapsed_seconds(baseline.get("captured_at"), feedback.get("updated_at")) if feedback else 0.0
            applied_deletions = len((decisions or {}).get("applied_deletions", []))
            quality_gate = (decisions or {}).get("quality_gate") or {}
            quality_metrics: dict[str, Any] = {}
            if eligible_item:
                if quality_gate.get("passed") is True:
                    totals["quality_gate_passed"] += 1
                else:
                    totals["quality_gate_blocked"] += 1
                fallback_data = (decisions or {}).get("fallbacks", [])
                if isinstance(fallback_data, list):
                    totals["layout_fallbacks"] += len(fallback_data)
                    totals["unverified_ai_fallbacks"] += len(fallback_data)
                elif isinstance(fallback_data, dict):
                    totals["layout_fallbacks"] += int(bool(fallback_data.get("layout")))
                    totals["unverified_ai_fallbacks"] += len(fallback_data.get("unverified", []))
            if eligible_item and gold_reviewed:
                quality_metrics = _score_against_gold(work_dir, decisions, gold)
                totals["gold_samples_scored"] += 1
                for key in (
                    "critical_term_errors", "word_internal_breaks", "false_deletions",
                    "expected_deletions", "predicted_deletions", "matched_deletions",
                    "expected_boundaries", "predicted_boundaries", "matched_boundaries",
                ):
                    totals[key] += int(quality_metrics[key])
                totals["false_deletion_seconds"] += float(quality_metrics["false_deletion_seconds"])
                if quality_gate.get("passed") is True and quality_metrics["publish_ready"]:
                    totals["publish_ready_without_edits"] += 1
            if eligible_item:
                totals["source_seconds"] += source_seconds
                totals["pipeline_seconds"] += pipeline_seconds
                usage = analysis.get("usage", {}) if analysis else {}
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    totals[key] += int(usage.get(key) or 0)
                totals["corrections"] += len(analysis.get("corrections", [])) if analysis else 0
                totals["applied_deletions"] += applied_deletions
                totals["rejected_deletions"] += len((decisions or {}).get("rejected_deletions", []))
            if eligible_item and review_complete:
                totals["reviewed_applied_deletions"] += applied_deletions
                totals["manual_refinement_seconds"] += refinement_seconds
                totals["restored_deletions"] += len(changes.get("restored_sentence_ids", []))
                totals["user_deletions"] += len(changes.get("removed_sentence_ids", []))
                totals["text_edits"] += len(changes.get("text_edits", []))
                totals["split_edits"] += len(changes.get("split_after_token_ids", []))
                totals["merge_edits"] += len(changes.get("merged_after_token_ids", []))
                totals["title_edits"] += len(changes.get("content_title_edits", []))
                totals["cover_edits"] += int(bool(changes.get("cover_edit")))
            samples.append(
                {
                    "sample_id": f"{job.get('id') or job_path.parent.name}/{item_id}",
                    "job_id": str(job.get("id") or job_path.parent.name),
                    "item_id": item_id,
                    "job_status": str(job.get("status") or ""),
                    "item_status": str(item.get("status") or ""),
                    "source_name": source_name,
                    "source_fingerprint": _file_fingerprint(source),
                    "has_transcript": bool(item.get("transcript_path")),
                    "has_ai_decisions": bool(decisions),
                    "has_user_feedback": bool(feedback),
                    "review_status": str((feedback.get("review") or {}).get("status") or "pending") if feedback else "pending",
                    "has_completed_review": review_complete,
                    "output_count": len(item.get("outputs") or {}),
                    "eligible": eligible_item,
                    "quality_gate_passed": quality_gate.get("passed") is True,
                    "gold_reviewed": gold_reviewed,
                    "quality_metrics": quality_metrics,
                }
            )
    eligible = [sample for sample in samples if sample["eligible"]]
    unique_sources = {sample["source_fingerprint"] for sample in eligible if sample["source_fingerprint"]}
    feedback_count = sum(1 for sample in eligible if sample["has_completed_review"])
    raw_feedback_count = sum(1 for sample in eligible if sample["has_user_feedback"])
    decision_count = sum(1 for sample in eligible if sample["has_ai_decisions"])
    reviewed_gold = sum(
        1
        for path in gold_dir.glob("*.json") if gold_dir.exists()
        if (_read_json(path).get("review") or {}).get("status") == "completed"
    )
    reviewed_applied = totals["reviewed_applied_deletions"]
    deletion_recall = (
        round(totals["matched_deletions"] / totals["expected_deletions"], 4)
        if totals["expected_deletions"] else None
    )
    boundary_f1 = _f1(
        totals["matched_boundaries"], totals["predicted_boundaries"], totals["expected_boundaries"]
    )
    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "all_items": len(samples),
            "eligible_items": len(eligible),
            "unique_eligible_sources": len(unique_sources),
            "items_with_ai_decisions": decision_count,
            "items_with_user_feedback": raw_feedback_count,
            "items_with_completed_review": feedback_count,
            "reviewed_gold_samples": reviewed_gold,
            "fixed_set_target": FIXED_SET_TARGET,
            "fixed_set_ready": len(unique_sources) >= FIXED_SET_TARGET,
            "quality_evidence_ready": (
                decision_count >= FIXED_SET_TARGET
                and reviewed_gold >= FIXED_SET_TARGET
                and totals["quality_gate_passed"] >= FIXED_SET_TARGET
                and totals["quality_gate_blocked"] == 0
                and totals["unverified_ai_fallbacks"] == 0
                and totals["critical_term_errors"] == 0
                and totals["word_internal_breaks"] == 0
                and totals["false_deletions"] == 0
                and deletion_recall is not None and deletion_recall >= 0.95
                and boundary_f1 is not None and boundary_f1 >= 0.95
                and totals["publish_ready_without_edits"] >= FIXED_SET_TARGET
            ),
        },
        "metrics": {
            **totals,
            "deletion_restore_rate": round(totals["restored_deletions"] / reviewed_applied, 4) if reviewed_applied else None,
            "segmentation_rework_events": totals["split_edits"] + totals["merge_edits"],
            "processing_seconds_per_source_minute": round(totals["pipeline_seconds"] / (totals["source_seconds"] / 60), 3)
            if totals["source_seconds"] > 0
            else None,
            "average_manual_refinement_seconds": round(totals["manual_refinement_seconds"] / feedback_count, 3)
            if feedback_count
            else None,
            "deletion_precision": round(totals["matched_deletions"] / totals["predicted_deletions"], 4)
            if totals["predicted_deletions"] else None,
            "deletion_recall": deletion_recall,
            "boundary_f1": boundary_f1,
            "publish_ready_rate": round(totals["publish_ready_without_edits"] / totals["gold_samples_scored"], 4)
            if totals["gold_samples_scored"] else None,
        },
        "gaps": _evaluation_gaps(len(unique_sources), decision_count, feedback_count, reviewed_gold),
        "samples": samples,
    }


def _evaluation_gaps(unique_sources: int, decisions: int, feedback: int, reviewed_gold: int) -> list[str]:
    gaps = []
    if unique_sources < FIXED_SET_TARGET:
        gaps.append(f"还缺 {FIXED_SET_TARGET - unique_sources} 条不重复的完成视频才能组成 {FIXED_SET_TARGET} 条固定测试集")
    if decisions < FIXED_SET_TARGET:
        gaps.append(f"还缺 {FIXED_SET_TARGET - decisions} 条新版 AI 决策记录")
    if feedback < FIXED_SET_TARGET:
        gaps.append(f"还缺 {FIXED_SET_TARGET - feedback} 条金标准审校样本，当前不能可靠计算删词召回率和断句 F1")
    if reviewed_gold < FIXED_SET_TARGET:
        gaps.append(f"还缺 {FIXED_SET_TARGET - reviewed_gold} 条完成逐段审校的金标准；AI 草稿不计入发布门")
    return gaps


def _file_fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(1024 * 1024))
        if size > 1024 * 1024:
            handle.seek(max(0, size - 1024 * 1024))
            digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def _score_against_gold(work_dir: Path, decisions: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    final_segments = _read_json_list(work_dir / "volcengine_segments.json")
    final_text = "".join(str(segment.get("text") or "") for segment in final_segments)
    boundary_ids = {
        str((segment.get("tokens") or [{}])[-1].get("id") or "")
        for segment in final_segments if segment.get("tokens")
    }
    expected_boundaries = {
        str(item.get("after_token_id") or "") for item in gold.get("breakpoints", []) if item.get("after_token_id")
    }
    critical_term_errors = sum(
        1 for item in gold.get("correct_terms", [])
        if str(item.get("text") or "") and str(item.get("text") or "") not in final_text
    )
    word_internal_breaks = 0
    for span in gold.get("protected_phrases", []):
        ids = [str(value) for value in span.get("token_ids", []) if str(value)]
        if ids and any(token_id in boundary_ids for token_id in ids[:-1]):
            word_internal_breaks += 1
    expected_deletions = [item for item in gold.get("deletion_labels", []) if item.get("expected_delete") is True]
    predicted_deletions = decisions.get("verified_deletions", decisions.get("applied_deletions", []))
    expected_sets = {tuple(str(value) for value in item.get("token_ids", [])) for item in expected_deletions}
    predicted_sets = {tuple(str(value) for value in item.get("token_ids", [])) for item in predicted_deletions}
    false_items = [item for item in predicted_deletions if tuple(str(value) for value in item.get("token_ids", [])) not in expected_sets]
    return {
        "critical_term_errors": critical_term_errors,
        "word_internal_breaks": word_internal_breaks,
        "false_deletions": len(false_items),
        "false_deletion_seconds": round(sum(max(0.0, float(item.get("end", 0)) - float(item.get("start", 0))) for item in false_items), 3),
        "expected_deletions": len(expected_sets),
        "predicted_deletions": len(predicted_sets),
        "matched_deletions": len(expected_sets & predicted_sets),
        "expected_boundaries": len(expected_boundaries),
        "predicted_boundaries": len(boundary_ids),
        "matched_boundaries": len(expected_boundaries & boundary_ids),
        "publish_ready": critical_term_errors == 0 and word_internal_breaks == 0 and not false_items,
    }


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _f1(matches: int, predicted: int, expected: int) -> float | None:
    if not predicted or not expected:
        return None
    precision = matches / predicted
    recall = matches / expected
    return round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0


def _feedback_review_complete(feedback: dict[str, Any]) -> bool:
    if not feedback:
        return False
    review = feedback.get("review") or {}
    final_result = feedback.get("final_result") or {}
    if review.get("status") != "completed" or not isinstance(final_result, dict):
        return False
    serialized = json.dumps(final_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return review.get("reviewed_snapshot_sha256") == expected


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _elapsed_seconds(start_value: object, end_value: object) -> float:
    if not start_value or not end_value:
        return 0.0
    try:
        start = datetime.fromisoformat(str(start_value))
        end = datetime.fromisoformat(str(end_value))
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Build a local AI-cutting quality evidence report")
    parser.add_argument("--jobs-dir", type=Path, default=ROOT / "jobs")
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation" / "report.local.json")
    args = parser.parse_args()
    report = build_report(args.jobs_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(
        f"Evaluation report: eligible={summary['eligible_items']} unique={summary['unique_eligible_sources']} "
        f"decisions={summary['items_with_ai_decisions']} feedback={summary['items_with_user_feedback']} "
        f"completed_reviews={summary['items_with_completed_review']}"
    )
    for gap in report["gaps"]:
        print(f"- {gap}")
    print(f"Written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
