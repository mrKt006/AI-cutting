from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import traceback
import uuid
import wave
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cut_silence import Segment, cut_video  # noqa: E402
from ffmpeg_utils import ffmpeg_filter_path, media_duration, run as ffmpeg_run, video_size  # noqa: E402
from make_cover import make_cover  # noqa: E402
from make_subtitle import SubtitleCue  # noqa: E402
from render_video import burn_subtitles  # noqa: E402
from safe_json import read_json_file  # noqa: E402
from style_presets import (  # noqa: E402
    DEFAULT_STYLE_PRESETS,
    get_style_preset,
    load_style_presets,
    save_style_presets,
    subtitle_override,
    subtitle_to_ass_style,
)


ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "jobs"
PYTHON = sys.executable
JOB_SECRETS: dict[str, dict[str, str]] = {}
SETTINGS_PATH = ROOT / "web" / "settings.local.json"
STYLE_PRESETS_PATH = ROOT / "web" / "style_presets.local.json"
PREVIEW_DIR = ROOT / "web" / "static" / "style_previews"

PRESETS = {
    "natural": {"label": "自然", "noise": "-30dB", "min_silence": 0.45, "padding": 0.12},
    "standard": {"label": "标准", "noise": "-28dB", "min_silence": 0.35, "padding": 0.10},
    "compact": {"label": "紧凑", "noise": "-26dB", "min_silence": 0.30, "padding": 0.08},
    "aggressive": {"label": "激进", "noise": "-24dB", "min_silence": 0.25, "padding": 0.06},
}

STAGES = {
    "queued": "等待中",
    "running": "处理中",
    "done": "完成",
    "failed": "失败",
}

app = FastAPI(title="AI-cutting Web")
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "web" / "static")), name="static")


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    match = re.search(r"/jobs/([^/?#]+)", request.url.path)
    if match:
        try:
            _write_web_traceback(_job_path(match.group(1)), "web-unhandled", exc)
        except Exception:
            pass
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，诊断信息已记录"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    settings = _load_settings()
    style_presets = load_style_presets(STYLE_PRESETS_PATH)
    env = {
        "volc_ready": bool(
            (os.environ.get("VOLC_APP_ID") and os.environ.get("VOLC_ACCESS_TOKEN"))
            or (settings.get("volc_app_id") and settings.get("volc_access_token"))
        ),
    }
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "presets": PRESETS,
            "style_presets": style_presets,
            "jobs": _recent_jobs(),
            "env": env,
            "settings": _public_settings(settings),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    settings = _load_settings()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "settings": _public_settings(settings),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/settings")
async def save_settings(
    volc_app_id: str = Form(""),
    volc_access_token: str = Form(""),
    subtitle_delay: float = Form(0.0),
    detect_disfluency: str | None = Form(None),
) -> RedirectResponse:
    existing = _load_settings()
    token = volc_access_token.strip() or existing.get("volc_access_token", "")
    _save_settings(
        {
            "volc_app_id": volc_app_id.strip(),
            "volc_access_token": token,
            "subtitle_delay": subtitle_delay,
            "detect_disfluency": bool(detect_disfluency),
        }
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/style-presets", response_class=HTMLResponse)
def style_presets_page(request: Request) -> HTMLResponse:
    presets = load_style_presets(STYLE_PRESETS_PATH)
    selected_id = request.query_params.get("preset") or presets[0]["id"]
    selected = get_style_preset(selected_id, STYLE_PRESETS_PATH)
    preview = request.query_params.get("preview") or ""
    active_style = request.query_params.get("active") or "subtitle"
    if active_style not in {"subtitle", "cover"}:
        active_style = "subtitle"
    preview_text = request.query_params.get("text") or "默认文本"
    preview_aspect = _preview_aspect(request.query_params.get("aspect"))
    return templates.TemplateResponse(
        request=request,
        name="style_presets.html",
        context={
            "request": request,
            "presets": presets,
            "selected": selected,
            "preview": preview,
            "active_style": active_style,
            "preview_text": preview_text,
            "preview_aspect": preview_aspect,
            "saved": request.query_params.get("saved") == "1",
        },
    )
@app.post("/style-presets")
async def save_style_preset(request: Request) -> RedirectResponse:
    form = await request.form()
    action = str(form.get("action") or "save")
    presets = load_style_presets(STYLE_PRESETS_PATH)
    preset_id = _slug(str(form.get("preset_id") or form.get("name") or "style"))
    preview_path = str(form.get("preview_path") or "")
    active_style = str(form.get("active_text_style") or "subtitle")
    preview_text = str(form.get("preview_text") or "默认文本")
    preview_aspect = _preview_aspect(str(form.get("preview_aspect") or "9:16"))

    if action == "create":
        new_id = _unique_preset_id(presets, "new-style")
        new_preset = get_style_preset("default-white", STYLE_PRESETS_PATH)
        new_preset["id"] = new_id
        new_preset["name"] = "新建预设"
        presets.append(new_preset)
        save_style_presets(presets, STYLE_PRESETS_PATH)
        return RedirectResponse(
            _style_presets_url(new_id, saved=True, preview_path=preview_path, active="subtitle", text=preview_text, aspect=preview_aspect),
            status_code=303,
        )

    if action == "delete":
        if len(presets) <= 1:
            return RedirectResponse(_style_presets_url(preset_id, preview_path=preview_path, active=active_style, text=preview_text, aspect=preview_aspect), status_code=303)
        presets = [item for item in presets if item["id"] != preset_id]
        save_style_presets(presets, STYLE_PRESETS_PATH)
        return RedirectResponse(_style_presets_url(presets[0]["id"], saved=True, preview_path=preview_path, active=active_style, text=preview_text, aspect=preview_aspect), status_code=303)

    if action == "duplicate":
        source = get_style_preset(preset_id, STYLE_PRESETS_PATH)
        source["id"] = _unique_preset_id(presets, f"{source['id']}-copy")
        source["name"] = f"{source['name']} 副本"
        presets.append(source)
        save_style_presets(presets, STYLE_PRESETS_PATH)
        return RedirectResponse(_style_presets_url(source["id"], saved=True, preview_path=preview_path, active=active_style, text=preview_text, aspect=preview_aspect), status_code=303)

    saved = _preset_from_form(form, preset_id)
    replaced = False
    for index, item in enumerate(presets):
        if item["id"] == preset_id:
            presets[index] = saved
            replaced = True
            break
    if not replaced:
        presets.append(saved)
    save_style_presets(presets, STYLE_PRESETS_PATH)
    return RedirectResponse(_style_presets_url(preset_id, saved=True, preview_path=preview_path, active=active_style, text=preview_text, aspect=preview_aspect), status_code=303)


@app.post("/style-presets/preview-frame")
async def preview_frame(
    video: UploadFile = File(...),
    preset: str = Form("default-white"),
    active: str = Form("subtitle"),
    text: str = Form("默认文本"),
    aspect: str = Form("9:16"),
) -> RedirectResponse:
    if not video or not video.filename:
        raise HTTPException(status_code=400, detail="请上传视频文件")
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:10]
    suffix = Path(video.filename).suffix.lower() or ".mp4"
    source = PREVIEW_DIR / f"{token}{suffix}"
    frame = PREVIEW_DIR / f"{token}.jpg"
    with source.open("wb") as handle:
        shutil.copyfileobj(video.file, handle)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            "1",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(frame),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    source.unlink(missing_ok=True)
    return RedirectResponse(
        _style_presets_url(preset, preview_path=f"/static/style_previews/{frame.name}", active=active, text=text, aspect=_preview_aspect(aspect)),
        status_code=303,
    )


@app.get("/api/style-presets")
def api_style_presets() -> dict[str, Any]:
    return {"presets": load_style_presets(STYLE_PRESETS_PATH)}


@app.post("/api/style-presets/preview-ass")
async def api_preview_ass(preset_id: str = Form("default-white")) -> JSONResponse:
    preset = get_style_preset(preset_id, STYLE_PRESETS_PATH)
    style = preset["subtitle"]
    ass = "\n".join(
        [
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            subtitle_to_ass_style(style, width=1080, height=1920),
        ]
    )
    return JSONResponse({"preset_id": preset["id"], "ass": ass})


