from __future__ import annotations

import json
import os
import sys
import warnings
import tempfile
from pathlib import Path

warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*")

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.app import _write_training_feedback, app  # noqa: E402


def _find_job_id() -> str | None:
    jobs_dir = ROOT / "jobs"
    if not jobs_dir.exists():
        return None
    for job_json in sorted(jobs_dir.glob("*/job.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            job = json.loads(job_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if job.get("status") == "done":
            return job_json.parent.name
    for job_json in sorted(jobs_dir.glob("*/job.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        return job_json.parent.name
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    job_id = _find_job_id()
    if not job_id:
        print("No jobs found; skipped web error boundary check.")
        return 0

    client = TestClient(app, raise_server_exceptions=False)
    original_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = ""
        from web.app import _runtime_status

        runtime = _runtime_status({})
        if runtime["ffmpeg_ready"] and not os.environ.get("PATH"):
            print("Runtime status check failed: ffmpeg_ready without PATH repair.")
            return 1
    finally:
        os.environ["PATH"] = original_path

    from web.app import _sanitize_title_clips

    legacy_title = {"cover_text": "封面标题", "video_text": "封面标题", "show_video_title": True}
    migrated_titles = _sanitize_title_clips(
        [{"id": "t001", "start": 0, "end": 3, "text": "封面标题", "enabled": True, "use_for_cover": True}],
        {},
        10.0,
        legacy_title,
    )
    if migrated_titles[0]["text"] or migrated_titles[0]["enabled"] or migrated_titles[0]["use_for_cover"]:
        print("Legacy cover/video title separation check failed.")
        return 1

    with tempfile.TemporaryDirectory(prefix="feedback-check-", dir=ROOT) as tmp:
        job_dir = Path(tmp) / "job-test"
        work_dir = job_dir / "work" / "001"
        work_dir.mkdir(parents=True)
        (work_dir / "raw_transcript_segments.json").write_text(
            json.dumps([{"text": "我我们开始", "tokens": [{"id": "t1", "text": "我"}]}], ensure_ascii=False),
            encoding="utf-8",
        )
        (work_dir / "transcript_analysis.json").write_text(
            json.dumps({"status": "ok", "auto_edit_mode": "standard", "delete_ranges": [{"token_ids": ["t1"]}]}),
            encoding="utf-8",
        )
        (work_dir / "auto_edit_plan.json").write_text(
            json.dumps({"keep_segments": [{"start": 0.2, "end": 2.0}]}), encoding="utf-8"
        )
        initial = {"item_id": "001", "sentences": [{"id": "s1", "text": "开始", "start": 0, "end": 1}]}
        final = {"item_id": "001", "sentences": [{"id": "s1", "text": "现在开始", "start": 0, "end": 1}]}
        _write_training_feedback(job_dir, initial, final)
        feedback_text = (work_dir / "training_feedback.json").read_text(encoding="utf-8")
        feedback = json.loads(feedback_text)
        if feedback["user_changes"]["text_edits"][0]["after"] != "现在开始" or "api_key" in feedback_text.lower():
            print("Training feedback persistence check failed.")
            return 1
        if not feedback.get("auto_edit_plan", {}).get("keep_segments"):
            print("Training feedback edit-plan check failed.")
            return 1

    index_response = client.get("/")
    print(f"index page: {index_response.status_code}")
    index_required_fragments = ["FFmpeg", "火山", "开始处理"]
    missing = [fragment for fragment in index_required_fragments if fragment not in index_response.text]
    if index_response.status_code != 200 or missing:
        print(f"Index page environment status check failed; missing: {', '.join(missing)}")
        return 1
    settings_response = client.get("/settings")
    print(f"settings page: {settings_response.status_code}")
    settings_required_fragments = ["运行环境", "FFmpeg / FFprobe", "火山引擎凭证"]
    missing = [fragment for fragment in settings_required_fragments if fragment not in settings_response.text]
    if settings_response.status_code != 200 or missing:
        print(f"Settings page runtime status check failed; missing: {', '.join(missing)}")
        return 1

    cases = [
        ("empty save body", "post", f"/api/jobs/{job_id}/edit-project?item=001", {}, 400),
        (
            "bad save json",
            "post",
            f"/api/jobs/{job_id}/edit-project?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
        (
            "bad render json",
            "post",
            f"/jobs/{job_id}/render-edited?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
        (
            "bad timeline preview json",
            "post",
            f"/api/jobs/{job_id}/edit-preview?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
        (
            "bad subtitle analysis json",
            "post",
            f"/api/jobs/{job_id}/reanalyze-subtitles?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
        (
            "bad subtitle reflow json",
            "post",
            f"/api/jobs/{job_id}/reflow-subtitles?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
        (
            "bad cover preview json",
            "post",
            f"/api/jobs/{job_id}/cover-preview?item=001",
            {"content": "{bad", "headers": {"content-type": "application/json"}},
            400,
        ),
    ]

    failed = False
    for label, method, path, kwargs, expected in cases:
        response = getattr(client, method)(path, **kwargs)
        print(f"{label}: {response.status_code} {response.text[:160].replace(chr(10), ' ')}")
        if response.status_code != expected:
            failed = True

    if failed:
        print("Web error boundary check failed.")
        return 1
    print("Web error boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
