from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*")

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web.app import app  # noqa: E402


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

    index_response = client.get("/")
    print(f"index page: {index_response.status_code}")
    index_required_fragments = ["FFmpeg", "火山", "开始处理"]
    missing = [fragment for fragment in index_required_fragments if fragment not in index_response.text]
    if index_response.status_code != 200 or missing:
        print(f"Index page environment status check failed; missing: {', '.join(missing)}")
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
