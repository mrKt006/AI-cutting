from __future__ import annotations

from pathlib import Path

from ffmpeg_utils import ffmpeg_filter_path, run


def burn_subtitles(video: Path, subtitle_ass: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    ass_path = ffmpeg_filter_path(subtitle_ass)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"subtitles='{ass_path}'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
