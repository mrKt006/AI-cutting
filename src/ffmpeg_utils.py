from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

from safe_json import loads_json


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stderr_lines = [line.strip() for line in str(exc.stderr or "").splitlines() if line.strip()]
        stdout_lines = [line.strip() for line in str(exc.stdout or "").splitlines() if line.strip()]
        diagnostic = "\n".join((stderr_lines or stdout_lines)[-12:])
        if len(diagnostic) > 4000:
            diagnostic = diagnostic[-4000:]
        tool = Path(str(cmd[0])).name or str(cmd[0])
        message = f"{tool} 执行失败（退出码 {exc.returncode}）"
        if diagnostic:
            message += ":\n" + diagnostic
        raise RuntimeError(message) from exc


def require_tool(name: str) -> None:
    try:
        run([name, "-version"])
    except (FileNotFoundError, RuntimeError) as exc:
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
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate",
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


def video_frame_rate(path: Path) -> float:
    """Return the source video FPS, preferring the average rate."""
    data = probe_media(path)
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = str(stream.get(key) or "").strip()
            if not value or value in {"0/0", "N/A"}:
                continue
            try:
                rate = float(Fraction(value))
            except (ValueError, ZeroDivisionError):
                continue
            if rate > 0:
                return rate
    return 0.0


def ffmpeg_filter_path(path: Path) -> str:
    normalized = path.resolve().as_posix()
    normalized = normalized.replace(":", r"\:")
    normalized = normalized.replace("'", r"\'")
    return normalized
