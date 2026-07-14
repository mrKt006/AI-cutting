from __future__ import annotations

import asyncio
import hashlib
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
from ai_layout import layout_tokens_with_ai  # noqa: E402
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
from subtitle_layout import analysis_break_sets, segment_tokens, text_overflows, tokens_from_text, wrap_title_text  # noqa: E402
from llm_analysis import analyze_transcript, apply_high_confidence_corrections  # noqa: E402
from transcript_document import (  # noqa: E402
    ParsedTranscriptDocument,
    TranscriptDocumentError,
    parse_transcript_document,
)


ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "jobs"
PYTHON = sys.executable
JOB_SECRETS: dict[str, dict[str, str]] = {}
ACTIVE_JOB_IDS: set[str] = set()
SETTINGS_PATH = ROOT / "web" / "settings.local.json"
STYLE_PRESETS_PATH = ROOT / "web" / "style_presets.local.json"
PREVIEW_DIR = ROOT / "web" / "static" / "style_previews"
LLM_CACHE_DIR = ROOT / "web" / ".cache" / "llm"
TITLE_WRAP_CACHE: dict[str, dict[str, Any]] = {}
WINDOWS_TOOL_DIRS = [
    Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
]
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
MAX_VIDEOS_PER_JOB = 20
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024

PRESETS = {
    "natural": {"label": "保守", "decision_label": "AI 保守决策", "noise": "-30dB", "min_silence": 0.45, "padding": 0.12, "auto_edit_mode": "conservative"},
    "standard": {"label": "标准", "decision_label": "AI 标准决策", "noise": "-28dB", "min_silence": 0.35, "padding": 0.10, "auto_edit_mode": "standard"},
    "compact": {"label": "紧凑", "decision_label": "AI 标准决策", "noise": "-26dB", "min_silence": 0.30, "padding": 0.08, "auto_edit_mode": "standard"},
    "aggressive": {"label": "激进", "decision_label": "AI 直接精简", "noise": "-24dB", "min_silence": 0.25, "padding": 0.06, "auto_edit_mode": "aggressive"},
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


@app.on_event("startup")
def recover_interrupted_jobs() -> None:
    """A process restart cannot leave old jobs looking permanently active."""
    if not JOBS_DIR.exists():
        return
    for job_file in JOBS_DIR.glob("*/job.json"):
        try:
            job = read_json_file(job_file)
        except RuntimeError:
            continue
        if not isinstance(job, dict) or job.get("status") not in {"queued", "running"}:
            continue
        job["status"] = "failed"
        job["stage"] = "failed"
        job["error"] = "服务曾中断，任务未能完成。请点击重新处理。"
        job["updated_at"] = _now()
        for item in job.get("params", {}).get("items", []):
            if item.get("status") in {"queued", "running"}:
                item["status"] = "failed"
                item["error"] = "服务中断"
        job_file.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


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
    env = _runtime_status(settings)
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


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, status: str = "", q: str = "") -> HTMLResponse:
    normalized_status = status if status in STAGES else ""
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "request": request,
            "jobs": _recent_jobs(limit=None, status=normalized_status, query=q),
            "active_status": normalized_status,
            "query": q.strip(),
            "status_labels": STAGES,
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
            "env": _runtime_status(settings),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/settings")