@app.post("/api/style-presets/render-preview")
async def api_render_preview(request: Request) -> JSONResponse:
    form = await request.form()
    preview_path = str(form.get("preview_path") or "")
    frame: Path | None = None
    if preview_path:
        if not preview_path.startswith("/static/style_previews/"):
            raise HTTPException(status_code=400, detail="预览帧路径无效")
        frame = (ROOT / "web" / preview_path.lstrip("/")).resolve()
        if not frame.exists() or not str(frame).startswith(str(PREVIEW_DIR.resolve())):
            raise HTTPException(status_code=404, detail="预览帧不存在")

    preset = _preset_from_form(form, str(form.get("preset_id") or "preview"))
    active_style = str(form.get("active_text_style") or "subtitle")
    render_style = preset["cover_title"] if active_style == "cover" else preset["subtitle"]
    text = str(form.get("preview_text") or "默认文本")
    preview_aspect = _preview_aspect(str(form.get("preview_aspect") or "9:16"))
    width, height = _preview_dimensions(preview_aspect)
    token = uuid.uuid4().hex[:10]
    ass_path = PREVIEW_DIR / f"accurate_{token}.ass"
    output = PREVIEW_DIR / f"accurate_{token}.jpg"
    ass_path.write_text(_preview_ass(text, render_style, width, height), encoding="utf-8")
    if frame:
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(frame),
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"subtitles='{ffmpeg_filter_path(ass_path)}'"
            ),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0xf0fdfa:s={width}x{height}:r=1",
            "-vf",
            f"subtitles='{ffmpeg_filter_path(ass_path)}'",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
    subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return JSONResponse({"image": f"/static/style_previews/{output.name}", "aspect": preview_aspect})


@app.post("/jobs")
async def create_job(
    video: list[UploadFile] = File(...),
    title: str = Form(""),
    item_titles: list[str] = Form([]),
    preset: str = Form("standard"),
    style_preset_id: str = Form("default-white"),
    export_subtitles: str | None = Form(None),
    export_asr_json: str | None = Form(None),
    export_report: str | None = Form(None),
) -> RedirectResponse:
    settings = _load_settings()
    try:
        style_preset = get_style_preset(style_preset_id, STYLE_PRESETS_PATH)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if preset not in PRESETS:
        raise HTTPException(status_code=400, detail="Unsupported preset")
    has_env_creds = bool(os.environ.get("VOLC_APP_ID") and os.environ.get("VOLC_ACCESS_TOKEN"))
    has_saved_creds = bool(settings.get("volc_app_id") and settings.get("volc_access_token"))
    if not has_env_creds and not has_saved_creds:
        raise HTTPException(status_code=400, detail="火山引擎模式需要先在设置页填写 APP ID 和 Access Token")
    videos = [item for item in video if item and item.filename]
    if not videos:
        raise HTTPException(status_code=400, detail="请至少上传一个视频")

    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    base_title = title.strip()
    per_item_titles = [item.strip() for item in item_titles]
    today = datetime.now().strftime("%Y%m%d")
    items = []
    for index, upload in enumerate(videos, start=1):
        item_id = f"{index:03d}"
        item_input_dir = input_dir / item_id
        item_output_dir = output_dir / item_id
        item_input_dir.mkdir(parents=True, exist_ok=True)
        item_output_dir.mkdir(parents=True, exist_ok=True)
        item_title = per_item_titles[index - 1] if index - 1 < len(per_item_titles) else ""
        video_title = item_title or base_title or Path(upload.filename or f"video-{index}").stem
        if not item_title and base_title and len(videos) > 1:
            video_title = f"{base_title}-{index:02d}"
        output_basename = _safe_output_basename(f"{video_title}-{today}")
        video_path = item_input_dir / _safe_video_name(upload.filename or f"video-{index}.mp4", index=index)
        with video_path.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise HTTPException(status_code=400, detail=f"{upload.filename} 为空或保存失败")
        title_path = item_input_dir / "title.txt"
        title_path.write_text(video_title, encoding="utf-8")
        items.append(
            {
                "id": item_id,
                "source_name": upload.filename or video_path.name,
                "title": video_title,
                "video": str(video_path),
                "title_path": str(title_path),
                "output_dir": str(item_output_dir),
                "output_basename": output_basename,
                "status": "queued",
                "outputs": {},
                "error": None,
            }
        )

    params = {
        "subtitle_source": "volcengine",
        "preset": preset,
        "style_preset_id": style_preset["id"],
        "style_preset_name": style_preset["name"],
        "subtitle_delay": float(settings.get("subtitle_delay", 0.0)),
        "detect_disfluency": bool(settings.get("detect_disfluency", False)),
        "export_subtitles": bool(export_subtitles),
        "export_asr_json": bool(export_asr_json),
        "export_report": bool(export_report),
        "title": base_title or (items[0]["title"] if len(items) == 1 else f"批量任务 {len(items)} 个视频"),
        "output_dir": str(output_dir),
        "items": items,
        "volc_app_id_source": "settings" if settings.get("volc_app_id") else "environment",
    }
    _write_job(
        job_dir,
        {
            "id": job_id,
            "status": "queued",
            "stage": "queued",
            "created_at": _now(),
            "updated_at": _now(),
            "params": params,
            "outputs": {},
            "error": None,
            "log": [],
        },
    )
    if settings.get("volc_app_id") or settings.get("volc_access_token"):
        JOB_SECRETS[job_id] = {
            "VOLC_APP_ID": settings.get("volc_app_id", ""),
            "VOLC_ACCESS_TOKEN": settings.get("volc_access_token", ""),
        }

    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str) -> HTMLResponse:
    job = _read_job(job_id)
    return templates.TemplateResponse(
        request=request,
        name="job.html",
        context={
            "request": request,
            "job": job,
            "files": _output_files(job_id),
            "stage_label": STAGES.get(job.get("status"), job.get("status")),
        },
    )


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = _read_job(job_id)
    job["files"] = _output_files(job_id)
    return job


@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, job_id: str, item: str = "001") -> HTMLResponse:
    job_dir = _job_path(job_id)
    job = _read_job(job_id)
    project = _load_or_create_edit_project(job_dir, job, item)
    video_path = Path(project["current_video"])
    style_preset = get_style_preset(project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    editor_assets = _safe_editor_assets(job_dir, project)
    project = dict(project)
    project["output_files"] = [file for file in _output_files(job_id) if file["name"].startswith(f"edited/{project['item_id']}/")]
    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context={
            "request": request,
            "job": job,
            "item_id": project["item_id"],
            "project": project,
            "project_json": json.dumps(project, ensure_ascii=False),
            "video_url": _media_url_for_path(job_dir, video_path),
            "video_info": _editor_video_info(video_path),
            "editor_assets": editor_assets,
            "subtitle_style": style_preset.get("subtitle") or {},
            "title_style": style_preset.get("cover_title") or {},
        },
    )


@app.get("/api/jobs/{job_id}/edit-project")
def api_edit_project(job_id: str, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    job = _read_job(job_id)
    return _load_or_create_edit_project(job_dir, job, item)


@app.post("/api/jobs/{job_id}/edit-project")
async def api_save_edit_project(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    try:
        existing = _load_or_create_edit_project(job_dir, _read_job(job_id), item)
        payload = await _read_json_object(request, source="保存精修项目", required=True)
        project = _sanitize_edit_project({**existing, **payload}, existing)
        _write_edit_project(job_dir, project)
        return {"ok": True, "project": project}
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "save-edit-project", exc)
        raise HTTPException(status_code=500, detail="保存精修项目失败，诊断信息已写入任务目录") from exc


@app.post("/jobs/{job_id}/render-edited")
async def render_edited(job_id: str, request: Request, item: str = "001") -> JSONResponse:
    job_dir = _job_path(job_id)
    try:
        job = _read_job(job_id)
        project = _load_or_create_edit_project(job_dir, job, item)
        payload = await _read_json_object(request, source="导出精修项目", required=False)
        if payload:
            project = _sanitize_edit_project({**project, **payload}, project)
            _write_edit_project(job_dir, project)
        result = _render_edit_project(job_dir, job, project)
        return JSONResponse({"ok": True, "outputs": result, "files": _output_files(job_id)})
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "render-edited", exc)
        raise HTTPException(status_code=500, detail="导出精修版失败，诊断信息已写入任务目录") from exc


@app.get("/jobs/{job_id}/download/{name:path}")
def download(job_id: str, name: str) -> FileResponse:
    files = {item["name"]: item for item in _output_files(job_id)}
    if name not in files:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(files[name]["path"], filename=files[name]["download_name"])


