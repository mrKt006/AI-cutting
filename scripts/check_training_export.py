from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from export_training_feedback import export_feedback  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="training-export-", dir=ROOT) as raw_tmp:
        root = Path(raw_tmp)
        jobs = root / "jobs"
        for index, consent in enumerate((False, True)):
            work = jobs / f"job-{index}" / "work" / "001"
            work.mkdir(parents=True)
            feedback = {
                "job_id": f"job-{index}",
                "item_id": "001",
                "updated_at": "2026-01-01T00:00:00",
                "data_policy": {"training_consent": consent},
                "raw_transcript": [{"text": "测试"}],
                "ai_decision": {"corrections": [], "api_key": "must-not-export"},
                "initial_result": {"current_video": "C:/private/video.mp4", "sentences": []},
                "final_result": {"sentences": []},
                "user_changes": {},
            }
            (work / "training_feedback.json").write_text(json.dumps(feedback, ensure_ascii=False), encoding="utf-8")
        output = root / "feedback.jsonl"
        count, skipped = export_feedback(jobs, output)
        assert count == 1 and skipped == 1
        exported = output.read_text(encoding="utf-8")
        assert "must-not-export" not in exported
        assert "C:/private" not in exported
        assert "job-1" not in exported
        assert len(json.loads(exported)["id"]) == 24

    print("Training export check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
