from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import main as pipeline  # noqa: E402
from make_subtitle import TimingSegment  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-cutting-pause-") as raw_tmp:
        root = Path(raw_tmp)
        control = root / "control.json"
        checkpoints = root / "checkpoints"
        args = argparse.Namespace(control_file=str(control), checkpoint_dir=str(checkpoints))

        control.write_text(json.dumps({"pause_requested": False}), encoding="utf-8")
        pipeline._stage_checkpoint(args, "validated")
        state = json.loads((checkpoints / "state.json").read_text(encoding="utf-8"))
        assert state["completed_stage"] == "validated"

        control.write_text(json.dumps({"pause_requested": True}), encoding="utf-8")
        try:
            pipeline._stage_checkpoint(args, "asr")
        except pipeline.PauseRequested:
            pass
        else:
            raise AssertionError("pause request did not stop at the checkpoint")

        control.write_text(json.dumps({"pause_requested": False, "cancel_requested": True}), encoding="utf-8")
        try:
            pipeline._stage_checkpoint(args, "analysis")
        except pipeline.CancelRequested:
            pass
        else:
            raise AssertionError("cancel request did not stop at the checkpoint")

        segments = [
            TimingSegment(
                0.1,
                0.8,
                "AI获客系统",
                ({"id": "t1", "text": "AI", "start": 0.1, "end": 0.2},),
            )
        ]
        pipeline._save_checkpoint(checkpoints, "asr", {"segments": pipeline._timing_segments_data(segments)})
        restored = pipeline._timing_segments_from_data(pipeline._load_checkpoint(checkpoints, "asr")["segments"])
        assert len(restored) == 1
        assert restored[0].text == "AI获客系统"
        assert restored[0].tokens[0]["id"] == "t1"

    print("Pause checkpoint checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
