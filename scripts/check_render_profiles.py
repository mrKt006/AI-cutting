from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ffmpeg_utils  # noqa: E402
import render_video  # noqa: E402


def _command_for(profile: str, fps: float) -> list[str]:
    with patch("render_video.video_frame_rate", return_value=fps), patch("render_video.run") as run_mock:
        render_video.burn_subtitles(
            Path("input.mp4"), Path("subtitle.ass"), Path("output.mp4"), profile=profile
        )
    return run_mock.call_args.args[0]


def main() -> int:
    master_60 = _command_for("master", 60.0)
    assert "-fpsmax" not in master_60
    assert "-r" not in master_60
    assert master_60[master_60.index("-crf") + 1] == "18"

    master_high = _command_for("master", 120.0)
    assert "-fpsmax" not in master_high
    assert master_high[master_high.index("-r") + 1] == "60"

    platform = _command_for("platform", 60.0)
    assert platform[platform.index("-r") + 1] == "30"
    assert platform[platform.index("-maxrate") + 1] == "5M"

    error = subprocess.CalledProcessError(
        1,
        ["ffmpeg", "-fpsmax", "60"],
        output="",
        stderr="ffmpeg banner\nUnrecognized option 'fpsmax'.\nError splitting the argument list: Option not found\n",
    )
    with patch("ffmpeg_utils.subprocess.run", side_effect=error):
        try:
            ffmpeg_utils.run(["ffmpeg", "-fpsmax", "60"])
        except RuntimeError as exc:
            message = str(exc)
            assert "Unrecognized option 'fpsmax'" in message
            assert "退出码 1" in message
        else:
            raise AssertionError("FFmpeg stderr was not surfaced")

    print("Render profile compatibility check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
