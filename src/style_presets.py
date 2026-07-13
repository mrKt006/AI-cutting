from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from safe_json import read_json_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRESETS_PATH = ROOT / "web" / "style_presets.local.json"


DEFAULT_STYLE_PRESETS: list[dict[str, Any]] = [
    {
        "id": "default-white",
        "name": "默认口播白字",
        "subtitle": {
            "font_family": "Microsoft YaHei",
            "font_size": 64,
            "bold": True,
            "italic": False,
            "underline": False,
            "primary_color": "#ffffff",
            "opacity": 100,
            "outline_color": "#000000",
            "outline_enabled": True,
            "shadow_color": "#000000",
            "shadow_enabled": False,
            "outline_width": 4,
            "shadow_offset": 0,
            "blur": 0,
            "letter_spacing": 0,
            "line_spacing": 0,
            "scale": 100,
            "uniform_scale": True,
            "scale_x": 100,
            "scale_y": 100,
            "position_x": 0,
            "position_y": 650,
            "rotation": 0,
            "text_align": "center",
            "background_enabled": False,
            "background_color": "#000000",
            "background_opacity": 52,
            "background_padding": 18,
            "glow_enabled": False,
            "glow_color": "#ffffff",
            "glow_strength": 0,
            "alignment": 2,
            "margin_x": 80,
            "margin_y": 170,
            "target_len": 12,
            "max_len": 18,
            "animation_in": "none",
            "animation_out": "none",
        },
        "cover_title": {
            "font_family": "Microsoft YaHei",
            "font_size": 76,
            "bold": True,
            "primary_color": "#fff446",
            "outline_color": "#000000",
            "shadow_color": "#000000",
            "outline_width": 4,
            "shadow_offset": 0,
            "background_enabled": True,
            "background_color": "#000000",
            "background_opacity": 52,
            "background_padding": 26,
            "alignment": 8,
            "margin_x": 80,
            "margin_y": 260,
        },
    },
    {
        "id": "yellow-bold",
        "name": "黄字黑边重点款",
        "subtitle": {
            "font_family": "Microsoft YaHei",
            "font_size": 68,
            "bold": True,
            "italic": False,
            "underline": False,
            "primary_color": "#fff446",
            "opacity": 100,
            "outline_color": "#000000",
            "outline_enabled": True,
            "shadow_color": "#000000",
            "shadow_enabled": True,
            "outline_width": 5,
            "shadow_offset": 1,
            "blur": 0,
            "letter_spacing": 0,
            "line_spacing": 0,
            "scale": 100,
            "uniform_scale": True,
            "scale_x": 100,
            "scale_y": 100,
            "position_x": 0,
            "position_y": 650,
            "rotation": 0,
            "text_align": "center",
            "background_enabled": False,
            "background_color": "#000000",
            "background_opacity": 45,
            "background_padding": 18,
            "glow_enabled": False,
            "glow_color": "#fff446",
            "glow_strength": 0,
            "alignment": 2,
            "margin_x": 80,
            "margin_y": 180,
            "target_len": 10,
            "max_len": 16,
            "animation_in": "fade",
            "animation_out": "fade",
        },
        "cover_title": {
            "font_family": "Microsoft YaHei",
            "font_size": 82,
            "bold": True,
            "primary_color": "#fff446",
            "outline_color": "#000000",
            "shadow_color": "#000000",
            "outline_width": 5,
            "shadow_offset": 1,
            "background_enabled": True,
            "background_color": "#000000",
            "background_opacity": 56,
            "background_padding": 28,
            "alignment": 8,
            "margin_x": 72,
            "margin_y": 250,
        },
    },
    {
        "id": "bottom-box",
        "name": "底部半透明背景款",
        "subtitle": {
            "font_family": "Microsoft YaHei",
            "font_size": 58,
            "bold": True,
            "italic": False,
            "underline": False,
            "primary_color": "#ffffff",
            "opacity": 100,
            "outline_color": "#111111",
            "outline_enabled": True,
            "shadow_color": "#000000",
            "shadow_enabled": False,
            "outline_width": 2,
            "shadow_offset": 0,
            "blur": 0,
            "letter_spacing": 0,
            "line_spacing": 0,
            "scale": 100,
            "uniform_scale": True,
            "scale_x": 100,
            "scale_y": 100,
            "position_x": 0,
            "position_y": 680,
            "rotation": 0,
            "text_align": "center",
            "background_enabled": True,
            "background_color": "#000000",
            "background_opacity": 58,
            "background_padding": 22,
            "glow_enabled": False,
            "glow_color": "#ffffff",
            "glow_strength": 0,
            "alignment": 2,
            "margin_x": 70,
            "margin_y": 150,
            "target_len": 13,
            "max_len": 20,
            "animation_in": "slide_up",
            "animation_out": "fade",
        },
        "cover_title": {
            "font_family": "Microsoft YaHei",
            "font_size": 70,
            "bold": True,
            "primary_color": "#ffffff",
            "outline_color": "#000000",
            "shadow_color": "#000000",
            "outline_width": 3,
            "shadow_offset": 0,
            "background_enabled": True,
            "background_color": "#000000",
            "background_opacity": 58,
            "background_padding": 30,
            "alignment": 8,
            "margin_x": 80,
            "margin_y": 270,
        },
    },
]


