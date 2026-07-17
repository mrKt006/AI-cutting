from __future__ import annotations

from pathlib import Path

from ffmpeg_utils import ffmpeg_filter_path, run, video_frame_rate


def burn_subtitles(video: Path, subtitle_ass: Path, output: Path, profile: str = "platform") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    ass_path = ffmpeg_filter_path(subtitle_ass)
    if profile == "platform":
        video_args = ["-r", "30", "-crf", "23", "-maxrate", "5M", "-bufsize", "10M"]
        audio_bitrate = "128k"
    elif profile == "master":
        source_fps = video_frame_rate(video)
        # `-fpsmax` is unavailable in the older FFmpeg build bundled with
        # SpleeterGUI. Preserve the source rate up to 60 FPS; only add the
        # widely supported output `-r` cap when the source is actually higher.
        video_args = (["-r", "60"] if source_fps > 60.01 else []) + ["-crf", "18"]
        audio_bitrate = "192k"
    else:
        # Jobs created before output profiles existed keep the previous encoding behavior.
        video_args = ["-crf", "20"]
        audio_bitrate = "160k"
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
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
