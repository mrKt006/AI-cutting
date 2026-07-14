from __future__ import annotations

import json
import os
import sys
import warnings
import tempfile
from pathlib import Path
from unittest.mock import patch

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
    index_required_fragments = ["FFmpeg", "火山", "开始处理", "内容标题", "封面标题", "逐字稿"]
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

    transcript_markdown = """# 测试文档

## 内容标题
内容标题测试

## 封面标题
第一行
第二行

## 逐字稿
我们在广西做AI获客系统。

## 备注
这里不参与匹配。
"""
    transcript_response = client.post(
        "/api/transcripts/parse",
        files={"file": ("script.md", transcript_markdown.encode("utf-8"), "text/markdown")},
    )
    transcript_data = transcript_response.json()
    if (
        transcript_response.status_code != 200
        or transcript_data.get("content_title") != "内容标题测试"
        or transcript_data.get("cover_title") != "第一行\n第二行"
        or transcript_data.get("transcript_length") != len("我们在广西做AI获客系统。")
    ):
        print(f"Transcript preview API check failed: {transcript_response.status_code} {transcript_response.text}")
        return 1
    invalid_transcript = client.post(
        "/api/transcripts/parse",
        files={"file": ("script.md", "# 只有标题".encode("utf-8"), "text/markdown")},
    )
    if invalid_transcript.status_code != 400 or "有效逐字稿" not in invalid_transcript.text:
        print(f"Invalid transcript boundary check failed: {invalid_transcript.status_code} {invalid_transcript.text}")
        return 1

    with tempfile.TemporaryDirectory(prefix="create-job-check-", dir=ROOT) as tmp:
        temporary_jobs = Path(tmp) / "jobs"
        with (
            patch("web.app.JOBS_DIR", temporary_jobs),
            patch("web.app._runtime_status", return_value={"ffmpeg_ready": True}),
            patch(
                "web.app._load_settings",
                return_value={
                    "volc_app_id": "test-app",
                    "volc_access_token": "test-token",
                    "llm_enabled": False,
                },
            ),
            patch("web.app._run_job", return_value=None),
        ):
            create_response = client.post(
                "/jobs",
                data={"transcript_indices": "0", "style_preset_id": "default-white"},
                files=[
                    ("video", ("video.mp4", b"fake-video", "video/mp4")),
                    ("transcript_files", ("script.md", transcript_markdown.encode("utf-8"), "text/markdown")),
                ],
                follow_redirects=False,
            )
        if create_response.status_code != 303:
            print(f"Transcript job creation failed: {create_response.status_code} {create_response.text}")
            return 1
        created_jobs = list(temporary_jobs.glob("*/job.json"))
        if len(created_jobs) != 1:
            print("Transcript job creation did not persist exactly one job.")
            return 1
        created_job = json.loads(created_jobs[0].read_text(encoding="utf-8"))
        created_item = created_job["params"]["items"][0]
        if (
            created_item.get("content_title") != "内容标题测试"
            or created_item.get("cover_title") != "第一行\n第二行"
            or created_item.get("transcript_source") != "逐字稿"
            or not Path(created_item.get("transcript_path", "")).is_file()
        ):
            print(f"Transcript job fields are incorrect: {created_item}")
            return 1

        pause_job_dir = temporary_jobs / "pause-test"
        pause_job_dir.mkdir(parents=True)
        pause_job = {
            "id": "pause-test",
            "status": "running",
            "stage": "running",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "log": [],
            "params": {
                "title": "暂停测试",
                "items": [{"id": "001", "status": "running", "outputs": {}, "error": None}],
            },
        }
        (pause_job_dir / "job.json").write_text(json.dumps(pause_job, ensure_ascii=False), encoding="utf-8")
        with patch("web.app.JOBS_DIR", temporary_jobs):
            pause_response = client.post("/jobs/pause-test/pause", follow_redirects=False)
        paused_request = json.loads((pause_job_dir / "job.json").read_text(encoding="utf-8"))
        control = json.loads((pause_job_dir / "control.json").read_text(encoding="utf-8"))
        if pause_response.status_code != 303 or paused_request.get("status") != "pausing" or not control.get("pause_requested"):
            print("Pause request endpoint check failed.")
            return 1

        paused_request["status"] = "paused"
        paused_request["stage"] = "paused"
        paused_request["params"]["items"][0]["status"] = "paused"
        (pause_job_dir / "job.json").write_text(json.dumps(paused_request, ensure_ascii=False), encoding="utf-8")
        with (
            patch("web.app.JOBS_DIR", temporary_jobs),
            patch("web.app._runtime_status", return_value={"ffmpeg_ready": True}),
            patch(
                "web.app._load_settings",
                return_value={"volc_app_id": "test-app", "volc_access_token": "test-token", "llm_api_key": ""},
            ),
            patch("web.app._run_job", return_value=None),
        ):
            resume_response = client.post("/jobs/pause-test/resume", follow_redirects=False)
        resumed = json.loads((pause_job_dir / "job.json").read_text(encoding="utf-8"))
        if resume_response.status_code != 303 or resumed.get("status") != "queued" or resumed["params"]["items"][0]["status"] != "queued":
            print("Resume endpoint check failed.")
            return 1
        resumed["status"] = "paused"
        resumed["stage"] = "paused"
        resumed["params"]["items"][0]["status"] = "paused"
        (pause_job_dir / "job.json").write_text(json.dumps(resumed, ensure_ascii=False), encoding="utf-8")
        with patch("web.app.JOBS_DIR", temporary_jobs):
            cancel_response = client.post("/jobs/pause-test/cancel", follow_redirects=False)
        cancelled = json.loads((pause_job_dir / "job.json").read_text(encoding="utf-8"))
        if cancel_response.status_code != 303 or cancelled.get("status") != "cancelled" or cancelled["params"]["items"][0]["status"] != "cancelled":
            print("Cancel endpoint check failed.")
            return 1
    jobs_response = client.get("/jobs")
    jobs_required_fragments = ["全部任务", "标题或任务 ID", "全部状态"]
    missing = [fragment for fragment in jobs_required_fragments if fragment not in jobs_response.text]
    if jobs_response.status_code != 200 or missing:
        print(f"Jobs page check failed; missing: {', '.join(missing)}")
        return 1
    styles_response = client.get("/style-presets")
    style_fragments = ["内容标题", "在成片中显示内容标题", "仅开头显示", "精确预览失败，点击重试", "确定删除这个预设吗"]
    missing = [fragment for fragment in style_fragments if fragment not in styles_response.text]
    if styles_response.status_code != 200 or missing:
        print(f"Content title preset check failed; missing: {', '.join(missing)}")
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
