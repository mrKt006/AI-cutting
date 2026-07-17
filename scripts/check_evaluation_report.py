from __future__ import annotations

import json
import hashlib
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_evaluation_report import build_report  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="evaluation-check-", dir=ROOT) as raw_tmp:
        jobs_dir = Path(raw_tmp) / "jobs"
        gold_dir = Path(raw_tmp) / "evaluation" / "gold"
        gold_dir.mkdir(parents=True)
        for index in range(20):
            job_dir = jobs_dir / f"job-{index:02d}"
            work_dir = job_dir / "work" / "001"
            source = job_dir / f"source-{index:02d}.mp4"
            output = job_dir / "output.mp4"
            work_dir.mkdir(parents=True)
            source.write_bytes((f"video-{index}" * 32).encode("utf-8"))
            output.write_bytes(b"output")
            job = {
                "id": job_dir.name,
                "status": "done",
                "params": {
                    "items": [
                        {
                            "id": "001",
                            "status": "done",
                            "video": str(source),
                            "source_name": source.name,
                            "outputs": {"成片.mp4": str(output)},
                        }
                    ]
                },
            }
            (job_dir / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            (work_dir / "transcript_analysis.json").write_text(
                json.dumps({"corrections": [{"token_ids": ["t1"]}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
                encoding="utf-8",
            )
            (work_dir / "ai_decisions.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "applied_deletions": [{"token_ids": ["t1"]}],
                        "verified_deletions": [{"token_ids": ["t1"], "start": 0.0, "end": 0.1}],
                        "rejected_deletions": [],
                        "fallbacks": {"layout": None, "unverified": []},
                        "quality_gate": {"passed": True, "blockers": []},
                    }
                ),
                encoding="utf-8",
            )
            final_result = {"sentences": [{"id": "s1", "text": f"sample-{index}"}]}
            serialized_snapshot = json.dumps(final_result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            review_hash = hashlib.sha256(serialized_snapshot.encode("utf-8")).hexdigest()
            (work_dir / "training_feedback.json").write_text(
                json.dumps(
                    {
                        "updated_at": "2026-01-01T00:01:10",
                        "final_result": final_result,
                        "review": {
                            "status": "completed",
                            "completed_at": "2026-01-01T00:01:10",
                            "reviewed_snapshot_sha256": review_hash,
                        },
                        "user_changes": {"restored_sentence_ids": ["s1"] if index == 0 else [], "text_edits": []},
                    }
                ),
                encoding="utf-8",
            )
            checkpoints = work_dir / "checkpoints"
            checkpoints.mkdir()
            (checkpoints / "media.json").write_text(json.dumps({"original_duration": 60}), encoding="utf-8")
            (checkpoints / "stage_validated.json").write_text(
                json.dumps({"updated_at": "2026-01-01T00:00:00"}), encoding="utf-8"
            )
            (checkpoints / "stage_completed.json").write_text(
                json.dumps({"updated_at": "2026-01-01T00:00:30"}), encoding="utf-8"
            )
            (work_dir / "auto_edit_baseline.json").write_text(
                json.dumps({"captured_at": "2026-01-01T00:01:00"}), encoding="utf-8"
            )
            (work_dir / "volcengine_segments.json").write_text(
                json.dumps([{"text": "sample", "tokens": [{"id": "t1", "text": "sample"}]}]), encoding="utf-8"
            )
            (gold_dir / f"source-{index:02d}.json").write_text(
                json.dumps(
                    {
                        "review": {"status": "completed"},
                        "correct_terms": [],
                        "protected_phrases": [],
                        "breakpoints": [{"after_token_id": "t1"}],
                        "deletion_labels": [{"token_ids": ["t1"], "expected_delete": True}],
                    }
                ),
                encoding="utf-8",
            )
        report = build_report(jobs_dir)
        assert report["summary"]["fixed_set_ready"]
        assert report["summary"]["quality_evidence_ready"]
        assert report["summary"]["items_with_completed_review"] == 20
        assert report["summary"]["unique_eligible_sources"] == 20
        assert report["metrics"]["applied_deletions"] == 20
        assert report["metrics"]["deletion_restore_rate"] == 0.05
        assert report["metrics"]["total_tokens"] == 240
        assert report["metrics"]["processing_seconds_per_source_minute"] == 30
        assert report["metrics"]["average_manual_refinement_seconds"] == 10
        assert report["gaps"] == []
        serialized = json.dumps(report, ensure_ascii=False)
        assert raw_tmp not in serialized

    print("Evaluation report check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