async def save_settings(
    volc_app_id: str = Form(""),
    volc_access_token: str = Form(""),
    subtitle_delay: float = Form(0.0),
    detect_disfluency: str | None = Form(None),
    llm_enabled: str | None = Form(None),
    llm_base_url: str = Form(""),
    llm_model: str = Form(""),
    llm_api_key: str = Form(""),
) -> RedirectResponse:
    existing = _load_settings()
    token = volc_access_token.strip() or existing.get("volc_access_token", "")
    llm_key = llm_api_key.strip() or existing.get("llm_api_key", "")
    _save_settings(
        {
            "volc_app_id": volc_app_id.strip(),
            "volc_access_token": token,
            "subtitle_delay": subtitle_delay,
            "detect_disfluency": bool(detect_disfluency),
            "llm_enabled": bool(llm_enabled),
            "llm_base_url": llm_base_url.strip(),
            "llm_model": llm_model.strip(),
            "llm_api_key": llm_key,
        }
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/settings/test-llm")
async def api_test_llm() -> dict[str, Any]:
    settings = _load_settings()
    result = await asyncio.to_thread(
        analyze_transcript,
        [{"id": "test-1", "text": "测试"}],
        base_url=str(settings.get("llm_base_url") or ""),
        model=str(settings.get("llm_model") or ""),
        api_key=str(settings.get("llm_api_key") or ""),
        timeout=20.0,
    )
    if result.get("status") != "ok":
        raise HTTPException(status_code=400, detail="模型连接失败，请检查 Base URL、Model 和 API Key")
    return {"ok": True, "message": "模型连接正常"}


@app.get("/style-presets", response_class=HTMLResponse)
def style_presets_page(request: Request) -> HTMLResponse:
    presets = load_style_presets(STYLE_PRESETS_PATH)
    selected_id = request.query_params.get("preset") or presets[0]["id"]
    selected = get_style_preset(selected_id, STYLE_PRESETS_PATH)
    preview = request.query_params.get("preview") or ""
    active_style = request.query_params.get("active") or "subtitle"
    if active_style not in {"subtitle", "video", "cover"}:
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
    original_preset_id = _slug(str(form.get("original_preset_id") or preset_id))
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
    if preset_id != original_preset_id and any(item["id"] == preset_id for item in presets):
        raise HTTPException(status_code=400, detail="预设 ID 已存在，请换一个 ID")
    replaced = False
    for index, item in enumerate(presets):
        if item["id"] == original_preset_id:
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
    render_style = preset["cover_title"] if active_style == "cover" else preset["video_title"] if active_style == "video" else preset["subtitle"]
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


@app.post("/api/title-wrap")
async def api_title_wrap(request: Request) -> dict[str, Any]:
    payload = await _read_json_object(request, source="标题智能换行", required=True)
    text = str(payload.get("text") or "").strip()
    preset_id = str(payload.get("preset_id") or "default-white")
    kind = "cover_title" if payload.get("kind") == "cover" else "video_title"
    aspect = _preview_aspect(str(payload.get("aspect") or "9:16"))
    width, height = _preview_dimensions(aspect)
    preset = get_style_preset(preset_id, STYLE_PRESETS_PATH)
    style = dict(preset.get(kind) or {})
    if not text:
        return {"ok": True, "text": "", "style": style, "analysis_status": "skipped"}
    cache_key = hashlib.sha1(
        json.dumps({"text": text, "preset": preset_id, "kind": kind, "aspect": aspect}, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if cache_key in TITLE_WRAP_CACHE:
        return TITLE_WRAP_CACHE[cache_key]
    settings = _load_settings()
    source_lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    tokens = [
        token
        for line_index, line in enumerate(source_lines or [text], start=1)
        for token in tokens_from_text(line, 0.0, 1.0, prefix=f"title-{line_index}")
    ]
    analysis = await asyncio.to_thread(
        analyze_transcript,
        tokens,
        base_url=str(settings.get("llm_base_url") or ""),
        model=str(settings.get("llm_model") or ""),
        api_key=str(settings.get("llm_api_key") or ""),
        timeout=float(settings.get("llm_timeout") or 60.0),
        cache_dir=LLM_CACHE_DIR,
    )
    wrapped_lines: list[str] = []
    layout_audits: list[dict[str, Any]] = []
    for line_index, line in enumerate(source_lines or [text], start=1):
        line_tokens = tokens_from_text(line, 0.0, 1.0, prefix=f"title-{line_index}")
        groups, layout_audit = await asyncio.to_thread(
            layout_tokens_with_ai,
            line_tokens,
            style,
            width,
            height,
            analysis,
            base_url=str(settings.get("llm_base_url") or ""),
            model=str(settings.get("llm_model") or ""),
            api_key=str(settings.get("llm_api_key") or ""),
            timeout=float(settings.get("llm_timeout") or 60.0),
            cache_dir=LLM_CACHE_DIR,
        )
        wrapped_lines.extend("".join(str(token.get("text") or "") for token in group) for group in groups)
        layout_audits.append(layout_audit)
    result = {
        "ok": True,
        "text": "\n".join(wrapped_lines).strip() or wrap_title_text(text, style, width, height, analysis),
        "style": style,
        "analysis_status": analysis.get("status"),
        "layout_status": "ai" if layout_audits and all(item.get("status") == "ai" for item in layout_audits) else "mixed",
    }
    if len(TITLE_WRAP_CACHE) >= 128:
        TITLE_WRAP_CACHE.pop(next(iter(TITLE_WRAP_CACHE)))
    TITLE_WRAP_CACHE[cache_key] = result
    return result


@app.post("/api/transcripts/parse")
async def parse_transcript_preview(file: UploadFile = File(...)) -> dict[str, Any]:
    _, parsed = await _read_transcript_upload(file)
    return _transcript_preview_payload(parsed, file.filename or "")


@app.post("/jobs")
async def create_job(
    video: list[UploadFile] = File(...),
    title: str = Form(""),
    item_titles: list[str] = Form([]),
    content_title: str = Form(""),
    cover_title: str = Form(""),
    item_content_titles: list[str] = Form([]),
    item_cover_titles: list[str] = Form([]),
    transcript_files: list[UploadFile] = File([]),
    transcript_indices: list[int] = Form([]),
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
    runtime = _runtime_status(settings)
    if not runtime["ffmpeg_ready"]:
        detail = runtime.get("ffmpeg_error") or "本机未检测到 FFmpeg / FFprobe"
        raise HTTPException(status_code=400, detail=f"视频运行环境不可用：{detail}")
    has_env_creds = bool(os.environ.get("VOLC_APP_ID") and os.environ.get("VOLC_ACCESS_TOKEN"))
    has_saved_creds = bool(settings.get("volc_app_id") and settings.get("volc_access_token"))
    if not has_env_creds and not has_saved_creds:
        raise HTTPException(status_code=400, detail="火山引擎模式需要先在设置页填写 APP ID 和 Access Token")
    videos = [item for item in video if item and item.filename]
    if not videos:
        raise HTTPException(status_code=400, detail="请至少上传一个视频")
    if len(videos) > MAX_VIDEOS_PER_JOB:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {MAX_VIDEOS_PER_JOB} 个视频")
    for upload in videos:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in SUPPORTED_VIDEO_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"{upload.filename} 不是支持的视频格式")
        content_type = str(upload.content_type or "").lower()
        if content_type and not (content_type.startswith("video/") or content_type == "application/octet-stream"):
            raise HTTPException(status_code=400, detail=f"{upload.filename} 的文件类型不是视频")

    uploaded_transcripts = [item for item in transcript_files if item and item.filename]
    if len(uploaded_transcripts) != len(transcript_indices):
        raise HTTPException(status_code=400, detail="逐字稿与视频的对应关系无效，请重新选择逐字稿")
    transcript_records: dict[int, tuple[UploadFile, bytes, ParsedTranscriptDocument]] = {}
    for upload, video_index in zip(uploaded_transcripts, transcript_indices):
        if video_index < 0 or video_index >= len(videos):
            raise HTTPException(status_code=400, detail="逐字稿对应的视频不存在")
        if video_index in transcript_records:
            raise HTTPException(status_code=400, detail=f"视频 {video_index + 1} 只能绑定一份逐字稿")
        raw_document, parsed_document = await _read_transcript_upload(upload)
        transcript_records[video_index] = (upload, raw_document, parsed_document)

    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    legacy_title = title.strip()
    base_content_title = content_title.strip()
    base_cover_title = cover_title.strip() or legacy_title
    per_item_titles = [item.strip() for item in item_titles]
    per_item_content_titles = [item.strip() for item in item_content_titles]
    per_item_cover_titles = [item.strip() for item in item_cover_titles]
    today = datetime.now().strftime("%Y%m%d")
    items = []
    for index, upload in enumerate(videos, start=1):
        item_id = f"{index:03d}"
        item_input_dir = input_dir / item_id
        item_output_dir = output_dir / item_id
        item_input_dir.mkdir(parents=True, exist_ok=True)
        item_output_dir.mkdir(parents=True, exist_ok=True)
        transcript_record = transcript_records.get(index - 1)
        parsed_document = transcript_record[2] if transcript_record else None
        legacy_item_title = per_item_titles[index - 1] if index - 1 < len(per_item_titles) else ""
        explicit_content_title = (
            per_item_content_titles[index - 1] if index - 1 < len(per_item_content_titles) else ""
        )
        explicit_cover_title = (
            per_item_cover_titles[index - 1] if index - 1 < len(per_item_cover_titles) else ""
        )
        filename_title = Path(upload.filename or f"video-{index}").stem
        video_content_title = (
            explicit_content_title
            or (parsed_document.content_title if parsed_document else "")
            or base_content_title
            or legacy_item_title
            or legacy_title
            or filename_title
        )
        video_cover_title = (
            explicit_cover_title
            or (parsed_document.cover_title if parsed_document else "")
            or base_cover_title
            or video_content_title
        )
        output_basename = _safe_output_basename(f"{video_cover_title}-{today}")
        video_path = item_input_dir / _safe_video_name(upload.filename or f"video-{index}.mp4", index=index)
        _save_uploaded_video(upload, video_path)
        if not video_path.exists() or video_path.stat().st_size == 0:
            raise HTTPException(status_code=400, detail=f"{upload.filename} 为空或保存失败")
        content_title_path = item_input_dir / "content_title.txt"
        cover_title_path = item_input_dir / "cover_title.txt"
        title_path = item_input_dir / "title.txt"
        content_title_path.write_text(video_content_title, encoding="utf-8")
        cover_title_path.write_text(video_cover_title, encoding="utf-8")
        title_path.write_text(video_content_title, encoding="utf-8")
        transcript_path = ""
        transcript_document_path = ""
        transcript_warnings: list[str] = []
        transcript_source = ""
        if transcript_record and parsed_document:
            transcript_upload, raw_document, parsed_document = transcript_record
            suffix = Path(transcript_upload.filename or "").suffix.lower()
            original_path = item_input_dir / f"transcript{suffix}"
            original_path.write_bytes(raw_document)
            normalized_path = item_input_dir / "script.txt"
            normalized_path.write_text(parsed_document.transcript, encoding="utf-8")
            transcript_path = str(normalized_path)
            transcript_document_path = str(original_path)
            transcript_warnings = list(parsed_document.warnings)
            transcript_source = parsed_document.transcript_source
        items.append(
            {
                "id": item_id,
                "source_name": upload.filename or video_path.name,
                "title": video_content_title,
                "content_title": video_content_title,
                "cover_title": video_cover_title,
                "video": str(video_path),
                "title_path": str(title_path),
                "content_title_path": str(content_title_path),
                "cover_title_path": str(cover_title_path),
                "transcript_path": transcript_path,
                "transcript_document_path": transcript_document_path,
                "transcript_source": transcript_source,
                "transcript_warnings": transcript_warnings,
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
        "auto_edit_mode": PRESETS[preset]["auto_edit_mode"],
        "style_preset_id": style_preset["id"],
        "style_preset_name": style_preset["name"],
        "subtitle_delay": float(settings.get("subtitle_delay", 0.0)),
        "detect_disfluency": bool(settings.get("detect_disfluency", False)),
        "llm_enabled": bool(settings.get("llm_enabled", False)),
        "llm_base_url": str(settings.get("llm_base_url") or ""),
        "llm_model": str(settings.get("llm_model") or ""),
        "export_subtitles": bool(export_subtitles),
        "export_asr_json": bool(export_asr_json),
        "export_report": bool(export_report),
        "title": base_content_title or (items[0]["title"] if len(items) == 1 else f"批量任务 {len(items)} 个视频"),
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
    if settings.get("volc_app_id") or settings.get("volc_access_token") or settings.get("llm_api_key"):
        JOB_SECRETS[job_id] = {
            "VOLC_APP_ID": settings.get("volc_app_id", ""),
            "VOLC_ACCESS_TOKEN": settings.get("volc_access_token", ""),
            "AI_CUTTING_LLM_API_KEY": settings.get("llm_api_key", ""),
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
            "display_error": _friendly_job_error(job.get("error")),
        },
    )


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = _read_job(job_id)
    job["files"] = _output_files(job_id)
    return job


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str) -> RedirectResponse:
    if job_id in ACTIVE_JOB_IDS:
        raise HTTPException(status_code=409, detail="任务仍在运行，不能重复启动")
    job_dir = _job_path(job_id)
    job = _load_job(job_dir)
    if job.get("status") not in {"failed", "queued"}:
        raise HTTPException(status_code=409, detail="只有失败或中断的任务可以重新处理")
    settings = _load_settings()
    runtime = _runtime_status(settings)
    if not runtime["ffmpeg_ready"]:
        raise HTTPException(status_code=400, detail=f"视频运行环境不可用：{runtime.get('ffmpeg_error') or 'FFmpeg 自检失败'}")
    volc_app_id = settings.get("volc_app_id") or os.environ.get("VOLC_APP_ID") or ""
    volc_token = settings.get("volc_access_token") or os.environ.get("VOLC_ACCESS_TOKEN") or ""
    if not volc_app_id or not volc_token:
        raise HTTPException(status_code=400, detail="重新处理前请先在设置页配置火山 APP ID 和 Access Token")

    pending = 0
    for item in job.get("params", {}).get("items", []):
        if item.get("status") == "done" and item.get("outputs"):
            continue
        pending += 1
        item["status"] = "queued"
        item["error"] = None
        item["outputs"] = {}
        for raw_path in (item.get("output_dir"), str(job_dir / "work" / str(item.get("id") or "001"))):
            if not raw_path:
                continue
            target = Path(raw_path).resolve()
            if target.is_relative_to(job_dir.resolve()):
                shutil.rmtree(target, ignore_errors=True)
                target.mkdir(parents=True, exist_ok=True)
    if not pending:
        raise HTTPException(status_code=409, detail="这个任务没有需要重新处理的视频")

    job["status"] = "queued"
    job["stage"] = "queued"
    job["error"] = None
    job["updated_at"] = _now()
    job.setdefault("log", []).append({"time": _now(), "message": f"用户重新处理 {pending} 个未完成视频"})
    _write_job(job_dir, job)
    JOB_SECRETS[job_id] = {
        "VOLC_APP_ID": str(volc_app_id),
        "VOLC_ACCESS_TOKEN": str(volc_token),
        "AI_CUTTING_LLM_API_KEY": str(settings.get("llm_api_key") or ""),
    }
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, job_id: str, item: str = "001") -> HTMLResponse:
    job_dir = _job_path(job_id)
    job = _read_job(job_id)
    project = _load_or_create_edit_project(job_dir, job, item)
    video_path = Path(project["current_video"])
    style_preset = get_style_preset(project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    editor_assets = _safe_editor_assets(job_dir, project)
    project = dict(project)
    video_info = _editor_video_info(video_path)
    for sentence in project.get("sentences", []):
        sentence["overlong"] = text_overflows(
            str(sentence.get("text") or ""),
            style_preset.get("subtitle") or {},
            int(video_info.get("width") or 1080),
            int(video_info.get("height") or 1920),
        )
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
            "video_info": video_info,
            "editor_assets": editor_assets,
            "style_presets": load_style_presets(STYLE_PRESETS_PATH),
            "subtitle_style": style_preset.get("subtitle") or {},
            "title_style": style_preset.get("video_title") or style_preset.get("cover_title") or {},
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
        _write_training_feedback(job_dir, existing, project)
        return {"ok": True, "project": project}
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "save-edit-project", exc)
        raise HTTPException(status_code=500, detail="保存精修项目失败，诊断信息已写入任务目录") from exc


@app.post("/api/jobs/{job_id}/edit-preview")
async def api_render_edit_preview(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    try:
        job = _read_job(job_id)
        existing = _load_or_create_edit_project(job_dir, job, item)
        payload = await _read_json_object(request, source="生成时间线预览", required=True)
        project = _sanitize_edit_project({**existing, **payload}, existing)
        source = Path(project.get("render_source_video") or project.get("current_video") or "")
        if not source.exists():
            raise HTTPException(status_code=400, detail="找不到时间线预览源视频")

        clips = _timeline_clips(project, float(project.get("duration") or media_duration(source)))
        signature = _timeline_preview_signature(project)
        safe_item = _safe_path_segment(project.get("item_id") or item) or "001"
        preview_dir = job_dir / "work" / "editor_previews" / safe_item
        preview_dir.mkdir(parents=True, exist_ok=True)
        output = preview_dir / f"timeline-{signature}.mp4"
        if not output.exists() or output.stat().st_size < 1024:
            temporary = preview_dir / f".{output.stem}-{uuid.uuid4().hex}.mp4"
            try:
                await asyncio.to_thread(_render_timeline_preview, source, temporary, clips)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
        _prune_timeline_previews(preview_dir, keep=output)
        return {
            "ok": True,
            "signature": signature,
            "url": _media_url_for_path(job_dir, output),
            "duration": round(sum(float(clip["segment"].duration) for clip in clips), 3),
        }
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "render-edit-preview", exc)
        raise HTTPException(status_code=500, detail="生成时间线预览失败，诊断信息已写入任务目录") from exc


@app.post("/api/jobs/{job_id}/reanalyze-subtitles")
async def api_reanalyze_subtitles(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    try:
        existing = _load_or_create_edit_project(job_dir, _read_job(job_id), item)
        payload = await _read_json_object(request, source="重新智能分析", required=False)
        project = _sanitize_edit_project({**existing, **payload}, existing) if payload else existing
        settings = _load_settings()
        tokens = [token for sentence in project.get("sentences", []) for token in sentence.get("tokens", [])]
        analysis = await asyncio.to_thread(
            analyze_transcript,
            tokens,
            base_url=str(settings.get("llm_base_url") or ""),
            model=str(settings.get("llm_model") or ""),
            api_key=str(settings.get("llm_api_key") or ""),
            timeout=60.0,
        )
        if analysis.get("status") != "ok":
            previous = project.get("analysis") if isinstance(project.get("analysis"), dict) else {}
            if previous.get("status") == "ok":
                return {
                    "ok": False,
                    "preserved": True,
                    "analysis": previous,
                    "warning": f"本次智能分析失败：{analysis.get('reason') or 'unknown'}，已保留上一次成功结果。",
                }
            project["analysis"] = analysis
            _write_edit_project(job_dir, project)
            return {"ok": False, "preserved": False, "analysis": analysis}
        apply_high_confidence_corrections(tokens, analysis)
        repeat_ids = {
            str(token_id)
            for candidate in analysis.get("repeat_candidates", [])
            for token_id in candidate.get("token_ids", [])
        }
        for sentence in project.get("sentences", []):
            sentence["text"] = "".join(str(token.get("text") or "") for token in sentence.get("tokens", [])).strip()
            sentence["review_flags"] = ["repeat_candidate"] if any(str(token.get("id")) in repeat_ids for token in sentence.get("tokens", [])) else []
        project["analysis"] = analysis
        _write_edit_project(job_dir, project)
        return {"ok": True, "analysis": analysis, "project": project}
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "reanalyze-subtitles", exc)
        raise HTTPException(status_code=500, detail="重新智能分析失败，诊断信息已写入任务目录") from exc


@app.post("/api/jobs/{job_id}/reflow-subtitles")
async def api_reflow_subtitles(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    try:
        existing = _load_or_create_edit_project(job_dir, _read_job(job_id), item)
        payload = await _read_json_object(request, source="按预设重新断句", required=False)
        project = _sanitize_edit_project({**existing, **payload}, existing) if payload else existing
        source = Path(project.get("render_source_video") or project.get("current_video") or "")
        width, height = video_size(source)
        preset = get_style_preset(project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
        settings = _load_settings()
        project["sentences"] = await asyncio.to_thread(
            _reflow_project_sentences,
            project,
            preset.get("subtitle") or {},
            width,
            height,
            {
                "base_url": str(settings.get("llm_base_url") or ""),
                "model": str(settings.get("llm_model") or ""),
                "api_key": str(settings.get("llm_api_key") or ""),
                "timeout": float(settings.get("llm_timeout") or 60.0),
            },
        )
        _write_edit_project(job_dir, project)
        return {"ok": True, "project": project}
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "reflow-subtitles", exc)
        raise HTTPException(status_code=500, detail="按预设重新断句失败，诊断信息已写入任务目录") from exc


@app.post("/api/jobs/{job_id}/cover-preview")
async def api_cover_preview(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    try:
        existing = _load_or_create_edit_project(job_dir, _read_job(job_id), item)
        payload = await _read_json_object(request, source="生成封面预览", required=True)
        project = _sanitize_edit_project({**existing, **payload}, existing)
        source = Path(project.get("render_source_video") or project.get("current_video") or "")
        if not source.exists():
            raise HTTPException(status_code=400, detail="找不到封面预览源视频")
        cover = project.get("cover") or {}
        preset = get_style_preset(cover.get("style_preset_id") or project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
        style = dict(preset.get("cover_title") or {})
        if isinstance(cover.get("style_override"), dict):
            style.update(cover["style_override"])
        signature_payload = json.dumps({"cover": cover, "sentences": project.get("sentences", [])}, ensure_ascii=True, sort_keys=True)
        signature = hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()[:16]
        safe_item = _safe_path_segment(project.get("item_id") or item) or "001"
        output = job_dir / "work" / "cover_previews" / safe_item / f"cover-{signature}.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        clips = _timeline_clips(project, float(project.get("duration") or media_duration(source)))
        source_time = _timeline_source_time(clips, float(cover.get("frame_time") or 0.0))
        if not output.exists():
            await asyncio.to_thread(make_cover, source, str(cover.get("text") or ""), output, style, source_time)
        return {"ok": True, "url": _media_url_for_path(job_dir, output), "signature": signature}
    except HTTPException:
        raise
    except Exception as exc:
        _write_web_traceback(job_dir, "cover-preview", exc)
        raise HTTPException(status_code=500, detail="生成封面预览失败，诊断信息已写入任务目录") from exc


@app.post("/api/jobs/{job_id}/cover-style-preset")
async def api_save_cover_style_preset(job_id: str, request: Request, item: str = "001") -> dict[str, Any]:
    job_dir = _job_path(job_id)
    existing = _load_or_create_edit_project(job_dir, _read_job(job_id), item)
    payload = await _read_json_object(request, source="保存封面预设", required=True)
    project = _sanitize_edit_project({**existing, **payload}, existing)
    cover = project.get("cover") or {}
    source_preset = get_style_preset(cover.get("style_preset_id") or project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    new_preset = dict(source_preset)
    new_preset["id"] = f"cover-{uuid.uuid4().hex[:8]}"
    new_preset["name"] = f"{str(cover.get('text') or '新封面')[:10]}封面"
    new_preset["cover_title"] = {**source_preset.get("cover_title", {}), **(cover.get("style_override") or {})}
    presets = load_style_presets(STYLE_PRESETS_PATH)
    presets.append(new_preset)
    save_style_presets(presets, STYLE_PRESETS_PATH)
    project["cover"]["style_preset_id"] = new_preset["id"]
    project["cover"]["style_override"] = {}
    _write_edit_project(job_dir, project)
    return {"ok": True, "preset": new_preset, "project": project}


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
    ACTIVE_JOB_IDS.add(job_id)

    try:
        _update_job(job_dir, status="running", stage="running", message="开始处理")
        _append_log(job_dir, f"Python: {PYTHON}")
        _append_log(job_dir, f"工作目录: {ROOT}")
        _append_log(job_dir, "字幕源: 火山引擎")
        _append_log(job_dir, f"样式预设: {params.get('style_preset_id') or 'default-white'}")
        env = _augment_tool_path(os.environ.copy())
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
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
            if item.get("status") == "done" and item.get("outputs"):
                _append_log(job_dir, f"跳过已完成的视频: {item['title']}")
                continue
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
                "--content-title",
                item.get("content_title_path") or item["title_path"],
                "--cover-title",
                item.get("cover_title_path") or item["title_path"],
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
                "--auto-edit-mode",
                params.get("auto_edit_mode") or "standard",
            ]
            if item.get("transcript_path"):
                cmd.extend(["--script", item["transcript_path"]])
            if params["detect_disfluency"]:
                cmd.append("--detect-disfluency")
            if params.get("llm_enabled"):
                cmd.extend(
                    [
                        "--llm-enabled",
                        "--llm-base-url",
                        params.get("llm_base_url") or "",
                        "--llm-model",
                        params.get("llm_model") or "",
                    ]
                )
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
    finally:
        ACTIVE_JOB_IDS.discard(job_id)


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save_uploaded_video(upload: UploadFile, target: Path) -> None:
    written = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{upload.filename} 超过单文件 4 GB 限制",
                    )
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise


async def _read_transcript_upload(upload: UploadFile) -> tuple[bytes, ParsedTranscriptDocument]:
    suffix = Path(upload.filename or "").suffix.lower()
    raw = await upload.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise HTTPException(status_code=413, detail=f"{upload.filename} 超过逐字稿 2 MB 限制")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{upload.filename} 不是 UTF-8 编码") from exc
    try:
        parsed = parse_transcript_document(text, suffix, upload.filename or "")
    except TranscriptDocumentError as exc:
        raise HTTPException(status_code=400, detail=f"{upload.filename}：{exc}") from exc
    return raw, parsed


def _transcript_preview_payload(parsed: ParsedTranscriptDocument, filename: str) -> dict[str, Any]:
    return {
        "ok": True,
        "filename": filename,
        "content_title": parsed.content_title,
        "cover_title": parsed.cover_title,
        "transcript_source": parsed.transcript_source,
        "transcript_length": len(re.sub(r"\s+", "", parsed.transcript)),
        "warnings": list(parsed.warnings),
    }


def _safe_video_name(name: str, index: int = 1) -> str:
    suffix = Path(name).suffix.lower() or ".mp4"
    if suffix not in SUPPORTED_VIDEO_SUFFIXES:
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
                _annotate_sentence_break_sources(saved_project["sentences"], saved_project.get("analysis", {}))
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
            "tokens": list(segment.get("tokens") or tokens_from_text(
                str(segment.get("text") or ""),
                float(segment["start"]),
                float(segment["end"]),
                prefix=f"s{index:03d}",
            )),
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
    _annotate_sentence_break_sources(project["sentences"], project.get("analysis", {}))
    repeat_ids = {
        str(token_id)
        for candidate in project.get("analysis", {}).get("repeat_candidates", [])
        for token_id in candidate.get("token_ids", [])
    }
    for sentence in project["sentences"]:
        sentence["review_flags"] = ["repeat_candidate"] if any(str(token.get("id")) in repeat_ids for token in sentence.get("tokens", [])) else []
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
    title_layout_path = job_dir / "work" / item["id"] / "title_layout.json"
    title_layout: dict[str, Any] = {}
    if title_layout_path.exists():
        try:
            loaded_title_layout = read_json_file(title_layout_path)
            if isinstance(loaded_title_layout, dict):
                title_layout = loaded_title_layout
        except RuntimeError:
            pass
    video_title_text = str(title_layout.get("video_text") or title)
    cover_title_text = str(title_layout.get("cover_text") or title)
    preset = get_style_preset(params.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    content_title_style = preset.get("video_title") or {}
    content_title_enabled = bool(content_title_style.get("enabled", False))
    content_title_end = duration
    if content_title_style.get("display_mode") == "intro":
        content_title_end = min(duration, max(0.2, float(content_title_style.get("display_duration", 3.0))))
    return {
        "version": 4,
        "job_id": job.get("id"),
        "item_id": item["id"],
        "title": {"cover_text": cover_title_text, "video_text": video_title_text if content_title_enabled else "", "show_video_title": content_title_enabled},
        "cover": {
            "text": cover_title_text,
            "frame_time": min(max(duration * 0.2, 0.0), max(0.0, duration - 0.05)),
            "style_preset_id": params.get("style_preset_id") or "default-white",
            "style_override": {},
        },
        "title_clips": [
            {
                "id": "t001",
                "start": 0.0,
                "end": max(0.2, content_title_end),
                "text": video_title_text if content_title_enabled else "",
                "enabled": content_title_enabled,
                "use_for_cover": False,
                "style_override": {},
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
        "analysis": _load_transcript_analysis(job_dir, item["id"]),
        "outputs": {},
        "created_at": _now(),
        "updated_at": _now(),
    }


def _sanitize_edit_project(project: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(existing)
    duration = max(0.0, float(project.get("duration") or existing.get("duration") or 0.0))
    cleaned["version"] = 4
    cleaned["duration"] = duration
    title = project.get("title") if isinstance(project.get("title"), dict) else {}
    old_title = existing.get("title") if isinstance(existing.get("title"), dict) else {}
    cleaned["title"] = {
        "cover_text": str(title.get("cover_text", old_title.get("cover_text", "")))[:800],
        "video_text": str(title.get("video_text", old_title.get("video_text", "")))[:800],
        "show_video_title": bool(title.get("show_video_title", old_title.get("show_video_title", False))),
    }
    cover = project.get("cover") if isinstance(project.get("cover"), dict) else {}
    old_cover = existing.get("cover") if isinstance(existing.get("cover"), dict) else {}
    cleaned["cover"] = {
        "text": str(cover.get("text", old_cover.get("text", cleaned["title"]["cover_text"])))[:800],
        "frame_time": _clamp_float(cover.get("frame_time", old_cover.get("frame_time", 0.0)), 0.0, duration),
        "style_preset_id": str(cover.get("style_preset_id") or old_cover.get("style_preset_id") or project.get("style_preset_id") or "default-white"),
        "style_override": cover.get("style_override") if isinstance(cover.get("style_override"), dict) else old_cover.get("style_override", {}),
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
    if isinstance(project.get("analysis"), dict):
        cleaned["analysis"] = project["analysis"]
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
        incoming_tokens = item.get("tokens") if isinstance(item.get("tokens"), list) else original.get("tokens", [])
        tokens = _sanitize_tokens(incoming_tokens, text, start, end, sentence_id)
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
                "tokens": tokens,
                "timing_pending": bool(item.get("timing_pending", original.get("timing_pending", False))),
                "review_flags": item.get("review_flags") if isinstance(item.get("review_flags"), list) else original.get("review_flags", []),
                "layout_source": str(item.get("layout_source") or original.get("layout_source") or "")[:40],
                "layout_reason": str(item.get("layout_reason") or original.get("layout_reason") or "")[:300],
                "style_override": item.get("style_override") if isinstance(item.get("style_override"), dict) else original.get("style_override", {}),
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


def _sanitize_tokens(
    items: list[Any], text: str, start: float, end: float, sentence_id: str
) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("text") or ""):
            continue
        token_start = _clamp_float(item.get("start", start), start, end)
        token_end = _clamp_float(item.get("end", token_start), token_start, end)
        tokens.append(
            {
                "id": str(item.get("id") or f"{sentence_id}-w{index:04d}")[:80],
                "text": str(item.get("text") or "")[:200],
                "original_text": str(item.get("original_text") or item.get("text") or "")[:200],
                "start": round(token_start, 3),
                "end": round(max(token_start, token_end), 3),
                "timing_source": str(item.get("timing_source") or "estimated")[:40],
                "edited": bool(item.get("edited", False)),
            }
        )
    joined = "".join(token["text"] for token in tokens).strip()
    if not tokens or joined != text.replace(" ", "").strip():
        return tokens_from_text(text, start, end, prefix=sentence_id)
    return tokens


def _reflow_project_sentences(
    project: dict[str, Any],
    style: dict[str, Any],
    width: int,
    height: int,
    llm: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []
    layout_chunks: list[dict[str, Any]] = []

    def flush() -> None:
        if not run:
            return
        tokens = [dict(token) for sentence in run for token in sentence.get("tokens", [])]
        preferred, required, forbidden = analysis_break_sets(tokens, project.get("analysis", {}))
        if llm and llm.get("api_key"):
            groups, layout_audit = layout_tokens_with_ai(
                tokens,
                style,
                width,
                height,
                project.get("analysis", {}),
                base_url=str(llm.get("base_url") or ""),
                model=str(llm.get("model") or ""),
                api_key=str(llm.get("api_key") or ""),
                timeout=float(llm.get("timeout") or 60.0),
                cache_dir=LLM_CACHE_DIR,
            )
            layout_chunks.extend(layout_audit.get("chunks", []))
        else:
            groups = segment_tokens(tokens, style, width, height, preferred, required, forbidden)
        run_start = float(run[0].get("clip_start", run[0].get("start", 0)))
        run_end = float(run[-1].get("clip_end", run[-1].get("end", 0)))
        for index, group in enumerate(groups):
            visible = [token for token in group if str(token.get("text") or "")]
            if not visible:
                continue
            next_start = float(groups[index + 1][0].get("start", run_end)) if index + 1 < len(groups) else run_end
            sentence_id = f"r{hashlib.sha1('|'.join(str(token.get('id')) for token in visible).encode('utf-8')).hexdigest()[:10]}"
            text = "".join(str(token.get("text") or "") for token in visible).strip()
            result.append(
                {
                    "id": sentence_id,
                    "start": round(float(visible[0].get("start", run_start)), 3),
                    "end": round(float(visible[-1].get("end", next_start)), 3),
                    "clip_start": round(run_start if index == 0 else float(visible[0].get("start", run_start)), 3),
                    "clip_end": round(next_start, 3),
                    "timeline_order": len(result) + 1,
                    "original_text": text,
                    "text": text,
                    "enabled": True,
                    "remove_video": False,
                    "gap": False,
                    "synthetic_gap": False,
                    "edited": False,
                    "tokens": visible,
                    "timing_pending": False,
                    "review_flags": [],
                    "layout_source": "",
                    "layout_reason": "",
                }
            )
        run.clear()

    for sentence in project.get("sentences", []):
        barrier = bool(
            sentence.get("edited")
            or sentence.get("timing_pending")
            or sentence.get("remove_video")
            or sentence.get("gap")
        )
        if barrier:
            flush()
            result.append(dict(sentence))
        else:
            run.append(sentence)
    flush()
    if layout_chunks:
        project.setdefault("analysis", {})["layout_decision"] = {
            "status": "ai" if all(item.get("status") == "ai" for item in layout_chunks) else "mixed",
            "chunks": layout_chunks,
        }
    for index, sentence in enumerate(result, start=1):
        sentence["timeline_order"] = index
    _annotate_sentence_break_sources(result, project.get("analysis", {}))
    return result


def _annotate_sentence_break_sources(sentences: list[dict[str, Any]], analysis: dict[str, Any]) -> None:
    tokens = [token for sentence in sentences for token in sentence.get("tokens", [])]
    preferred, required, _ = analysis_break_sets(tokens, analysis)
    reason_by_id = {
        str(item.get("after_token_id") or ""): str(item.get("reason") or "")
        for key in ("break_hints", "allowed_breaks")
        for item in analysis.get(key, [])
    }
    ai_final_ends = {
        str(sentence.get("token_ids", [])[-1])
        for chunk in analysis.get("layout_decision", {}).get("chunks", [])
        if chunk.get("status") == "ai"
        for sentence in chunk.get("sentences", [])
        if sentence.get("token_ids")
    }
    for sentence in sentences:
        sentence_tokens = sentence.get("tokens", [])
        last_id = str(sentence_tokens[-1].get("id") or "") if sentence_tokens else ""
        if last_id in ai_final_ends:
            sentence["layout_source"] = "ai_final"
            sentence["layout_reason"] = "AI 根据真实像素宽度返回的最终断句"
        elif last_id in required:
            sentence["layout_source"] = "ai_sentence"
            sentence["layout_reason"] = "AI 完整语义句边界"
        elif last_id in preferred:
            sentence["layout_source"] = "ai_break"
            sentence["layout_reason"] = reason_by_id.get(last_id) or "AI 建议的自然断点"
        else:
            sentence["layout_source"] = "width"
            sentence["layout_reason"] = "根据当前预设的真实文字宽度适配"


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
                "text": title.get("video_text") or "",
                "enabled": bool(title.get("show_video_title")),
                "use_for_cover": False,
                "style_override": {},
            }
        ]
    cleaned: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        clip_id = _safe_clip_id(str(item.get("id") or f"t{index:03d}"), "t", index, used_ids)
        original = old_by_id.get(clip_id, {})
        legacy_cover_link = bool(item.get("use_for_cover", original.get("use_for_cover", False)))
        start = _clamp_float(item.get("start", original.get("start", 0.0)), 0.0, duration)
        end = _clamp_float(item.get("end", original.get("end", min(duration, start + 3.0))), 0.0, duration)
        if end <= start:
            end = min(duration, start + 0.2)
        text = str(item.get("text", original.get("text", title.get("video_text", ""))))[:800]
        enabled = bool(item.get("enabled", original.get("enabled", False)))
        if legacy_cover_link:
            text = ""
            enabled = False
            title["video_text"] = ""
            title["show_video_title"] = False
        cleaned.append(
            {
                "id": clip_id,
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "enabled": enabled,
                "use_for_cover": False,
                "style_override": item.get("style_override") if isinstance(item.get("style_override"), dict) else original.get("style_override", {}),
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


def _write_training_feedback(job_dir: Path, initial: dict[str, Any], final: dict[str, Any]) -> None:
    item_id = str(final.get("item_id") or "001")
    work_dir = job_dir / "work" / item_id
    work_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = work_dir / "auto_edit_baseline.json"
    feedback_path = work_dir / "training_feedback.json"
    if baseline_path.exists():
        try:
            baseline = read_json_file(baseline_path)
        except RuntimeError:
            baseline = _feedback_project_snapshot(initial)
    else:
        baseline = _feedback_project_snapshot(initial)
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    raw_transcript: list[dict[str, Any]] = []
    for candidate in (work_dir / "raw_transcript_segments.json", work_dir / "volcengine_segments.json"):
        if candidate.exists():
            try:
                value = read_json_file(candidate)
                if isinstance(value, list):
                    raw_transcript = value
                    break
            except RuntimeError:
                continue
    analysis: dict[str, Any] = {}
    analysis_path = work_dir / "transcript_analysis.json"
    if analysis_path.exists():
        try:
            value = read_json_file(analysis_path)
            if isinstance(value, dict):
                analysis = value
        except RuntimeError:
            pass
    edit_plan: dict[str, Any] = {}
    edit_plan_path = work_dir / "auto_edit_plan.json"
    if edit_plan_path.exists():
        try:
            value = read_json_file(edit_plan_path)
            if isinstance(value, dict):
                edit_plan = value
        except RuntimeError:
            pass
    final_snapshot = _feedback_project_snapshot(final)
    before = {item["id"]: item for item in baseline.get("sentences", [])}
    after = {item["id"]: item for item in final_snapshot.get("sentences", [])}
    feedback = {
        "version": 1,
        "job_id": job_dir.name,
        "item_id": item_id,
        "updated_at": _now(),
        "raw_transcript": raw_transcript,
        "ai_decision": analysis,
        "auto_edit_plan": edit_plan,
        "initial_result": baseline,
        "final_result": final_snapshot,
        "user_changes": {
            "restored_sentence_ids": [key for key, item in before.items() if item.get("remove_video") and not after.get(key, {}).get("remove_video")],
            "removed_sentence_ids": [key for key, item in after.items() if item.get("remove_video") and not before.get(key, {}).get("remove_video")],
            "text_edits": [
                {"sentence_id": key, "before": before.get(key, {}).get("text", ""), "after": item.get("text", "")}
                for key, item in after.items()
                if key in before and item.get("text") != before[key].get("text")
            ],
        },
    }
    feedback_path.write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8")


def _feedback_project_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "style_preset_id": str(project.get("style_preset_id") or ""),
        "sentences": [
            {
                "id": str(item.get("id") or ""),
                "start": float(item.get("start") or 0.0),
                "end": float(item.get("end") or 0.0),
                "text": str(item.get("text") or ""),
                "original_text": str(item.get("original_text") or ""),
                "enabled": bool(item.get("enabled", True)),
                "remove_video": bool(item.get("remove_video", False)),
                "timeline_order": int(item.get("timeline_order") or 0),
            }
            for item in project.get("sentences", [])
            if isinstance(item, dict) and not item.get("synthetic_gap")
        ],
    }


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
    cover = project.get("cover") if isinstance(project.get("cover"), dict) else {}
    cover_title = cover.get("text") or project.get("title", {}).get("cover_text") or item.get("title") or "精修视频"
    output_video = edited_dir / f"{_safe_output_basename(cover_title)}-精修.mp4"
    burn_subtitles(no_subtitle, ass_path, output_video)
    cover_path = edited_dir / f"{_safe_output_basename(cover_title)}-精修封面.jpg"
    cover_preset = get_style_preset(cover.get("style_preset_id") or project.get("style_preset_id") or "default-white", STYLE_PRESETS_PATH)
    cover_style = dict(cover_preset.get("cover_title") or {})
    if isinstance(cover.get("style_override"), dict):
        cover_style.update(cover["style_override"])
    make_cover(
        no_subtitle,
        cover_title,
        cover_path,
        cover_style,
        frame_time=float(cover.get("frame_time") or 0.0),
    )
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
        cues.append(SubtitleCue(index=len(cues) + 1, start=mapped_start, end=mapped_end, text=str(sentence.get("text") or ""), style=sentence.get("style_override") or None))
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
                style=clip.get("style_override") or None,
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


def _timeline_source_time(clips: list[dict[str, Any]], timeline_time: float) -> float:
    cursor = 0.0
    for clip in clips:
        segment = clip["segment"]
        if cursor <= timeline_time <= cursor + segment.duration:
            if _clip_is_gap(clip):
                return max(0.0, segment.start)
            return max(segment.start, min(segment.end, segment.start + timeline_time - cursor))
        cursor += segment.duration
    return clips[-1]["segment"].end if clips else 0.0


def _clip_is_gap(clip: dict[str, Any]) -> bool:
    sentence = clip.get("sentence") if isinstance(clip.get("sentence"), dict) else {}
    return bool(sentence.get("gap")) and not bool(sentence.get("remove_video"))


def _coalesced_render_clips(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for clip in clips:
        segment = clip["segment"]
        current = {"sentence": clip.get("sentence") or {}, "segment": Segment(segment.start, segment.end)}
        if not merged or _clip_is_gap(current) or _clip_is_gap(merged[-1]):
            merged.append(current)
            continue
        previous = merged[-1]
        previous_segment = previous["segment"]
        if abs(float(previous_segment.end) - float(segment.start)) <= 0.04:
            previous["segment"] = Segment(previous_segment.start, max(previous_segment.end, segment.end))
        else:
            merged.append(current)
    return merged


def _render_timeline_video(source: Path, output: Path, clips: list[dict[str, Any]]) -> None:
    clips = _coalesced_render_clips(clips)
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


def _timeline_preview_signature(project: dict[str, Any]) -> str:
    timeline = []
    for sentence in sorted(
        project.get("sentences", []),
        key=lambda item: (float(item.get("timeline_order", 0)), str(item.get("id") or "")),
    ):
        timeline.append(
            {
                "id": str(sentence.get("id") or ""),
                "order": round(float(sentence.get("timeline_order", 0)), 4),
                "start": round(float(sentence.get("clip_start", sentence.get("start", 0))), 3),
                "end": round(float(sentence.get("clip_end", sentence.get("end", 0))), 3),
                "removed": bool(sentence.get("remove_video")),
                "gap": bool(sentence.get("gap")),
                "synthetic_gap": bool(sentence.get("synthetic_gap")),
            }
        )
    payload = "v2:" + json.dumps(timeline, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _render_timeline_preview(source: Path, output: Path, clips: list[dict[str, Any]]) -> None:
    clips = _coalesced_render_clips(clips)
    width, height = video_size(source)
    target_width = max(2, min(width, 720))
    target_width -= target_width % 2
    target_height = max(2, round(height * target_width / max(width, 1)))
    target_height -= target_height % 2
    parts: list[str] = []
    labels: list[str] = []
    for index, clip in enumerate(clips):
        segment = clip["segment"]
        duration = max(0.05, float(segment.duration))
        if _clip_is_gap(clip):
            parts.append(
                f"color=c=black:s={target_width}x{target_height}:r=30:d={duration:.3f},"
                f"format=yuv420p,setsar=1[v{index}]"
            )
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=44100:d={duration:.3f},"
                f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{index}]"
            )
        else:
            parts.append(
                f"[0:v]trim=start={segment.start:.3f}:end={segment.end:.3f},"
                f"setpts=PTS-STARTPTS,scale={target_width}:{target_height},fps=30,format=yuv420p,setsar=1[v{index}]"
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
            "ultrafast",
            "-crf",
            "27",
            "-g",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def _prune_timeline_previews(preview_dir: Path, keep: Path) -> None:
    candidates = sorted(
        (path for path in preview_dir.glob("timeline-*.mp4") if path != keep),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[2:]:
        path.unlink(missing_ok=True)


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
                cues.append(SubtitleCue(index=len(cues) + 1, start=cue_start, end=cue_end, text=str(sentence.get("text") or ""), style=sentence.get("style_override") or None))
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
        cues.append(SubtitleCue(index=len(cues) + 1, start=start, end=end, text=str(clip.get("text") or ""), style=clip.get("style_override") or None))
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
    title_style.update(preset.get("video_title") or preset.get("cover_title") or {})
    title_style.setdefault("position_x", 0)
    title_style.setdefault("position_y", -520)
    title_style.setdefault("text_align", "center")
    title_style.setdefault("opacity", 100)
    subtitle_styles = [({**subtitle_style, **(cue.style or {})}, f"Subtitle{cue.index}" if cue.style else "Subtitle") for cue in subtitle_cues]
    title_styles = [({**title_style, **(cue.style or {})}, f"Title{cue.index}" if cue.style else "Title") for cue in title_cues]
    style_lines = [
        subtitle_to_ass_style(subtitle_style, width=width, height=height).replace("Style: Default,", "Style: Subtitle,", 1),
        subtitle_to_ass_style(title_style, width=width, height=height).replace("Style: Default,", "Style: Title,", 1),
    ]
    style_lines.extend(
        subtitle_to_ass_style(style, width=width, height=height).replace("Style: Default,", f"Style: {name},", 1)
        for style, name in [*subtitle_styles, *title_styles]
        if name not in {"Subtitle", "Title"}
    )
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
        *style_lines,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for cue, (cue_style, style_name) in zip(subtitle_cues, subtitle_styles):
        override = subtitle_override(cue_style, cue.start, cue.end, width=width, height=height)
        lines.append(f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},{style_name},,0,0,0,,{override}{_ass_text(cue.text)}")
    for cue, (cue_style, style_name) in zip(title_cues, title_styles):
        override = subtitle_override(cue_style, cue.start, cue.end, width=width, height=height)
        lines.append(f"Dialogue: 1,{_ass_time(cue.start)},{_ass_time(cue.end)},{style_name},,0,0,0,,{override}{_ass_text(cue.text)}")
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
        "subtitle_cues": [{"start": item.start, "end": item.end, "text": item.text, "style": item.style or {}} for item in subtitle_cues],
        "title_cues": [{"start": item.start, "end": item.end, "text": item.text, "style": item.style or {}} for item in title_cues],
        "title_clips": [
            {
                "id": str(item.get("id") or ""),
                "start": float(item.get("start") or 0.0),
                "end": float(item.get("end") or 0.0),
                "text": str(item.get("text") or ""),
                "enabled": bool(item.get("enabled", False)),
                "use_for_cover": bool(item.get("use_for_cover", False)),
                "style_override": item.get("style_override") if isinstance(item.get("style_override"), dict) else {},
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
                segments.append({"start": float(start), "end": float(end), "text": text, "tokens": row.get("tokens") or []})
        if segments:
            return segments
    return []


def _load_transcript_analysis(job_dir: Path, item_id: str) -> dict[str, Any]:
    path = job_dir / "work" / item_id / "transcript_analysis.json"
    if not path.exists():
        return {"status": "skipped", "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": []}
    try:
        data = read_json_file(path)
    except RuntimeError:
        return {"status": "failed", "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": []}
    return data if isinstance(data, dict) else {"status": "failed", "corrections": [], "break_hints": [], "allowed_breaks": [], "forbidden_breaks": [], "protected_spans": [], "repeat_candidates": []}


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


def _recent_jobs(
    limit: int | None = 8,
    status: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    for job_file in JOBS_DIR.glob("*/job.json"):
        try:
            jobs.append(_load_json_file(job_file))
        except RuntimeError:
            continue
    jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    if status:
        jobs = [item for item in jobs if item.get("status") == status]
    needle = query.strip().casefold()
    if needle:
        jobs = [
            item
            for item in jobs
            if needle in str(item.get("id") or "").casefold()
            or needle in str(item.get("params", {}).get("title") or "").casefold()
        ]
    return jobs if limit is None else jobs[: max(0, limit)]


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


def _friendly_job_error(value: Any) -> str:
    message = str(value or "").strip()
    lowered = message.casefold()
    if not message:
        return ""
    if "ffprobe" in lowered:
        return "无法读取视频文件。请确认文件能正常播放、格式受支持，然后重新处理。"
    if "ffmpeg" in lowered and ("not found" in lowered or "cannot" in lowered or "拒绝访问" in message):
        return "FFmpeg 运行失败。请到设置页检查视频运行环境后重新处理。"
    if "volc" in lowered or "openspeech" in lowered:
        return "语音识别服务调用失败。请检查火山配置和网络连接后重新处理。"
    first_line = message.splitlines()[0]
    return first_line if len(first_line) <= 180 else first_line[:177] + "..."


def _load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return read_json_file(SETTINGS_PATH)
    except RuntimeError:
        return {}


def _save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def _known_tool_dirs() -> list[Path]:
    seen: set[str] = set()
    dirs: list[Path] = []
    for directory in WINDOWS_TOOL_DIRS:
        if not str(directory):
            continue
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        dirs.append(resolved)
    return dirs


def _ensure_runtime_tool_path() -> None:
    current = os.environ.get("PATH", "")
    current_parts = {part.lower() for part in current.split(os.pathsep) if part}
    extras = [str(directory) for directory in _known_tool_dirs() if str(directory).lower() not in current_parts]
    if extras:
        os.environ["PATH"] = os.pathsep.join(extras + [current])


def _augment_tool_path(env: dict[str, str]) -> dict[str, str]:
    current = env.get("PATH", "")
    current_parts = {part.lower() for part in current.split(os.pathsep) if part}
    extras = [str(directory) for directory in _known_tool_dirs() if str(directory).lower() not in current_parts]
    if extras:
        env["PATH"] = os.pathsep.join(extras + [current])
    return env


def _probe_runtime_tool(name: str) -> tuple[str, str]:
    path = shutil.which(name) or ""
    if not path:
        return "", f"未在 PATH 中找到 {name}"
    try:
        result = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return path, f"{name} 无法执行：{exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip().splitlines()[0]
        return path, f"{name} 自检失败：{detail[:160]}"
    return path, ""


def _runtime_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or {}
    _ensure_runtime_tool_path()
    ffmpeg_path, ffmpeg_error = _probe_runtime_tool("ffmpeg")
    ffprobe_path, ffprobe_error = _probe_runtime_tool("ffprobe")
    volc_ready = bool(
        (os.environ.get("VOLC_APP_ID") and os.environ.get("VOLC_ACCESS_TOKEN"))
        or (settings.get("volc_app_id") and settings.get("volc_access_token"))
    )
    llm_ready = bool(
        settings.get("llm_enabled")
        and settings.get("llm_base_url")
        and settings.get("llm_model")
        and settings.get("llm_api_key")
    )
    return {
        "ffmpeg_ready": bool(ffmpeg_path and ffprobe_path and not ffmpeg_error and not ffprobe_error),
        "ffmpeg_path": ffmpeg_path or "",
        "ffprobe_path": ffprobe_path or "",
        "ffmpeg_error": ffmpeg_error or ffprobe_error,
        "volc_ready": volc_ready,
        "llm_ready": llm_ready,
    }


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
    llm_key = settings.get("llm_api_key", "")
    return {
        "volc_app_id": settings.get("volc_app_id", ""),
        "has_volc_access_token": bool(token),
        "subtitle_delay": settings.get("subtitle_delay", 0.0),
        "detect_disfluency": bool(settings.get("detect_disfluency", False)),
        "llm_enabled": bool(settings.get("llm_enabled", False)),
        "llm_base_url": settings.get("llm_base_url", ""),
        "llm_model": settings.get("llm_model", ""),
        "has_llm_api_key": bool(llm_key),
    }


def _preset_from_form(form: Any, preset_id: str) -> dict[str, Any]:
    subtitle = _style_from_form(form)
    video_title = _cover_style_from_form(form)
    cover_title = _cover_style_from_form(form)
    subtitle_json = _form_json(form, "subtitle_style_json")
    video_json = _form_json(form, "video_style_json")
    cover_json = _form_json(form, "cover_style_json")
    if subtitle_json:
        subtitle = _normalize_subtitle_style(subtitle_json)
    if cover_json:
        cover_title = _normalize_cover_style(cover_json)
    if video_json:
        video_title = _normalize_cover_style(video_json)
    return {
        "id": preset_id,
        "name": _form_str(form, "name", preset_id).strip() or preset_id,
        "subtitle": subtitle,
        "video_title": video_title,
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
    static_style = {**style, "animation_in": "none", "animation_out": "none"}
    override = subtitle_override(static_style, 0, 3, width=width, height=height)
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
            subtitle_to_ass_style(static_style, width=width, height=height),
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
    if active in {"subtitle", "video", "cover"}:
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
