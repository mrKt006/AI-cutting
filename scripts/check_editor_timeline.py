from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "src"))

from fastapi import HTTPException  # noqa: E402

from app import (  # noqa: E402
    _build_edit_plan,
    _is_clean_editor_source,
    _render_edit_project,
    _render_timeline_video,
    _sanitize_sentences,
    _timeline_clips,
    _write_edit_ass,
)
from cut_silence import Segment  # noqa: E402
from ffmpeg_utils import media_duration, run  # noqa: E402
from make_subtitle import SubtitleCue, write_ass  # noqa: E402


def main() -> int:
    project = {
        "duration": 10.0,
        "style_preset_id": "default-white",
        "settings": {"subtitle_offset": 0.0},
        "title_clips": [
            {
                "id": "t001",
                "start": 0.5,
                "end": 2.0,
                "text": "title",
                "enabled": True,
                "use_for_cover": True,
            }
        ],
        "sentences": [
            {
                "id": "s001",
                "start": 0.0,
                "end": 1.0,
                "clip_start": 0.0,
                "clip_end": 1.0,
                "timeline_order": 3,
                "text": "first",
                "enabled": True,
                "remove_video": False,
            },
            {
                "id": "s002",
                "start": 1.0,
                "end": 3.0,
                "clip_start": 1.0,
                "clip_end": 2.5,
                "timeline_order": 1,
                "text": "trimmed",
                "enabled": True,
                "remove_video": False,
            },
            {
                "id": "s003",
                "start": 3.0,
                "end": 5.0,
                "clip_start": 3.0,
                "clip_end": 5.0,
                "timeline_order": 2,
                "text": "third",
                "enabled": False,
                "remove_video": False,
                "gap": True,
                "synthetic_gap": True,
            },
            {
                "id": "s004",
                "start": 5.0,
                "end": 6.0,
                "clip_start": 5.0,
                "clip_end": 6.0,
                "timeline_order": 4,
                "text": "removed",
                "enabled": True,
                "remove_video": True,
            },
        ],
    }
    clips = _timeline_clips(project, 10.0)
    plan = _build_edit_plan(project, clips, [], [], sum(clip["segment"].duration for clip in clips))
    segments = plan["timeline_segments"]

    assert [item["sentence_id"] for item in segments] == ["s002", "s003", "s001"]
    assert segments[0]["source_start"] == 1.0
    assert segments[0]["source_end"] == 2.5
    assert segments[0]["timeline_start"] == 0.0
    assert segments[0]["timeline_end"] == 1.5
    assert segments[1]["subtitle_enabled"] is False
    assert segments[1]["type"] == "gap"
    assert segments[1]["gap"] is True
    assert segments[1]["synthetic_gap"] is True
    assert plan["removed_sentence_ids"] == ["s004"]
    assert plan["edited_duration"] == 4.5
    assert plan["title_clips"] == [
        {"id": "t001", "start": 0.5, "end": 2.0, "text": "title", "enabled": True, "use_for_cover": True}
    ]
    cleaned = _sanitize_sentences(
        [
            {
                "id": "g001",
                "start": 0.0,
                "end": 2.5,
                "clip_start": 0.0,
                "clip_end": 2.5,
                "timeline_order": 1,
                "gap": True,
                "synthetic_gap": True,
                "remove_video": False,
            }
        ],
        {},
        1.0,
    )
    assert cleaned[0]["clip_end"] == 2.5
    assert cleaned[0]["synthetic_gap"] is True
    with tempfile.TemporaryDirectory(prefix="ai-cutting-gap-") as tmp:
        tmp_dir = Path(tmp)
        edit_ass = tmp_dir / "edit.ass"
        _write_edit_ass(
            [SubtitleCue(index=1, start=0.0, end=1.0, text="单行字幕")],
            [SubtitleCue(index=1, start=0.0, end=1.0, text="第一行\n第二行")],
            edit_ass,
            width=1080,
            height=1920,
            preset={"subtitle": {"font_size": 64}, "cover_title": {"font_size": 76, "position_y": -520}},
        )
        edit_ass_text = edit_ass.read_text(encoding="utf-8")
        assert r"\q2" in edit_ass_text
        assert r"第一行\N第二行" in edit_ass_text

        cli_ass = tmp_dir / "cli.ass"
        write_ass([SubtitleCue(index=1, start=0.0, end=1.0, text="字幕A\n字幕B")], cli_ass)
        cli_ass_text = cli_ass.read_text(encoding="utf-8")
        assert r"\q2" in cli_ass_text
        assert r"字幕A\N字幕B" in cli_ass_text

        job_dir = tmp_dir / "job"
        burned_output = job_dir / "output" / "burned.mp4"
        clean_source = job_dir / "work" / "001" / "cut_no_subtitles.mp4"
        burned_output.parent.mkdir(parents=True, exist_ok=True)
        clean_source.parent.mkdir(parents=True, exist_ok=True)
        burned_output.write_bytes(b"not-a-real-video")
        clean_source.write_bytes(b"not-a-real-video")
        assert not _is_clean_editor_source(job_dir, burned_output)
        assert _is_clean_editor_source(job_dir, clean_source)
        try:
            _render_edit_project(
                job_dir,
                {"id": "job", "params": {"items": [{"id": "001", "title": "t", "output_dir": str(job_dir / "output")}]}},
                {
                    "item_id": "001",
                    "duration": 1.0,
                    "render_source_video": str(burned_output),
                    "sentences": [],
                    "title": {"cover_text": "t"},
                    "title_clips": [],
                    "style_preset_id": "default-white",
                },
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "无字幕精修源" in str(exc.detail)
        else:
            raise AssertionError("burned fallback source must not be accepted for edited export")

        source = tmp_dir / "source.mp4"
        output = tmp_dir / "with-gap.mp4"
        run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=30:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(source),
            ]
        )
        _render_timeline_video(
            source,
            output,
            [
                {"sentence": {"id": "a"}, "segment": Segment(0.0, 0.5)},
                {"sentence": {"id": "gap", "gap": True}, "segment": Segment(0.5, 1.5)},
                {"sentence": {"id": "b"}, "segment": Segment(1.5, 2.0)},
            ],
        )
        rendered_duration = media_duration(output)
        assert 1.85 <= rendered_duration <= 2.15, rendered_duration
    print("Editor timeline plan check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
