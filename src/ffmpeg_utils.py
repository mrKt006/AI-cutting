from __future__ import annotations

import subprocess
from pathlib import Path

from safe_json import loads_json


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=True)


def require_tool(name: str) -> None:
    try:
        run([name, "-version"])
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Required tool not found or not working: {name}") from exc


def probe_media(path: Path) -> dict:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=codec_type,width,height",
            "-show_entries",
            "format=duration",
            str(path),
        ]
    )
    return loads_json(result.stdout, f"ffprobe output for {path}")


def media_duration(path: Path) -> float:
    data = probe_media(path)
    return float(data["format"]["duration"])


def video_size(path: Path) -> tuple[int, int]:
    data = probe_media(path)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            return int(stream["width"]), int(stream["height"])
    raise RuntimeError(f"No video stream found in {path}")


def ffmpeg_filter_path(path: Path) -> str:
    normalized = path.resolve().as_posix()
    normalized = normalized.replace(":", r"\:")
    normalized = normalized.replace("'", r"\'")
    return normalized
