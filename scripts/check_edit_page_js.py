from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*")

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.app import app  # noqa: E402


SCRIPT_RE = re.compile(r"(?s)<script>\s*(.*?)\s*</script>")


def _job_candidates() -> list[tuple[str, str]]:
    jobs_dir = ROOT / "jobs"
    if not jobs_dir.exists():
        return []

    candidates: list[tuple[float, str, str]] = []
    for job_json in jobs_dir.glob("*/job.json"):
        try:
            job = json.loads(job_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = job.get("items") if isinstance(job.get("items"), list) else [{"id": "001"}]
        for item in items or [{"id": "001"}]:
            item_id = str(item.get("id") or "001")
            candidates.append((job_json.stat().st_mtime, job_json.parent.name, item_id))

    candidates.sort(reverse=True)
    return [(job_id, item_id) for _, job_id, item_id in candidates]


def _first_edit_page(client: TestClient) -> tuple[str, str, str] | None:
    for job_id, item_id in _job_candidates():
        response = client.get(f"/jobs/{job_id}/edit", params={"item": item_id})
        if response.status_code == 200 and "<script>" in response.text:
            return job_id, item_id, response.text
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    client = TestClient(app, raise_server_exceptions=False)
    page = _first_edit_page(client)
    if page is None:
        print("No renderable edit page found; skipped edit page JS check.")
        return 0

    job_id, item_id, html = page
    required_fragments = [
        'id="timeline-status"',
        "拖动片段调整顺序",
        "defaultTimelineStatus",
        "setTimelineStatus",
        "manualSelectionLockMs",
        "lockManualTimeSync",
        "isManualTimeSyncLocked",
        "handleEditableEnter",
        "focusSubtitlePreviewFromTimeline",
        "focusTitlePreviewFromTimeline",
        'id="play-btn" class="tool-button" type="button" aria-pressed="false"',
        "updatePlaybackButton",
        "togglePlayback",
        "stepPlayback",
        "ensureTimelinePreview",
        "startTimelinePreviewPlayback",
        "startLiveTimelinePlayback",
        "stopLiveTimelinePlayback",
        "stopTimelinePreviewPlayback",
        "timeline-preview-video",
        "full-transcript",
        'id="sentence-panel"',
        'sentencePanel.classList.toggle("full-mode"',
        "beginTextHistory",
        "updateTranscriptHighlight",
        "reanalyze-subtitles",
        "cover-preview-image",
        "coverPreviewUrl",
        "function sentenceAtTime(time)",
        "clipStart(sentence)",
        "clipEnd(sentence)",
        'event.key.toLowerCase() === "j"',
        'event.key.toLowerCase() === "k"',
        'event.key.toLowerCase() === "l"',
        'id="play-selection-btn"',
        'aria-pressed="false"',
        "setPreviewClipActive",
        "cancelPreviewClip",
        "previewSelectedClip",
        "stopPreviewClipIfNeeded",
        'id="clip-jump-start-btn"',
        'id="clip-jump-end-btn"',
        "jumpToSelectedClipEdge",
        'id="clip-set-start-btn"',
        'id="clip-set-end-btn"',
        "setSelectedClipEdgeToPlayhead",
        'event.key.toLowerCase() === "i"',
        'event.key.toLowerCase() === "o"',
        'id="title-preview" class="edit-title-preview" contenteditable="true"',
        "const isEditing = document.activeElement === element && element.isContentEditable",
        "document.activeElement !== subtitlePreview",
        "updateSelectedTitleText",
        'titlePreview.addEventListener("input"',
        'project.preview_source !== "clean-no-subtitles"',
        "当前任务没有无字幕精修源",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in html]
    if missing:
        print(f"Edit page {job_id}/{item_id} missing required timeline UI fragment(s): {', '.join(missing)}")
        return 1

    scripts = [match.group(1) for match in SCRIPT_RE.finditer(html)]
    if not scripts:
        print(f"No inline script found in edit page {job_id}/{item_id}.")
        return 1

    failed = False
    with tempfile.TemporaryDirectory(prefix="ai-cutting-edit-js-") as tmp:
        tmp_dir = Path(tmp)
        for index, script in enumerate(scripts, start=1):
            script_path = tmp_dir / f"edit-page-{index}.js"
            script_path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["node", "--check", str(script_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            print(f"edit page JS {job_id}/{item_id} script {index}: node --check {result.returncode}")
            if result.returncode:
                failed = True
                if result.stdout:
                    print(result.stdout.strip())
                if result.stderr:
                    print(result.stderr.strip())

    if failed:
        print("Edit page JS check failed.")
        return 1
    print("Edit page JS check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