@app.get("/jobs/{job_id}/editor-media/{name:path}")
def editor_media(job_id: str, name: str) -> FileResponse:
    job_dir = _job_path(job_id)
    work_dir = (job_dir / "work").resolve()
    path = (work_dir / name).resolve()
    if not str(path).startswith(str(work_dir)) or not path.is_file():
        raise HTTPException(status_code=404, detail="Editor media not found")
    if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=404, detail="Editor media not found")
    return FileResponse(path)


@app.get("/jobs/{job_id}/editor-assets/{item_id}/{name:path}")
def editor_assets(job_id: str, item_id: str, name: str) -> FileResponse:
    job_dir = _job_path(job_id)
    safe_item = _safe_path_segment(item_id) or "001"
    asset_dir = (job_dir / "work" / "editor_assets" / safe_item).resolve()
    path = (asset_dir / name).resolve()
    if not str(path).startswith(str(asset_dir)) or not path.is_file():
        raise HTTPException(status_code=404, detail="Editor asset not found")
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".json"}:
        raise HTTPException(status_code=404, detail="Editor asset not found")
    return FileResponse(path)


def _run_job(job_id: str) -> None:
    job_dir = JOBS_DIR / job_id
    job = _load_job(job_dir)
    params = job["params"]
    preset = PRESETS[params["preset"]]

    try:
        _update_job(job_dir, status="running", stage="running", message="开始处理")
        _append_log(job_dir, f"Python: {PYTHON}")
        _append_log(job_dir, f"工作目录: {ROOT}")
        _append_log(job_dir, "字幕源: 火山引擎")
        _append_log(job_dir, f"样式预设: {params.get('style_preset_id') or 'default-white'}")
        env = os.environ.copy()
        secrets = JOB_SECRETS.pop(job_id, {})
        for key, value in secrets.items():
            if value:
                env[key] = value

        items = params.get("items") or [
            {
                "id": "001",
                "title": params.get("title", "未命名视频"),
                "video": params.get("video"),
                "title_path": params.get("title_path"),
                "output_dir": params.get("output_dir"),
                "output_basename": _safe_output_basename(f"{params.get('title', '未命名视频')}-{datetime.now():%Y%m%d}"),
                "status": "queued",
                "outputs": {},
                "error": None,
            }
        ]
        _set_job_items(job_dir, items)
        for index, item in enumerate(items, start=1):
            item["status"] = "running"
            _set_job_items(job_dir, items)
            _append_log(job_dir, f"处理第 {index}/{len(items)} 个视频: {item['title']}")
            cmd = [
                PYTHON,
                str(ROOT / "src" / "main.py"),
                "--video",
                item["video"],
                "--title",
                item["title_path"],
                "--output-dir",
                item["output_dir"],
                "--output-basename",
                item["output_basename"],
                "--subtitle-source",
                "volcengine",
                f"--noise={preset['noise']}",
                "--min-silence",
                str(preset["min_silence"]),
                "--padding",
                str(preset["padding"]),
                "--subtitle-delay",
                str(params["subtitle_delay"]),
                "--style-preset",
                params.get("style_preset_id") or "default-white",
                "--style-presets-file",
                str(STYLE_PRESETS_PATH),
                "--editor-work-dir",
                str(job_dir / "work" / item["id"]),
            ]
            if params["detect_disfluency"]:
                cmd.append("--detect-disfluency")
            if params.get("export_subtitles"):
                cmd.append("--export-subtitles")
            if params.get("export_asr_json"):
                cmd.append("--export-asr-json")
            if params.get("export_report"):
                cmd.append("--export-report")
            _append_log(job_dir, " ".join(cmd))
            proc = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", capture_output=True, env=env)
            if proc.stdout:
                _append_log(job_dir, _mask_secrets(proc.stdout.strip(), secrets))
            if proc.stderr:
                masked_stderr = _mask_secrets(proc.stderr.strip(), secrets)
                _append_log(job_dir, masked_stderr)
                (job_dir / f"debug_traceback_{item['id']}.txt").write_text(masked_stderr, encoding="utf-8")
            if proc.returncode != 0:
                item["status"] = "failed"
                item["error"] = _short_process_error(proc, secrets)
                _set_job_items(job_dir, items)
                raise RuntimeError(f"{item['title']} 处理失败: {item['error']}")
            item_outputs = {}
            for path in Path(item["output_dir"]).glob("*"):
                if path.is_file():
                    item_outputs[path.name] = str(path)
            item["outputs"] = item_outputs
            item["status"] = "done"
            _set_job_items(job_dir, items)

        zip_path = _build_job_zip(job_dir, items)
        outputs = {"batch_zip": str(zip_path)}
        _update_job(job_dir, status="done", stage="done", message="处理完成，已生成 ZIP", outputs=outputs)
    except Exception as exc:
        _update_job(job_dir, status="failed", stage="failed", error=str(exc), message=f"失败: {exc}")


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_video_name(name: str, index: int = 1) -> str:
    suffix = Path(name).suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv"}:
        suffix = ".mp4"
    return f"video-{index:03d}" + suffix


def _safe_output_basename(value: str) -> str:
    forbidden = '<>:"/\\|?*'
    cleaned = "".join("-" if char in forbidden or ord(char) < 32 else char for char in value.strip())
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned[:80] or f"未命名视频-{datetime.now():%Y%m%d}"


def _set_job_items(job_dir: Path, items: list[dict[str, Any]]) -> None:
    job = _load_job(job_dir)
    job.setdefault("params", {})["items"] = items
    _write_job(job_dir, job)


def _build_job_zip(job_dir: Path, items: list[dict[str, Any]]) -> Path:
    zip_path = job_dir / "output" / "批量结果.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in items:
            output_dir = Path(item["output_dir"])
            folder = _safe_zip_folder(item.get("title") or item.get("id") or "video")
            for path in output_dir.glob("*"):
                if path.is_file():
                    archive.write(path, arcname=f"{folder}/{path.name}")
    return zip_path


def _safe_zip_folder(value: str) -> str:
    return _safe_output_basename(value).rstrip(".") or "video"


def _job_path(job_id: str) -> Path:
    path = (JOBS_DIR / job_id).resolve()
    if not str(path).startswith(str(JOBS_DIR.resolve())):
        raise HTTPException(status_code=404, detail="Job not found")
    return path


def _read_job(job_id: str) -> dict[str, Any]:
    job_dir = _job_path(job_id)
    if not (job_dir / "job.json").exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return _load_job(job_dir)


def _load_job(job_dir: Path) -> dict[str, Any]:
    return _load_json_file(job_dir / "job.json")


def _write_job(job_dir: Path, job: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    target = job_dir / "job.json"
    temp = job_dir / "job.tmp.json"
    temp.write_text(json.dumps(job, ensure_ascii=True, indent=2), encoding="utf-8")
    temp.replace(target)


async def _read_json_object(request: Request, *, source: str, required: bool = True) -> dict[str, Any]:
    body = await request.body()
    if not body or not body.strip():
        if required:
            raise HTTPException(status_code=400, detail=f"{source}请求体不能为空")
        return {}
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{source}请求体必须是 UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{source}JSON 格式无效：第 {exc.lineno} 行第 {exc.colno} 列",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{source}JSON 必须是对象")
    return payload


def _write_web_traceback(job_dir: Path, label: str, exc: BaseException) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "web-error"
    path = job_dir / f"debug_traceback_{safe_label}.txt"
    path.write_text(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        encoding="utf-8",
    )
    return path


def _update_job(job_dir: Path, **updates: Any) -> None:
    job = _load_job(job_dir)
    if "message" in updates:
        job.setdefault("log", []).append({"time": _now(), "message": updates.pop("message")})
    job.update(updates)
    _write_job(job_dir, job)


def _append_log(job_dir: Path, message: str) -> None:
    job = _load_job(job_dir)
    job.setdefault("log", []).append({"time": _now(), "message": message})
    _write_job(job_dir, job)


