from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from style_presets import DEFAULT_STYLE_PRESETS, hex_to_rgba


def make_cover(video: Path, title: str, output: Path, style: dict | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(frame)],
            text=True,
            capture_output=True,
            check=True,
        )
        try:
            _draw_title(frame, title, output, style=style)
        except Exception:
            shutil.copyfile(frame, output)


def _draw_title(frame: Path, title: str, output: Path, style: dict | None = None) -> None:
    from PIL import Image, ImageDraw, ImageFont

    style = style or DEFAULT_STYLE_PRESETS[0]["cover_title"]
    image = Image.open(frame).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    scale = float(style.get("scale", 100)) / 100
    font_size = max(24, round(float(style.get("font_size", 76)) * scale * height / 1920))
    font = _find_font(font_size, str(style.get("font_family", "")), bool(style.get("bold", True)))
    stroke_width = 0
    if style.get("outline_enabled", True):
        stroke_width = max(0, round(float(style.get("outline_width", 4)) * height / 1920))
    lines = _wrap_title(title, font, draw, int(width * 0.82))
    line_gap = max(10, round(height * 0.012))
    boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width) for line in lines]
    total_h = sum(box[3] - box[1] for box in boxes) + line_gap * max(0, len(lines) - 1)
    max_text_w = max((box[2] - box[0] for box in boxes), default=0)
    pad = max(0, round(float(style.get("background_padding", 26)) * height / 1920))
    if "position_x" in style or "position_y" in style:
        x = round(width / 2 + float(style.get("position_x", 0)) * width / 1080 - max_text_w / 2)
        y = round(height / 2 + float(style.get("position_y", -520)) * height / 1920 - total_h / 2)
        x, y = max(pad, x), max(pad, y)
    else:
        x, y = _position_box(
            int(style.get("alignment", 8)),
            width,
            height,
            max_text_w,
            total_h,
            round(float(style.get("margin_x", 80)) * width / 1080),
            round(float(style.get("margin_y", 260)) * height / 1920),
            pad,
        )

    if style.get("background_enabled", True):
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            (x - pad, y - pad, x + max_text_w + pad, y + total_h + pad),
            radius=max(14, round(height * 0.012)),
            fill=hex_to_rgba(
                str(style.get("background_color", "#000000")),
                int(style.get("background_opacity", 52)),
            ),
        )
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(image)

    cursor = y
    for line, box in zip(lines, boxes):
        text_w = box[2] - box[0]
        text_h = box[3] - box[1]
        text_x = x + (max_text_w - text_w) / 2
        align = str(style.get("text_align", "center"))
        if align == "left":
            text_x = x
        elif align == "right":
            text_x = x + max_text_w - text_w
        shadow_offset = 0
        if style.get("shadow_enabled", bool(style.get("shadow_offset", 0))):
            shadow_offset = max(0, round(float(style.get("shadow_offset", 0)) * height / 1920))
        if shadow_offset:
            draw.text(
                (text_x + shadow_offset, cursor + shadow_offset),
                line,
                font=font,
                fill=_rgb(str(style.get("shadow_color", "#000000"))),
            )
        _draw_text_with_opacity(
            image,
            (text_x, cursor),
            line,
            font=font,
            fill=_rgb(str(style.get("primary_color", "#fff446"))),
            opacity=int(style.get("opacity", 100)),
            stroke_width=stroke_width,
            stroke_fill=_rgb(str(style.get("outline_color", "#000000"))),
        )
        cursor += text_h + line_gap
    image.save(output, quality=94)


def _draw_text_with_opacity(image, position, text: str, *, font, fill, opacity: int, stroke_width: int, stroke_fill) -> None:
    from PIL import Image, ImageDraw

    opacity = max(0, min(100, opacity))
    if opacity >= 100:
        ImageDraw.Draw(image).text(
            position,
            text,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        return

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    alpha = round(255 * opacity / 100)
    overlay_draw.text(
        position,
        text,
        font=font,
        fill=(*fill, alpha),
        stroke_width=stroke_width,
        stroke_fill=(*stroke_fill, alpha),
    )
    composited = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    image.paste(composited)


def _find_font(size: int, family: str = "", bold: bool = True):
    from PIL import ImageFont

    family = family.lower()
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf" if "hei" in family else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc" if "song" in family else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def _wrap_title(title: str, font, draw, max_width: int) -> list[str]:
    text = title.strip() or "?????"
    tokens = re.findall(r"[A-Za-z0-9_-]+|\s+|.", text)
    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = current + token
        if current and (_visual_len(candidate) > 10 or draw.textlength(candidate, font=font) > max_width):
            lines.append(current.strip())
            current = token.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.strip())
    return lines[:3]


def _visual_len(text: str) -> int:
    length = 0.0
    for char in text:
        if char.isspace():
            continue
        length += 1.0 if ord(char) > 127 else 0.5
    return round(length)

def _position_box(
    alignment: int,
    width: int,
    height: int,
    box_w: int,
    box_h: int,
    margin_x: int,
    margin_y: int,
    pad: int,
) -> tuple[int, int]:
    if alignment in {1, 4, 7}:
        x = margin_x + pad
    elif alignment in {3, 6, 9}:
        x = width - margin_x - pad - box_w
    else:
        x = (width - box_w) // 2
    if alignment in {7, 8, 9}:
        y = margin_y + pad
    elif alignment in {4, 5, 6}:
        y = (height - box_h) // 2
    else:
        y = height - margin_y - pad - box_h
    return max(pad, int(x)), max(pad, int(y))


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        value = "ffffff"
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
