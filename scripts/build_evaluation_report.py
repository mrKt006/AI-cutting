from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def build_report(jobs_dir: Path) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    totals = {
        "corrections": 0,
        "applied_deletions": 0,
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
            changes = feedback.get("user_changes", {}) if feedback else {}
            media = _read_json(work_dir / "checkpoints" / "media.json")
            validated_stage = _read_json(work_dir / "checkpoints" / "stage_validated.json")
            completed_stage = _read_json(work_dir / "checkpoints" / "stage_completed.json")
            baseline = _read_json(work_dir / "auto_edit_baseline.json")
            source_seconds = float(media.get("original_duration") or 0.0)
            pipeline_seconds = _elapsed_seconds(validated_stage.get("updated_at"), completed_stage.get("updated_at"))
            refinement_seconds = _elapsed_seconds(baseline.get("captured_at"), feedback.get("updated_at")) if feedback else 0.0
            totals["source_seconds"] += source_seconds
            totals["pipeline_seconds"] += pipeline_seconds
            totals["manual_refinement_seconds"] += refinement_seconds
            usage = analysis.get("usage", {}) if analysis else {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                totals[key] += int(usage.get(key) or 0)
            totals["corrections"] += len(analysis.get("corrections", [])) if analysis else 0
            totals["applied_deletions"] += len((decisions or {}).get("applied_deletions", []))
            totals["rejected_deletions"] += len((decisions or {}).get("rejected_deletions", []))
            totals["restored_deletions"] += len(changes.get("restored_sentence_ids", []))
            totals["user_deletions"] += len(changes.get("removed_sentence_ids", []))
            totals["text_edits"] += len(changes.get("text_edits", []))
            totals["split_edits"] += len(changes.get("split_after_token_ids", []))
            totals["merge_edits"] += len(changes.get("merged_after_token_ids", []))
            totals["title_edits"] += len(changes.get("content_title_edits", []))
            totals["cover_edits"] += int(bool(changes.get("cover_edit")))
            source = Path(str(item.get("video") or ""))
            samples.append(
                {
                    "sample_id": f"{job.get('id') or job_path.parent.name}/{item_id}",
                    "job_status": str(job.get("status") or ""),
                    "item_status": str(item.get("status") or ""),
                    "source_name": str(item.get("source_name") or source.name),
                    "source_fingerprint": _file_fingerprint(source),
                    "has_transcript": bool(item.get("transcript_path")),
                    "has_ai_decisions": bool(decisions),
                    "has_user_feedback": bool(feedback),
                    "output_count": len(item.get("outputs") or {}),
                    "eligible": bool(item.get("status") == "done" and source.is_file() and item.get("outputs")),
                }
            )
    eligible = [sample for sample in samples if sample["eligible"]]
    unique_sources = {sample["source_fingerprint"] for sample in eligible if sample["source_fingerprint"]}
    feedback_count = sum(1 for sample in eligible if sample["has_user_feedback"])
    decision_count = sum(1 for sample in eligible if sample["has_ai_decisions"])
    applied = totals["applied_deletions"]
    return {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "all_items": len(samples),
            "eligible_items": len(eligible),
            "unique_eligible_sources": len(unique_sources),
            "items_with_ai_decisions": decision_count,
            "items_with_user_feedback": feedback_count,
            "fixed_set_target": 20,
            "fixed_set_ready": len(unique_sources) >= 20,
            "quality_evidence_ready": decision_count >= 20 and feedback_count >= 20,
        },
        "metrics": {
            **totals,
            "deletion_restore_rate": round(totals["restored_deletions"] / applied, 4) if applied else None,
            "segmentation_rework_events": totals["split_edits"] + totals["merge_edits"],
            "processing_seconds_per_source_minute": round(totals["pipeline_seconds"] / (totals["source_seconds"] / 60), 3)
            if totals["source_seconds"] > 0
            else None,
            "average_manual_refinement_seconds": round(totals["manual_refinement_seconds"] / feedback_count, 3)
            if feedback_count
            else None,
        },
        "gaps": _evaluation_gaps(len(unique_sources), decision_count, feedback_count),
        "samples": samples,
    }


def _evaluation_gaps(unique_sources: int, decisions: int, feedback: int) -> list[str]:
    gaps = []
    if unique_sources < 20:
        gaps.append(f"还缺 {20 - unique_sources} 条不重复的完成视频才能组成 20 条固定测试集")
    if decisions < 20:
        gaps.append(f"还缺 {20 - decisions} 条新版 AI 决策记录")
    if feedback < 20:
        gaps.append(f"还缺 {20 - feedback} 条人工复核反馈，当前不能可靠估算错误删除率和返工率")
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
        f"decisions={summary['items_with_ai_decisions']} feedback={summary['items_with_user_feedback']}"
    )
    for gap in report["gaps"]:
        print(f"- {gap}")
    print(f"Written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