def _load_or_create_edit_project(job_dir: Path, job: dict[str, Any], item_id: str = "001") -> dict[str, Any]:
    item = _find_job_item(job, item_id)
    project_path = _edit_project_path(job_dir, item["id"])
    if project_path.exists():
        try:
            saved_project = _sanitize_edit_project(read_json_file(project_path), _blank_edit_project(job_dir, job, item))
            if saved_project.get("sentences"):
                return saved_project
        except RuntimeError:
            pass

    project = _blank_edit_project(job_dir, job, item)
    segments = _load_editor_segments(job_dir, item)
    if not segments:
        segments = _load_srt_segments(Path(item.get("output_dir", "")) / "subtitle.srt")
    if not segments:
        duration = project["duration"] or 0.0
        title = item.get("title") or job.get("params", {}).get("title") or "未命名视频"
        segments = [{"start": 0.0, "end": max(0.2, duration), "text": title}]

    sentences = [
        {
            "id": f"s{index:03d}",
            "start": round(float(segment["start"]), 3),
            "end": round(float(segment["end"]), 3),
            "clip_start": round(float(segment["start"]), 3),
            "clip_end": round(float(segment["end"]), 3),
            "timeline_order": index,
            "original_text": str(segment.get("text") or "").strip(),
            "text": str(segment.get("text") or "").strip(),
            "enabled": True,
            "remove_video": False,
            "edited": False,
        }
        for index, segment in enumerate(segments, start=1)
        if float(segment.get("end", 0)) > float(segment.get("start", 0))
    ]
    for index, sentence in enumerate(sentences):
        if index == 0:
            sentence["clip_start"] = 0.0
        if index + 1 < len(sentences):
            sentence["clip_end"] = max(sentence["clip_end"], sentences[index + 1]["start"])
        else:
            sentence["clip_end"] = max(sentence["clip_end"], project["duration"] or sentence["clip_end"])
    project["sentences"] = sentences
    _write_edit_project(job_dir, project)
    return project