def load_style_presets(path: str | Path | None = None) -> list[dict[str, Any]]:
    presets_path = Path(path) if path else DEFAULT_PRESETS_PATH
    if not presets_path.exists():
        return deepcopy(DEFAULT_STYLE_PRESETS)
    data = read_json_file(presets_path)
    if isinstance(data, dict):
        data = data.get("presets", [])
    if not isinstance(data, list):
        return deepcopy(DEFAULT_STYLE_PRESETS)
    presets = [merge_style_preset(item) for item in data if isinstance(item, dict)]
    return presets or deepcopy(DEFAULT_STYLE_PRESETS)


def save_style_presets(presets: list[dict[str, Any]], path: str | Path | None = None) -> None:
    presets_path = Path(path) if path else DEFAULT_PRESETS_PATH
    presets_path.parent.mkdir(parents=True, exist_ok=True)
    clean_presets = [merge_style_preset(item) for item in presets]
    presets_path.write_text(
        json.dumps({"presets": clean_presets}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_style_preset(preset_id: str | None, path: str | Path | None = None) -> dict[str, Any]:
    presets = load_style_presets(path)
    if preset_id:
        for preset in presets:
            if preset.get("id") == preset_id:
                return merge_style_preset(preset)
    return merge_style_preset(presets[0])


def merge_style_preset(preset: dict[str, Any]) -> dict[str, Any]:
    base = deepcopy(DEFAULT_STYLE_PRESETS[0])
    base["video_title"] = _default_video_title_style()
    base.update({k: v for k, v in preset.items() if k not in {"subtitle", "video_title", "cover_title"}})
    base["subtitle"].update(preset.get("subtitle") or {})
    base["video_title"].update(preset.get("video_title") or {})
    base["cover_title"].update(preset.get("cover_title") or {})
    base["id"] = _slug(str(base.get("id") or base.get("name") or "style"))
    base["name"] = str(base.get("name") or base["id"])
    return base


def _default_video_title_style() -> dict[str, Any]:
    style = deepcopy(DEFAULT_STYLE_PRESETS[0]["subtitle"])
    style.update(
        {
            "enabled": False,
            "display_mode": "full",
            "display_duration": 3.0,
            "font_size": 64,
            "position_x": 0,
            "position_y": -620,
            "target_len": 12,
            "max_len": 20,
            "animation_in": "fade",
            "animation_out": "fade",
        }
    )
    return style


def hex_to_ass(color: str, alpha_percent: int = 0) -> str:
    value = (color or "#ffffff").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        value = "ffffff"
    r, g, b = value[0:2], value[2:4], value[4:6]
    alpha_percent = max(0, min(100, int(alpha_percent)))
    alpha = round(255 * alpha_percent / 100)
    return f"&H{alpha:02X}{b}{g}{r}&"


def hex_to_rgba(color: str, opacity_percent: int = 100) -> tuple[int, int, int, int]:
    value = (color or "#000000").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        value = "000000"
    opacity_percent = max(0, min(100, int(opacity_percent)))
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        round(255 * opacity_percent / 100),
    )


def subtitle_to_ass_style(style: dict[str, Any], width: int, height: int) -> str:
    font_size = _scaled(style.get("font_size", 64), height, 1920)
    outline = _scaled(style.get("outline_width", 4) if style.get("outline_enabled", True) else 0, height, 1920)
    if style.get("glow_enabled") and float(style.get("glow_strength", 0)) > 0:
        outline = max(outline, _scaled(style.get("glow_strength", 0), height, 1920))
    shadow = _scaled(style.get("shadow_offset", 0) if style.get("shadow_enabled", False) else 0, height, 1920)
    margin_x = _scaled(style.get("margin_x", 80), width, 1080)
    margin_y = _scaled(style.get("margin_y", 170), height, 1920)
    border_style = 3 if style.get("background_enabled") else 1
    back_alpha = 100 - int(style.get("background_opacity", 52))
    scale = float(style.get("scale", 100))
    scale_x = scale if style.get("uniform_scale", True) else float(style.get("scale_x", 100))
    scale_y = scale if style.get("uniform_scale", True) else float(style.get("scale_y", 100))
    spacing = float(style.get("letter_spacing", 0))
    opacity_alpha = 100 - int(style.get("opacity", 100))
    outline_color = style.get("glow_color") if style.get("glow_enabled") else style.get("outline_color", "#000000")
    return (
        f"Style: Default,{style.get('font_family', 'Microsoft YaHei')},{font_size},"
        f"{hex_to_ass(style.get('primary_color', '#ffffff'), alpha_percent=opacity_alpha)},&H0000FFFF,"
        f"{hex_to_ass(outline_color or '#000000')},"
        f"{hex_to_ass(style.get('background_color', '#000000'), alpha_percent=back_alpha)},"
        f"{_ass_bool(style.get('bold', True))},{_ass_bool(style.get('italic', False))},"
        f"{_ass_bool(style.get('underline', False))},0,{scale_x:g},{scale_y:g},{spacing:g},"
        f"{float(style.get('rotation', 0)):g},{border_style},"
        f"{outline},{shadow},{int(style.get('alignment', 2))},{margin_x},{margin_x},{margin_y},1"
    )


def subtitle_override(style: dict[str, Any], start: float, end: float, width: int, height: int) -> str:
    tags: list[str] = []
    animation_in = style.get("animation_in", "none")
    animation_out = style.get("animation_out", "none")
    anchor = _text_anchor(style)
    if style.get("no_wrap", True):
        tags.append(r"\q2")
    if animation_in == "fade" or animation_out in {"fade", "pop"}:
        tags.append(r"\fad(180,180)")
    if animation_in == "slide_up":
        x, y = _xy_position(style, width, height)
        tags.append(rf"\an{anchor}\move({x},{y + max(24, round(height * 0.025))},{x},{y},0,220)")
    else:
        x, y = _xy_position(style, width, height)
        tags.append(rf"\an{anchor}\pos({x},{y})")
    if int(style.get("blur", 0)) > 0:
        tags.append(rf"\blur{int(style.get('blur', 0))}")
    if float(style.get("line_spacing", 0)) != 0 and not any(tag.startswith(r"\q") for tag in tags):
        tags.append(rf"\q2")
    if not tags:
        return ""
    return "{" + "".join(tags) + "}"


def _xy_position(style: dict[str, Any], width: int, height: int) -> tuple[int, int]:
    x = width / 2 + float(style.get("position_x", 0)) * width / 1080
    y = height / 2 + float(style.get("position_y", 650)) * height / 1920
    return round(x), round(y)


def _text_anchor(style: dict[str, Any]) -> int:
    text_align = str(style.get("text_align", "center"))
    if text_align == "left":
        return 4
    if text_align == "right":
        return 6
    return 5


def _alignment_position(style: dict[str, Any], width: int, height: int) -> tuple[int, int]:
    alignment = int(style.get("alignment", 2))
    margin_x = _scaled(style.get("margin_x", 80), width, 1080)
    margin_y = _scaled(style.get("margin_y", 170), height, 1920)
    x = width // 2
    y = height - margin_y
    if alignment in {1, 4, 7}:
        x = margin_x
    elif alignment in {3, 6, 9}:
        x = width - margin_x
    if alignment in {7, 8, 9}:
        y = margin_y
    elif alignment in {4, 5, 6}:
        y = height // 2
    return x, y


def _scaled(value: Any, target: int, base: int) -> int:
    return max(0, round(float(value) * target / base))


def _ass_bool(value: Any) -> int:
    return -1 if bool(value) else 0


def _slug(value: str) -> str:
    allowed = []
    for char in value.lower().replace(" ", "-"):
        if char.isalnum() or char in {"-", "_"}:
            allowed.append(char)
    return "".join(allowed).strip("-") or "style"
