from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "volc_access_token",
    "access_token",
    "current_video",
    "render_source_video",
}


def export_feedback(jobs_dir: Path, output: Path) -> tuple[int, int]:
    records = []
    skipped_without_consent = 0
    for path in sorted(jobs_dir.glob("*/work/*/training_feedback.json")):
        try:
            feedback = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not bool((feedback.get("data_policy") or {}).get("training_consent")):
            skipped_without_consent += 1
            continue
        analysis = feedback.get("ai_decision") or {}
        record = {
            "id": _anonymous_record_id(feedback),
            "schema_version": 1,
            "prompt": {
                "raw_transcript": feedback.get("raw_transcript", []),
                "auto_edit_mode": analysis.get("auto_edit_mode", "standard"),
            },
            "rejected": feedback.get("initial_result", {}),
            "chosen": feedback.get("final_result", {}),
            "ai_decision": {
                key: analysis.get(key, [])
                for key in (
                    "corrections",
                    "break_hints",
                    "allowed_breaks",
                    "forbidden_breaks",
                    "protected_spans",
                    "delete_ranges",
                    "applied_delete_ranges",
                    "skipped_delete_ranges",
                    "final_sentences",
                )
            },
            "user_changes": feedback.get("user_changes", {}),
        }
        records.append(_redact(record))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
    return len(records), skipped_without_consent


def _anonymous_record_id(feedback: dict[str, Any]) -> str:
    value = json.dumps(
        {
            "job": feedback.get("job_id"),
            "item": feedback.get("item_id"),
            "updated": feedback.get("updated_at"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _redact(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if normalized in SENSITIVE_KEYS or normalized.endswith("_path"):
            continue
        cleaned[key] = _redact(item)
    return cleaned


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Export explicitly consented auto-edit feedback as JSONL")
    parser.add_argument("--jobs-dir", type=Path, default=ROOT / "jobs")
    parser.add_argument("--output", type=Path, default=ROOT / "training_data" / "auto_edit_feedback.jsonl")
    args = parser.parse_args()
    count, skipped = export_feedback(args.jobs_dir, args.output)
    print(f"Exported {count} consented feedback record(s) to {args.output}")
    if skipped:
        print(f"Skipped {skipped} local record(s) without training consent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
