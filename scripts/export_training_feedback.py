from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Export local auto-edit feedback as preference JSONL")
    parser.add_argument("--output", default="training_data/auto_edit_feedback.jsonl")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    records = []
    for path in sorted((ROOT / "jobs").glob("*/work/*/training_feedback.json")):
        try:
            feedback = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records.append(
            {
                "id": f"{feedback.get('job_id', '')}/{feedback.get('item_id', '')}",
                "prompt": {
                    "raw_transcript": feedback.get("raw_transcript", []),
                    "auto_edit_mode": feedback.get("ai_decision", {}).get("auto_edit_mode", "standard"),
                },
                "rejected": feedback.get("initial_result", {}),
                "chosen": feedback.get("final_result", {}),
                "ai_decision": feedback.get("ai_decision", {}),
                "auto_edit_plan": feedback.get("auto_edit_plan", {}),
                "user_changes": feedback.get("user_changes", {}),
            }
        )
    output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
    print(f"Exported {len(records)} feedback record(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
