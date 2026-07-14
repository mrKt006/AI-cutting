from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / "src"))

import main as pipeline  # noqa: E402
from make_subtitle import TimingSegment  # noqa: E402
from subtitle_layout import tokens_from_text  # noqa: E402


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("FFmpeg unavailable; skipped pipeline resume check.")
        return 0
    with tempfile.TemporaryDirectory(prefix="pipeline-resume-", dir=ROOT) as raw_tmp:
        root = Path(raw_tmp)
        video = root / "input.mp4"
        output = root / "output"
        work = root / "work"
        checkpoints = work / "checkpoints"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=white:s=360x640:d=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1.2",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(video),
            ],
            check=True,
            capture_output=True,
        )

        tokens = tokens_from_text("测试字幕", 0.1, 1.0, prefix="resume")
        segments = [TimingSegment(0.1, 1.0, "测试字幕", tuple(tokens))]
        segment_data = pipeline._timing_segments_data(segments)
        pipeline._save_checkpoint(checkpoints, "asr", {"segments": segment_data, "response": {"code": 0}})
        pipeline._save_checkpoint(
            checkpoints,
            "alignment",
            {"segments": segment_data, "report": {"status": "skipped", "reason": "test"}},
        )
        pipeline._save_checkpoint(
            checkpoints,
            "analysis",
            {
                "status": "skipped",
                "reason": "test",
                "corrections": [],
                "break_hints": [],
                "allowed_breaks": [],
                "forbidden_breaks": [],
                "protected_spans": [],
                "delete_ranges": [],
                "final_sentences": [],
            },
        )
        pipeline._save_checkpoint(
            checkpoints,
            "edit_plan",
            {"keep_segments": [{"start": 0.0, "end": 1.2}], "removed_segments": [], "removed_token_ids": []},
        )
        pipeline._save_checkpoint(
            checkpoints,
            "titles",
            {
                "content_analysis": {"status": "skipped"},
                "cover_analysis": {"status": "skipped"},
                "video_text": "测试标题",
                "cover_text": "测试标题",
                "video_layout": {},
                "cover_layout": {},
            },
        )
        pipeline._save_checkpoint(checkpoints, "subtitle_layout", {"segments": segment_data, "layout_decision": {}})

        command = [
            PYTHON,
            str(ROOT / "src" / "main.py"),
            "--video",
            str(video),
            "--script",
            str(root / "missing.txt"),
            "--title",
            "测试标题",
            "--output-dir",
            str(output),
            "--output-basename",
            "恢复测试",
            "--editor-work-dir",
            str(work),
            "--checkpoint-dir",
            str(checkpoints),
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        first = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, env=env)
        if first.returncode:
            print(first.stdout)
            print(first.stderr)
            return first.returncode
        artifacts = [
            checkpoints / "artifacts" / "cut_no_subtitles.mp4",
            output / "恢复测试.mp4",
            output / "恢复测试-封面.jpg",
        ]
        if not all(path.is_file() for path in artifacts):
            raise AssertionError("first run did not create resumable artifacts")
        mtimes = {path: path.stat().st_mtime_ns for path in artifacts}
        time.sleep(0.05)
        second = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, env=env)
        if second.returncode:
            print(second.stdout)
            print(second.stderr)
            return second.returncode
        assert all(path.stat().st_mtime_ns == mtimes[path] for path in artifacts)
        assert (checkpoints / "stage_completed.json").is_file()

    print("Pipeline resume check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
