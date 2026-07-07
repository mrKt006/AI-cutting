from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ffmpeg_utils import media_duration, run


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def detect_silences(video: Path, noise: str, min_duration: float) -> list[Segment]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(video),
        "-af",
        f"silencedetect=noise={noise}:d={min_duration}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg silencedetect failed")

    starts: list[float] = []
    silences: list[Segment] = []
    for line in proc.stderr.splitlines():
        start = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start:
            starts.append(float(start.group(1)))
            continue
        end = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end and starts:
            silences.append(Segment(starts.pop(0), float(end.group(1))))
    return silences


def build_keep_segments(duration: float, silences: list[Segment], padding: float) -> tuple[list[Segment], list[Segment]]:
    remove: list[Segment] = []
    for silence in silences:
        start = max(0.0, silence.start + padding)
        end = min(duration, silence.end - padding)
        if end - start > 0.05:
            remove.append(Segment(start, end))

    keep: list[Segment] = []
    cursor = 0.0
    for segment in remove:
        if segment.start > cursor:
            keep.append(Segment(cursor, segment.start))
        cursor = max(cursor, segment.end)
    if cursor < duration:
        keep.append(Segment(cursor, duration))

    keep = [segment for segment in keep if segment.duration > 0.05]
    return keep or [Segment(0.0, duration)], remove


def cut_video(video: Path, output: Path, keep_segments: list[Segment]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if len(keep_segments) == 1 and keep_segments[0].start <= 0.01:
        run(["ffmpeg", "-y", "-i", str(video), "-c", "copy", str(output)])
        return

    parts: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(keep_segments):
        parts.append(
            f"[0:v]trim=start={segment.start:.3f}:end={segment.end:.3f},setpts=PTS-STARTPTS[v{index}];"
            f"[0:a]atrim=start={segment.start:.3f}:end={segment.end:.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
        labels.append(f"[v{index}][a{index}]")

    filter_complex = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(keep_segments)}:v=1:a=1[v][a]"
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
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
            str(output),
        ]
    )


def auto_cut(video: Path, output: Path, noise: str, min_duration: float, padding: float) -> tuple[list[Segment], list[Segment], float, float]:
    original_duration = media_duration(video)
    silences = detect_silences(video, noise, min_duration)
    keep, removed = build_keep_segments(original_duration, silences, padding)
    cut_video(video, output, keep)
    output_duration = media_duration(output)
    return keep, removed, original_duration, output_duration