def _blank_edit_project(job_dir: Path, job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    params = job.get("params", {})
    output_dir = Path(item.get("output_dir") or job_dir / "output")
    preview_video = _find_preview_video(output_dir)
    work_video = job_dir / "work" / item["id"] / "cut_no_subtitles.mp4"
    clean_source = work_video if work_video.exists() else None
    render_source = clean_source or preview_video
    duration = 0.0
    for candidate in (render_source, preview_video, Path(item.get("video") or "")):
        if candidate and candidate.exists():
            try:
                duration = media_duration(candidate)
                break
            except Exception:
                continue
    title = item.get("title") or params.get("title") or "未命名视频"
    return {
        "version": 3,
        "job_id": job.get("id"),
        "item_id": item["id"],
        "title": {"cover_text": title, "video_text": "", "show_video_title": False},
        "title_clips": [
            {
                "id": "t001",
                "start": 0.0,
                "end": min(max(duration, 0.2), 3.0),
                "text": title,
                "enabled": False,
                "use_for_cover": True,
            }
        ],
        "settings": {"subtitle_offset": 0.0},
        "style_preset_id": params.get("style_preset_id") or "default-white",
        "duration": duration,
        "current_video": str(render_source or ""),
        "render_source_video": str(render_source or preview_video or ""),
        "preview_source": "clean-no-subtitles" if clean_source else "burned-output-fallback",
        "preview_warning": "" if clean_source else "这个任务没有无字幕精修源，预览可能出现双字幕。请重新处理一次视频后再精修。",
        "sentences": [],
        "outputs": {},
        "created_at": _now(),
        "updated_at": _now(),
    }


def _sanitize_edit_project(project: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(existing)
    duration = max(0.0, float(project.get("duration") or existing.get("duration") or 0.0))
    cleaned["version"] = 3
    cleaned["duration"] = duration
    title = project.get("title") if isinstance(project.get("title"), dict) else {}
    old_title = existing.get("title") if isinstance(existing.get("title"), dict) else {}
    cleaned["title"] = {
        "cover_text": str(title.get("cover_text", old_title.get("cover_text", "")))[:800],
        "video_text": str(title.get("video_text", old_title.get("video_text", "")))[:800],
        "show_video_title": bool(title.get("show_video_title", old_title.get("show_video_title", False))),
    }
    settings = project.get("settings") if isinstance(project.get("settings"), dict) else {}
    old_settings = existing.get("settings") if isinstance(existing.get("settings"), dict) else {}
    cleaned["settings"] = {
        "subtitle_offset": _clamp_float(settings.get("subtitle_offset", old_settings.get("subtitle_offset", 0.0)), -5.0, 5.0),
    }
    cleaned["style_preset_id"] = str(project.get("style_preset_id") or existing.get("style_preset_id") or "default-white")
    old_sentences = {
        str(item.get("id")): item
        for item in existing.get("sentences", [])
        if isinstance(item, dict) and item.get("id")
    }
    incoming_sentences = project.get("sentences") if isinstance(project.get("sentences"), list) else existing.get("sentences", [])
    cleaned["sentences"] = _sanitize_sentences(incoming_sentences, old_sentences, duration)
    old_titles = {
        str(item.get("id")): item
        for item in existing.get("title_clips", [])
        if isinstance(item, dict) and item.get("id")
    }
    incoming_titles = project.get("title_clips") if isinstance(project.get("title_clips"), list) else existing.get("title_clips", [])
    cleaned["title_clips"] = _sanitize_title_clips(incoming_titles, old_titles, duration, cleaned["title"])
    if isinstance(project.get("outputs"), dict):
        cleaned["outputs"] = project["outputs"]
    cleaned["updated_at"] = _now()
    return cleaned


def _sanitize_sentences(items: list[Any], old_by_id: dict[str, dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    needs_clip_init = False
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        sentence_id = _safe_clip_id(str(item.get("id") or f"s{index:03d}"), "s", index, used_ids)
        original = old_by_id.get(sentence_id, {})
        synthetic_gap = bool(item.get("synthetic_gap", original.get("synthetic_gap", False))) and bool(item.get("gap", original.get("gap", False)))
        max_clip_end = 3600.0 if synthetic_gap else duration
        start = _clamp_float(item.get("start", original.get("start", 0.0)), 0.0, max_clip_end)
        end = _clamp_float(item.get("end", original.get("end", min(max_clip_end, start + 0.2))), 0.0, max_clip_end)
        if end <= start:
            end = min(max_clip_end, start + 0.2)
        if "clip_start" not in item and "clip_start" not in original:
            needs_clip_init = True
        if "clip_end" not in item and "clip_end" not in original:
            needs_clip_init = True
        clip_start = _clamp_float(item.get("clip_start", original.get("clip_start", start)), 0.0, max_clip_end)
        clip_end = _clamp_float(item.get("clip_end", original.get("clip_end", end)), 0.0, max_clip_end)
        if clip_end <= clip_start:
            clip_end = min(max_clip_end, clip_start + 0.05)
        text = str(item.get("text", original.get("text", ""))).strip()[:1000]
        original_text = str(item.get("original_text", original.get("original_text", text))).strip()[:1000]
        cleaned.append(
            {
                "id": sentence_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "clip_start": round(clip_start, 3),
                "clip_end": round(clip_end, 3),
                "timeline_order": _clamp_float(item.get("timeline_order", original.get("timeline_order", index)), 0.0, 100000.0),
                "original_text": original_text,
                "text": text,
                "enabled": bool(item.get("enabled", original.get("enabled", True))),
                "remove_video": bool(item.get("remove_video", original.get("remove_video", False))),
                "gap": bool(item.get("gap", original.get("gap", False))),
                "synthetic_gap": synthetic_gap,
                "edited": bool(item.get("edited", original.get("edited", False))) or text != original_text,
            }
        )
    cleaned.sort(key=lambda item: (item["timeline_order"], item["start"], item["end"], item["id"]))
    for index, item in enumerate(cleaned, start=1):
        item["timeline_order"] = index
    if needs_clip_init:
        by_source_time = sorted(cleaned, key=lambda item: (item["start"], item["end"], item["id"]))
        for index, item in enumerate(by_source_time):
            if index == 0:
                item["clip_start"] = 0.0
            if index + 1 < len(by_source_time):
                item["clip_end"] = round(max(float(item["clip_end"]), float(by_source_time[index + 1]["start"])), 3)
            else:
                item["clip_end"] = round(max(float(item["clip_end"]), duration), 3)
    return cleaned


def _sanitize_title_clips(
    items: list[Any],
    old_by_id: dict[str, dict[str, Any]],
    duration: float,
    title: dict[str, Any],
) -> list[dict[str, Any]]:
    if not items:
        items = [
            {
                "id": "t001",
                "start": 0.0,
                "end": min(max(duration, 0.2), 3.0),
                "text": title.get("video_text") or title.get("cover_text") or "",
                "enabled": bool(title.get("show_video_title")),
                "use_for_cover": True,
            }
        ]
    cleaned: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        clip_id = _safe_clip_id(str(item.get("id") or f"t{index:03d}"), "t", index, used_ids)
        original = old_by_id.get(clip_id, {})
        start = _clamp_float(item.get("start", original.get("start", 0.0)), 0.0, duration)
        end = _clamp_float(item.get("end", original.get("end", min(duration, start + 3.0))), 0.0, duration)
        if end <= start:
            end = min(duration, start + 0.2)
        cleaned.append(
            {
                "id": clip_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": str(item.get("text", original.get("text", title.get("cover_text", ""))))[:800],
                "enabled": bool(item.get("enabled", original.get("enabled", False))),
                "use_for_cover": bool(item.get("use_for_cover", original.get("use_for_cover", False))),
            }
        )
    cleaned.sort(key=lambda item: (item["start"], item["end"], item["id"]))
    return cleaned


def _safe_clip_id(value: str, prefix: str, index: int, used_ids: set[str]) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or f"{prefix}{index:03d}"
    if not value.startswith(prefix):
        value = f"{prefix}-{value}"
    base = value[:40]
    candidate = base
    offset = 2
    while candidate in used_ids:
        candidate = f"{base}-{offset}"
        offset += 1
    used_ids.add(candidate)
    return candidate


def _clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(maximum, number))


def _write_edit_project(job_dir: Path, project: dict[str, Any]) -> None:
    path = _edit_project_path(job_dir, project.get("item_id") or "001")
    path.write_text(json.dumps(project, ensure_ascii=True, indent=2), encoding="utf-8")


def _render_edit_project(job_dir: Path, job: dict[str, Any], project: dict[str, Any]) -> list[dict[str, str]]:
    item = _find_job_item(job, project.get("item_id") or "001")
    source = Path(project.get("render_source_video") or project.get("current_video") or "")
    if not source.exists():
        raise HTTPException(status_code=400, detail="找不到可精修的视频源，请重新跑一次任务")
    if not _is_clean_editor_source(job_dir, source):
        raise HTTPException(status_code=400, detail="当前任务没有无字幕精修源，不能导出精修版。请重新处理一次视频后再进入精修台。")
    edited_dir = job_dir / "output" / "edited" / item["id"]
    edited_dir.mkdir(parents=True, exist_ok=True)
    duration = float(project.get("duration") or media_duration(source))
    timeline_clips = _timeline_clips(project, duration)
    no_subtitle = edited_dir / "精修无字幕.mp4"
    _render_timeline_video(source, no_subtitle, timeline_clips)

    preset = get_style_preset(project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    width, height = video_size(no_subtitle)
    edited_duration = media_duration(no_subtitle)
    cues = _timeline_subtitle_cues(project, timeline_clips, edited_duration)
    ass_path = edited_dir / "精修字幕.ass"
    title_cues = _timeline_title_cues(project, edited_duration)
    _write_edit_ass(cues, title_cues, ass_path, width=width, height=height, preset=preset)
    video_title = project.get("title", {}).get("cover_text") or item.get("title") or "精修视频"
    output_video = edited_dir / f"{_safe_output_basename(video_title)}-精修.mp4"
    burn_subtitles(no_subtitle, ass_path, output_video)
    cover_path = edited_dir / f"{_safe_output_basename(video_title)}-精修封面.jpg"
    make_cover(no_subtitle, video_title, cover_path, preset.get("cover_title"))
    plan_path = edited_dir / "edit_plan.json"
    manifest_path = edited_dir / "render_manifest.json"
    plan = _build_edit_plan(project, timeline_clips, cues, title_cues, edited_duration)
    plan_path.write_text(json.dumps(plan, ensure_ascii=True, indent=2), encoding="utf-8")
    manifest = {
        "job_id": job.get("id"),
        "item_id": item["id"],
        "input": {"source": str(source), "render_source": str(no_subtitle)},
        "outputs": {
            "edited_video": str(output_video),
            "edited_cover": str(cover_path),
            "edited_subtitle": str(ass_path),
            "edit_plan": str(plan_path),
        },
        "duration": {"source": duration, "edited": edited_duration},
        "created_at": _now(),
        "errors": [],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    project["outputs"] = {
        "edited_video": str(output_video),
        "edited_cover": str(cover_path),
        "edited_subtitle": str(ass_path),
        "edit_plan": str(plan_path),
        "render_manifest": str(manifest_path),
    }
    _write_edit_project(job_dir, project)
    return [
        _output_file_item(job_dir, output_video),
        _output_file_item(job_dir, cover_path),
        _output_file_item(job_dir, ass_path),
        _output_file_item(job_dir, plan_path),
        _output_file_item(job_dir, manifest_path),
    ]


def _is_clean_editor_source(job_dir: Path, source: Path) -> bool:
    try:
        resolved = source.resolve()
        work_dir = (job_dir / "work").resolve()
    except OSError:
        return False
    return source.name == "cut_no_subtitles.mp4" and str(resolved).startswith(str(work_dir))


def _edit_cues(project: dict[str, Any], removed: list[Segment], duration: float) -> list[SubtitleCue]:
    cues = []
    offset = float(project.get("settings", {}).get("subtitle_offset", 0.0))
    for sentence in project.get("sentences", []):
        if sentence.get("remove_video") or not sentence.get("enabled", True):
            continue
        start = float(sentence.get("start", 0.0))
        end = float(sentence.get("end", 0.0))
        if end <= start:
            continue
        mapped_start = _map_time_after_removes(start, removed)
        mapped_end = _map_time_after_removes(end, removed)
        if mapped_start is None or mapped_end is None:
            continue
        mapped_start = max(0.0, mapped_start + offset)
        mapped_end = min(duration, mapped_end + offset)
        if mapped_end <= mapped_start:
            continue
        cues.append(SubtitleCue(index=len(cues) + 1, start=mapped_start, end=mapped_end, text=str(sentence.get("text") or "")))
    return cues


def _edit_title_cues(project: dict[str, Any], removed: list[Segment], duration: float) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for clip in project.get("title_clips", []):
        if not clip.get("enabled", False):
            continue
        mapped_start = _map_time_after_removes(float(clip.get("start", 0.0)), removed)
        mapped_end = _map_time_after_removes(float(clip.get("end", 0.0)), removed)
        if mapped_start is None or mapped_end is None or mapped_end <= mapped_start:
            continue
        cues.append(
            SubtitleCue(
                index=len(cues) + 1,
                start=max(0.0, mapped_start),
                end=min(duration, mapped_end),
                text=str(clip.get("text") or ""),
            )
        )
    return cues


def _timeline_clips(project: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    for sentence in sorted(project.get("sentences", []), key=lambda item: (float(item.get("timeline_order", 0)), float(item.get("start", 0)))):
        if sentence.get("remove_video"):
            continue
        if sentence.get("synthetic_gap") and sentence.get("gap"):
            start = _clamp_float(sentence.get("clip_start", sentence.get("start", 0.0)), 0.0, 3600.0)
            end = _clamp_float(sentence.get("clip_end", sentence.get("end", start + 1.0)), start + 0.05, 3600.0)
        else:
            start = _clamp_float(sentence.get("clip_start", sentence.get("start", 0.0)), 0.0, duration)
            end = _clamp_float(sentence.get("clip_end", sentence.get("end", start)), 0.0, duration)
        if end - start <= 0.05:
            continue
        clips.append({"sentence": sentence, "segment": Segment(start, end)})
    if clips:
        return clips
    return [{"sentence": {}, "segment": Segment(0.0, duration)}]


def _clip_is_gap(clip: dict[str, Any]) -> bool:
    sentence = clip.get("sentence") if isinstance(clip.get("sentence"), dict) else {}
    return bool(sentence.get("gap")) and not bool(sentence.get("remove_video"))


def _render_timeline_video(source: Path, output: Path, clips: list[dict[str, Any]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if not any(_clip_is_gap(clip) for clip in clips):
        cut_video(source, output, [clip["segment"] for clip in clips])
        return

    width, height = video_size(source)
    parts: list[str] = []
    labels: list[str] = []
    for index, clip in enumerate(clips):
        segment = clip["segment"]
        duration = max(0.05, float(segment.duration))
        if _clip_is_gap(clip):
            parts.append(
                f"color=c=black:s={width}x{height}:r=30:d={duration:.3f},"
                f"format=yuv420p,setsar=1[v{index}]"
            )
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=44100:d={duration:.3f},"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{index}]"
            )
        else:
            parts.append(
                f"[0:v]trim=start={segment.start:.3f}:end={segment.end:.3f},"
                f"setpts=PTS-STARTPTS,scale={width}:{height},fps=30,format=yuv420p,setsar=1[v{index}]"
            )
            parts.append(
                f"[0:a]atrim=start={segment.start:.3f}:end={segment.end:.3f},"
                f"asetpts=PTS-STARTPTS,"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{index}]"
            )
        labels.append(f"[v{index}][a{index}]")

    filter_complex = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(clips)}:v=1:a=1[v][a]"
    ffmpeg_run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
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
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def _timeline_subtitle_cues(project: dict[str, Any], clips: list[dict[str, Any]], duration: float) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    offset = float(project.get("settings", {}).get("subtitle_offset", 0.0))
    cursor = 0.0
    for clip in clips:
        sentence = clip.get("sentence") or {}
        segment = clip["segment"]
        clip_duration = segment.duration
        if sentence.get("enabled", True) and str(sentence.get("text") or "").strip():
            source_start = _clamp_float(sentence.get("start", segment.start), segment.start, segment.end)
            source_end = _clamp_float(sentence.get("end", segment.end), segment.start, segment.end)
            cue_start = cursor + max(0.0, source_start - segment.start) + offset
            cue_end = cursor + min(clip_duration, source_end - segment.start) + offset
            cue_start = max(0.0, cue_start)
            cue_end = min(duration, cue_end)
            if cue_end > cue_start:
                cues.append(SubtitleCue(index=len(cues) + 1, start=cue_start, end=cue_end, text=str(sentence.get("text") or "")))
        cursor += clip_duration
    return cues


def _timeline_title_cues(project: dict[str, Any], duration: float) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    for clip in project.get("title_clips", []):
        if not clip.get("enabled", False):
            continue
        start = _clamp_float(clip.get("start", 0.0), 0.0, duration)
        end = _clamp_float(clip.get("end", start), 0.0, duration)
        if end <= start:
            continue
        cues.append(SubtitleCue(index=len(cues) + 1, start=start, end=end, text=str(clip.get("text") or "")))
    return cues


def _write_edit_ass(
    subtitle_cues: list[SubtitleCue],
    title_cues: list[SubtitleCue],
    path: Path,
    *,
    width: int,
    height: int,
    preset: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_style = preset.get("subtitle") or DEFAULT_STYLE_PRESETS[0]["subtitle"]
    title_style = dict(DEFAULT_STYLE_PRESETS[0]["subtitle"])
    title_style.update(preset.get("cover_title") or {})
    title_style.setdefault("position_x", 0)
    title_style.setdefault("position_y", -520)
    title_style.setdefault("text_align", "center")
    title_style.setdefault("opacity", 100)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        subtitle_to_ass_style(subtitle_style, width=width, height=height).replace("Style: Default,", "Style: Subtitle,", 1),
        subtitle_to_ass_style(title_style, width=width, height=height).replace("Style: Default,", "Style: Title,", 1),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue in subtitle_cues:
        override = subtitle_override(subtitle_style, cue.start, cue.end, width=width, height=height)
        lines.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Subtitle,,0,0,0,,{override}{_ass_text(cue.text)}")
    for cue in title_cues:
        override = subtitle_override(title_style, cue.start, cue.end, width=width, height=height)
        lines.append(f"Dialogue: 1,{_ass_time(cue.start)},{_ass_time(cue.end)},Title,,0,0,0,,{override}{_ass_text(cue.text)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_edit_plan(
    project: dict[str, Any],
    clips: list[dict[str, Any]],
    subtitle_cues: list[SubtitleCue],
    title_cues: list[SubtitleCue],
    duration: float,
) -> dict[str, Any]:
    cursor = 0.0
    timeline_segments = []
    for index, clip in enumerate(clips, start=1):
        segment = clip["segment"]
        sentence = clip.get("sentence") or {}
        timeline_segments.append(
            {
                "index": index,
                "sentence_id": sentence.get("id") or "",
                "type": "gap" if sentence.get("gap") and not sentence.get("remove_video") else "source",
                "text": sentence.get("text") or sentence.get("original_text") or "",
                "source_start": segment.start,
                "source_end": segment.end,
                "timeline_start": round(cursor, 3),
                "timeline_end": round(cursor + segment.duration, 3),
                "subtitle_enabled": bool(sentence.get("enabled", True)),
                "gap": bool(sentence.get("gap", False)),
                "synthetic_gap": bool(sentence.get("synthetic_gap", False)),
            }
        )
        cursor += segment.duration
    return {
        "version": 1,
        "source_duration": float(project.get("duration") or 0.0),
        "edited_duration": duration,
        "timeline_segments": timeline_segments,
        "removed_sentence_ids": [str(item.get("id")) for item in project.get("sentences", []) if item.get("remove_video")],
        "subtitle_cues": [{"start": item.start, "end": item.end, "text": item.text} for item in subtitle_cues],
        "title_cues": [{"start": item.start, "end": item.end, "text": item.text} for item in title_cues],
        "title_clips": [
            {
                "id": str(item.get("id") or ""),
                "start": float(item.get("start") or 0.0),
                "end": float(item.get("end") or 0.0),
                "text": str(item.get("text") or ""),
                "enabled": bool(item.get("enabled", False)),
                "use_for_cover": bool(item.get("use_for_cover", False)),
            }
            for item in project.get("title_clips", [])
            if isinstance(item, dict)
        ],
        "render_settings": {
            "style_preset_id": project.get("style_preset_id") or "default-white",
            "subtitle_offset": project.get("settings", {}).get("subtitle_offset", 0.0),
        },
    }


def _ass_time(seconds: float) -> str:
    centiseconds = round(max(0.0, seconds) * 100)
    hours, rem = divmod(centiseconds, 360_000)
    minutes, rem = divmod(rem, 6_000)
    sec, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02}:{sec:02}.{cs:02}"


def _ass_text(text: str) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _map_time_after_removes(value: float, removed: list[Segment]) -> float | None:
    shift = 0.0
    for segment in removed:
        if segment.start <= value < segment.end:
            return None
        if segment.end <= value:
            shift += segment.duration
    return max(0.0, value - shift)


def _merge_segments(segments: list[Segment], duration: float) -> list[Segment]:
    ordered = sorted((Segment(max(0.0, item.start), min(duration, item.end)) for item in segments if item.end > item.start), key=lambda item: item.start)
    merged: list[Segment] = []
    for segment in ordered:
        if not merged or segment.start > merged[-1].end:
            merged.append(segment)
        else:
            merged[-1] = Segment(merged[-1].start, max(merged[-1].end, segment.end))
    return merged


def _keep_segments(duration: float, removed: list[Segment]) -> list[Segment]:
    keep: list[Segment] = []
    cursor = 0.0
    for segment in removed:
        if segment.start > cursor:
            keep.append(Segment(cursor, segment.start))
        cursor = max(cursor, segment.end)
    if cursor < duration:
        keep.append(Segment(cursor, duration))
    return [item for item in keep if item.duration > 0.05] or [Segment(0.0, duration)]


def _find_job_item(job: dict[str, Any], item_id: str) -> dict[str, Any]:
    items = job.get("params", {}).get("items") or []
    if not items:
        params = job.get("params", {})
        items = [{"id": "001", "title": params.get("title"), "video": params.get("video"), "output_dir": params.get("output_dir")}]
    for item in items:
        if str(item.get("id")) == str(item_id):
            return item
    return items[0]


def _edit_project_path(job_dir: Path, item_id: str) -> Path:
    safe_item = "".join(char for char in str(item_id) if char.isalnum() or char in {"-", "_"}) or "001"
    return job_dir / f"edit_project_{safe_item}.json"


def _safe_path_segment(value: Any) -> str:
    return "".join(char for char in str(value) if char.isalnum() or char in {"-", "_"})[:80]


def _download_url_for_path(job_dir: Path, path: Path) -> str:
    output_dir = (job_dir / "output").resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(output_dir)):
        fallback = _find_preview_video(job_dir / "output")
        if not fallback:
            raise HTTPException(status_code=404, detail="Preview video not found")
        resolved = fallback.resolve()
    rel = resolved.relative_to(output_dir).as_posix()
    return f"/jobs/{job_dir.name}/download/{quote(rel)}"


def _media_url_for_path(job_dir: Path, path: Path) -> str:
    resolved = path.resolve()
    output_dir = (job_dir / "output").resolve()
    work_dir = (job_dir / "work").resolve()
    if str(resolved).startswith(str(output_dir)):
        rel = resolved.relative_to(output_dir).as_posix()
        return f"/jobs/{job_dir.name}/download/{quote(rel)}"
    if str(resolved).startswith(str(work_dir)):
        rel = resolved.relative_to(work_dir).as_posix()
        return f"/jobs/{job_dir.name}/editor-media/{quote(rel)}"
    return _download_url_for_path(job_dir, path)


def _safe_editor_assets(job_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    try:
        return _ensure_editor_assets(job_dir, project)
    except Exception as exc:
        _write_web_traceback(job_dir, "editor-assets", exc)
        return {"thumbs": [], "waveform": [], "duration": project.get("duration") or 0, "error": "editor_assets_failed"}


def _ensure_editor_assets(job_dir: Path, project: dict[str, Any]) -> dict[str, Any]:
    source = Path(project.get("render_source_video") or project.get("current_video") or "")
    if not source.exists():
        return {"thumbs": [], "waveform": [], "duration": project.get("duration") or 0, "error": "source_missing"}
    item_id = _safe_path_segment(project.get("item_id") or "001") or "001"
    asset_dir = job_dir / "work" / "editor_assets" / item_id
    asset_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = asset_dir / "manifest.json"
    source_signature = f"{source.resolve()}::{source.stat().st_size}::{source.stat().st_mtime_ns}"
    if manifest_path.exists():
        try:
            manifest = read_json_file(manifest_path)
            if manifest.get("source_signature") == source_signature:
                return _editor_assets_payload(job_dir, item_id, manifest)
        except RuntimeError:
            pass

    duration = float(project.get("duration") or media_duration(source))
    width, height = video_size(source)
    thumb_count = max(10, min(42, math.ceil(max(duration, 1.0) / 2.0)))
    thumbs: list[dict[str, Any]] = []
    for index in range(thumb_count):
        time = min(max(duration - 0.05, 0.0), duration * (index + 0.5) / thumb_count)
        filename = f"thumb_{index:03d}.jpg"
        output = asset_dir / filename
        ffmpeg_run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{time:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=96:-2",
                "-q:v",
                "5",
                str(output),
            ]
        )
        thumbs.append({"time": round(time, 3), "file": filename})

    waveform = _build_waveform(asset_dir, source, bars=360)
    manifest = {
        "version": 1,
        "item_id": item_id,
        "source": str(source),
        "source_signature": source_signature,
        "duration": duration,
        "video": {"width": width, "height": height},
        "thumbs": thumbs,
        "waveform": waveform,
        "created_at": _now(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    return _editor_assets_payload(job_dir, item_id, manifest)


def _editor_assets_payload(job_dir: Path, item_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    thumbs = []
    for item in manifest.get("thumbs", []):
        if not isinstance(item, dict) or not item.get("file"):
            continue
        filename = quote(str(item["file"]))
        thumbs.append(
            {
                "time": float(item.get("time") or 0.0),
                "url": f"/jobs/{job_dir.name}/editor-assets/{quote(item_id)}/{filename}",
            }
        )
    return {
        "duration": float(manifest.get("duration") or 0.0),
        "video": manifest.get("video") if isinstance(manifest.get("video"), dict) else {},
        "thumbs": thumbs,
        "waveform": manifest.get("waveform") if isinstance(manifest.get("waveform"), list) else [],
        "error": manifest.get("error") or "",
    }


def _build_waveform(asset_dir: Path, source: Path, bars: int = 320) -> list[float]:
    wav_path = asset_dir / "waveform.wav"
    try:
        ffmpeg_run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-acodec",
                "pcm_s16le",
                str(wav_path),
            ]
        )
        with wave.open(str(wav_path), "rb") as handle:
            sample_width = handle.getsampwidth()
            raw = handle.readframes(handle.getnframes())
        if sample_width != 2 or not raw:
            return []
        sample_count = len(raw) // 2
        samples = struct_unpack_int16(raw, sample_count)
        if not samples:
            return []
        chunk_size = max(1, math.ceil(len(samples) / bars))
        values: list[float] = []
        for start in range(0, len(samples), chunk_size):
            chunk = samples[start : start + chunk_size]
            if not chunk:
                continue
            rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) / 32768
            values.append(round(min(1.0, rms * 4.0), 4))
        return values[:bars]
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass


def struct_unpack_int16(raw: bytes, sample_count: int) -> tuple[int, ...]:
    return struct.unpack(f"<{sample_count}h", raw[: sample_count * 2])


def _editor_video_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"width": 0, "height": 0, "aspect": "--", "size": "--"}
    try:
        width, height = video_size(path)
    except Exception:
        return {"width": 0, "height": 0, "aspect": "--", "size": _format_size(path.stat().st_size)}
    if height and abs(width / height - 9 / 16) < 0.04:
        aspect = "9:16"
    elif height and abs(width / height - 16 / 9) < 0.04:
        aspect = "16:9"
    elif width == height:
        aspect = "1:1"
    elif height and abs(width / height - 4 / 5) < 0.04:
        aspect = "4:5"
    else:
        aspect = f"{width}:{height}"
    return {"width": width, "height": height, "aspect": aspect, "size": _format_size(path.stat().st_size)}


def _find_preview_video(output_dir: Path) -> Path | None:
    if not output_dir.exists():
        return None
    videos = [path for path in output_dir.rglob("*.mp4") if path.is_file() and "无字幕" not in path.name]
    if not videos:
        return None
    videos.sort(key=lambda path: (0 if "edited" not in path.parts else 1, len(path.parts), path.name))
    return videos[0]


def _load_editor_segments(job_dir: Path, item: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        job_dir / "work" / item["id"] / "volcengine_segments.json",
        Path(item.get("output_dir") or "") / "volcengine_segments.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = read_json_file(path)
        except RuntimeError:
            continue
        rows = data if isinstance(data, list) else data.get("utterances", []) if isinstance(data, dict) else []
        segments = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = row.get("start") if row.get("start") is not None else row.get("start_time", 0) / 1000
            end = row.get("end") if row.get("end") is not None else row.get("end_time", 0) / 1000
            text = str(row.get("text") or "").strip()
            if text:
                segments.append({"start": float(start), "end": float(end), "text": text})
        if segments:
            return segments
    return []


def _load_srt_segments(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8", errors="ignore").lstrip("\ufeff")
    segments = []
    blocks = [block.strip() for block in content.replace("\r\n", "\n").split("\n\n") if block.strip()]
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        start_text, end_text = [part.strip() for part in time_line.split("-->", 1)]
        text = " ".join(line for line in lines if line != time_line and not line.isdigit()).lstrip("\ufeff").strip()
        segments.append({"start": _srt_seconds(start_text), "end": _srt_seconds(end_text), "text": text})
    return segments


def _srt_seconds(value: str) -> float:
    head, _, ms_text = value.partition(",")
    parts = [int(part) for part in head.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + (int(ms_text[:3] or "0") / 1000)


def _recent_jobs() -> list[dict[str, Any]]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for job_file in JOBS_DIR.glob("*/job.json"):
        try:
            jobs.append(_load_json_file(job_file))
        except RuntimeError:
            continue
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jobs[:8]


def _output_files(job_id: str) -> list[dict[str, str]]:
    job_dir = _job_path(job_id)
    output_dir = job_dir / "output"
    if not output_dir.exists():
        return []
    files = []
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(output_dir).as_posix()
        if path.suffix.lower() not in {".mp4", ".jpg", ".srt", ".ass", ".json", ".zip"}:
            continue
        files.append(_output_file_item(job_dir, path, rel))
    files.sort(key=lambda item: (0 if item["name"].endswith(".zip") else 1, item["name"]))
    return files


def _output_file_item(job_dir: Path, path: Path, rel: str | None = None) -> dict[str, str]:
    output_dir = job_dir / "output"
    rel = rel or path.relative_to(output_dir).as_posix()
    return {
        "name": rel,
        "display_name": path.name,
        "download_name": path.name,
        "description": _file_description(path),
        "path": str(path),
        "size": _format_size(path.stat().st_size),
    }


def _file_description(path: Path) -> str:
    name = path.name
    if name.endswith(".zip"):
        return "批量打包下载"
    if name.endswith("-封面.jpg"):
        return "视频封面"
    if name.endswith(".mp4"):
        return "最终导出视频"
    if name == "subtitle.srt":
        return "通用字幕文件"
    if name == "subtitle.ass":
        return "烧录字幕样式"
    if name == "edit_report.json":
        return "剪辑参数和片段报告"
    if name == "volcengine_segments.json":
        return "火山引擎识别结果"
    if name == "edit_plan.json":
        return "精修渲染计划"
    if name == "render_manifest.json":
        return "精修导出清单"
    return "输出文件"


def _format_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return read_json_file(SETTINGS_PATH)
    except RuntimeError:
        return {}


def _save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json_file(path: Path) -> dict[str, Any]:
    data = read_json_file(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid JSON from {path}: expected object")
    return data


def _short_process_error(proc: subprocess.CompletedProcess[str], secrets: dict[str, str]) -> str:
    text = proc.stderr.strip() or proc.stdout.strip() or f"Command failed: {proc.returncode}"
    text = _mask_secrets(text, secrets)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Error:"):
            return line
    return text.splitlines()[0] if text.splitlines() else f"Command failed: {proc.returncode}"


def _mask_secrets(text: str, secrets: dict[str, str]) -> str:
    masked = text
    for value in secrets.values():
        if value:
            masked = masked.replace(value, "***")
    return masked


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    token = settings.get("volc_access_token", "")
    return {
        "volc_app_id": settings.get("volc_app_id", ""),
        "has_volc_access_token": bool(token),
        "subtitle_delay": settings.get("subtitle_delay", 0.0),
        "detect_disfluency": bool(settings.get("detect_disfluency", False)),
    }


def _preset_from_form(form: Any, preset_id: str) -> dict[str, Any]:
    subtitle = _style_from_form(form)
    cover_title = _cover_style_from_form(form)
    subtitle_json = _form_json(form, "subtitle_style_json")
    cover_json = _form_json(form, "cover_style_json")
    if subtitle_json:
        subtitle = _normalize_subtitle_style(subtitle_json)
    if cover_json:
        cover_title = _normalize_cover_style(cover_json)
    return {
        "id": preset_id,
        "name": _form_str(form, "name", preset_id).strip() or preset_id,
        "subtitle": subtitle,
        "cover_title": cover_title,
    }


def _style_from_form(form: Any) -> dict[str, Any]:
    return _normalize_subtitle_style(
        {
            "font_family": _form_str(form, "font_family", "Microsoft YaHei"),
            "font_size": _form_int(form, "font_size", 64),
            "bold": _form_bool(form, "bold"),
            "italic": _form_bool(form, "italic"),
            "underline": _form_bool(form, "underline"),
            "primary_color": _form_str(form, "primary_color", "#ffffff"),
            "opacity": _form_int(form, "opacity", 100),
            "outline_enabled": _form_bool(form, "outline_enabled"),
            "outline_color": _form_str(form, "outline_color", "#000000"),
            "shadow_enabled": _form_bool(form, "shadow_enabled"),
            "shadow_color": _form_str(form, "shadow_color", "#000000"),
            "outline_width": _form_float(form, "outline_width", 4),
            "shadow_offset": _form_float(form, "shadow_offset", 0),
            "blur": _form_float(form, "blur", 0),
            "letter_spacing": _form_float(form, "letter_spacing", 0),
            "line_spacing": _form_float(form, "line_spacing", 0),
            "scale": _form_float(form, "scale", 100),
            "uniform_scale": _form_bool(form, "uniform_scale"),
            "scale_x": _form_float(form, "scale_x", 100),
            "scale_y": _form_float(form, "scale_y", 100),
            "position_x": _form_float(form, "position_x", 0),
            "position_y": _form_float(form, "position_y", 650),
            "rotation": _form_float(form, "rotation", 0),
            "text_align": _form_str(form, "text_align", "center"),
            "background_enabled": _form_bool(form, "background_enabled"),
            "background_color": _form_str(form, "background_color", "#000000"),
            "background_opacity": _form_int(form, "background_opacity", 52),
            "background_padding": _form_int(form, "background_padding", 18),
            "glow_enabled": _form_bool(form, "glow_enabled"),
            "glow_color": _form_str(form, "glow_color", "#ffffff"),
            "glow_strength": _form_float(form, "glow_strength", 0),
            "alignment": 5,
            "margin_x": _form_int(form, "margin_x", 80),
            "margin_y": _form_int(form, "margin_y", 170),
            "target_len": _form_int(form, "target_len", 12),
            "max_len": _form_int(form, "max_len", 18),
            "animation_in": _form_str(form, "animation_in", "none"),
            "animation_out": _form_str(form, "animation_out", "none"),
        }
    )


def _cover_style_from_form(form: Any) -> dict[str, Any]:
    return _normalize_cover_style(_style_from_form(form))


def _normalize_subtitle_style(style: dict[str, Any]) -> dict[str, Any]:
    base = dict(DEFAULT_STYLE_PRESETS[0]["subtitle"])
    base.update(style)
    return base


def _normalize_cover_style(style: dict[str, Any]) -> dict[str, Any]:
    base = dict(DEFAULT_STYLE_PRESETS[0]["subtitle"])
    base.update(DEFAULT_STYLE_PRESETS[0]["cover_title"])
    base.update(
        {
            "italic": False,
            "underline": False,
            "opacity": 100,
            "outline_enabled": True,
            "shadow_enabled": False,
            "letter_spacing": 0,
            "line_spacing": 0,
            "scale": 100,
            "uniform_scale": True,
            "scale_x": 100,
            "scale_y": 100,
            "position_x": 0,
            "position_y": -520,
            "rotation": 0,
            "text_align": "center",
            "glow_enabled": False,
            "glow_color": "#fff446",
            "glow_strength": 0,
            "target_len": 10,
            "max_len": 16,
            "animation_in": "none",
            "animation_out": "none",
        }
    )
    base.update(style)
    return base


def _form_json(form: Any, key: str) -> dict[str, Any]:
    value = form.get(key)
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _preview_ass(text: str, style: dict[str, Any], width: int, height: int) -> str:
    override = subtitle_override(style, 0, 3, width=width, height=height)
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            subtitle_to_ass_style(style, width=width, height=height),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            f"Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,{override}{_ass_text(text)}",
            "",
        ]
    )

def _form_str(form: Any, key: str, default: str) -> str:
    value = form.get(key)
    return str(value) if value not in (None, "") else default


def _form_bool(form: Any, key: str) -> bool:
    return form.get(key) not in (None, "", "0", "false", "False")


def _form_int(form: Any, key: str, default: int) -> int:
    try:
        return int(float(str(form.get(key))))
    except (TypeError, ValueError):
        return default


def _form_float(form: Any, key: str, default: float) -> float:
    try:
        return float(str(form.get(key)))
    except (TypeError, ValueError):
        return default


def _unique_preset_id(presets: list[dict[str, Any]], base: str) -> str:
    existing = {item["id"] for item in presets}
    candidate = _slug(base)
    if candidate not in existing:
        return candidate
    index = 2
    while f"{candidate}-{index}" in existing:
        index += 1
    return f"{candidate}-{index}"


def _style_presets_url(
    preset_id: str,
    *,
    saved: bool = False,
    preview_path: str = "",
    active: str = "subtitle",
    text: str = "",
    aspect: str = "9:16",
) -> str:
    parts = [f"preset={quote(preset_id)}"]
    if preview_path:
        parts.append(f"preview={quote(preview_path, safe='/')}")
    if active in {"subtitle", "cover"}:
        parts.append(f"active={quote(active)}")
    if text:
        parts.append(f"text={quote(text)}")
    aspect = _preview_aspect(aspect)
    if aspect:
        parts.append(f"aspect={quote(aspect)}")
    if saved:
        parts.append("saved=1")
    return "/style-presets?" + "&".join(parts)


def _preview_aspect(value: str | None) -> str:
    if value in {"9:16", "16:9", "1:1", "4:5"}:
        return value
    return "9:16"


def _preview_dimensions(aspect: str) -> tuple[int, int]:
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "1:1":
        return 1080, 1080
    if aspect == "4:5":
        return 1080, 1350
    return 1080, 1920


def _slug(value: str) -> str:
    allowed = []
    for char in value.lower().replace(" ", "-"):
        if char.isalnum() or char in {"-", "_"}:
            allowed.append(char)
    return "".join(allowed).strip("-") or "style"
