from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def build_drafts(input_dir: Path, jobs_dir: Path, output_dir: Path, count: int = 10) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    artifacts = _latest_artifacts_by_source(jobs_dir)
    for index in range(1, count + 1):
        video = input_dir / f"{index}.mp4"
        if not video.is_file():
            continue
        artifact = artifacts.get(video.name, {})
        analysis = _read_json(Path(artifact.get("analysis", ""))) if artifact.get("analysis") else {}
        raw_segments = _read_list(Path(artifact.get("segments", ""))) if artifact.get("segments") else []
        decisions = _read_json(Path(artifact.get("decisions", ""))) if artifact.get("decisions") else {}
        transcript = "".join(str(segment.get("text") or "") for segment in raw_segments)
        draft = {
            "version": 1,
            "source": video.name,
            "source_sha256": _sha256(video),
            "source_bytes": video.stat().st_size,
            "status": "draft_pending_review",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "transcript": {
                "text": transcript,
                "segments": raw_segments,
                "source_job": artifact.get("job_id", ""),
            },
            "correct_terms": [
                {
                    "token_ids": item.get("token_ids", []),
                    "text": item.get("replacement", ""),
                    "source": "ai_draft",
                }
                for item in analysis.get("corrections", [])
            ],
            "protected_phrases": analysis.get("forbidden_breaks", []),
            "breakpoints": [
                {"after_token_id": sentence.get("token_ids", [""])[-1], "source": "ai_draft"}
                for sentence in analysis.get("final_sentences", [])
                if sentence.get("token_ids")
            ],
            "deletion_labels": [
                {
                    **candidate,
                    "expected_delete": any(
                        candidate.get("token_ids") == applied.get("token_ids")
                        for applied in decisions.get("verified_deletions", decisions.get("applied_deletions", []))
                    ),
                    "source": "ai_draft",
                }
                for candidate in decisions.get("deletion_candidates", analysis.get("deletion_candidates", []))
            ],
            "forbidden_deletions": [],
            "cover_safe_zones": {
                "avoid_top_ratio": 0.10,
                "person_safe_center_width_ratio": 0.45,
                "title_regions": ["top", "bottom"],
                "reviewed": False,
            },
            "review": {
                "status": "pending",
                "reviewed_at": None,
                "reviewed_by": None,
                "notes": "研发验收用；未完成逐段审校前不得计入发布门。",
            },
        }
        target = output_dir / f"{index}.json"
        target.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(target)
    return written


def _latest_artifacts_by_source(jobs_dir: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    paths = sorted(jobs_dir.glob("*/job.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for job_path in paths:
        job = _read_json(job_path)
        for item in job.get("params", {}).get("items", []):
            source = str(item.get("source_name") or "")
            if not source or source in result:
                continue
            work = job_path.parent / "work" / str(item.get("id") or "001")
            result[source] = {
                "job_id": str(job.get("id") or job_path.parent.name),
                "analysis": str(work / "transcript_analysis.json"),
                "segments": str(work / "raw_transcript_segments.json"),
                "decisions": str(work / "ai_decisions.json"),
            }
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_list(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate reviewable gold-standard drafts for the fixed video set")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "input")
    parser.add_argument("--jobs-dir", type=Path, default=ROOT / "jobs")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evaluation" / "gold")
    parser.add_argument("--count", type=int, default=10)
    args = parser.parse_args()
    written = build_drafts(args.input_dir, args.jobs_dir, args.output_dir, max(1, args.count))
    print(f"Gold-standard drafts written: {len(written)}")
    return 0 if len(written) == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
