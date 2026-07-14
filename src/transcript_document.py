from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_TRANSCRIPT_SUFFIXES = {".md", ".txt"}
PLACEHOLDERS = {"", "待确认", "暂无", "待补充", "未填写", "todo", "tbd"}
SECTION_ALIASES = {
    "内容标题": "content_title",
    "封面标题": "cover_title",
    "逐字稿": "transcript",
    "最终文案": "final_script",
    "原始文案": "original_script",
}


class TranscriptDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedTranscriptDocument:
    content_title: str
    cover_title: str
    transcript: str
    transcript_source: str
    warnings: tuple[str, ...]


def parse_transcript_document(text: str, suffix: str, filename: str = "") -> ParsedTranscriptDocument:
    normalized_suffix = suffix.lower()
    if normalized_suffix not in SUPPORTED_TRANSCRIPT_SUFFIXES:
        raise TranscriptDocumentError("逐字稿只支持 .md 或 .txt 文件")
    content = str(text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not content.strip():
        raise TranscriptDocumentError("逐字稿文件为空")

    if normalized_suffix == ".md":
        document_title, sections, unknown = _parse_markdown_sections(content)
    else:
        document_title, sections, unknown = _parse_text_sections(content)

    values = {key: _single_section_value(sections, key) for key in SECTION_ALIASES.values()}
    transcript_source = ""
    transcript = ""
    for key, label in (("transcript", "逐字稿"), ("final_script", "最终文案"), ("original_script", "原始文案")):
        candidate = _clean_body(values.get(key, ""))
        if _is_placeholder(candidate):
            continue
        transcript = candidate
        transcript_source = label
        break
    if not transcript:
        raise TranscriptDocumentError("没有找到有效逐字稿；请填写逐字稿、最终文案或原始文案板块")

    content_title = _clean_title(values.get("content_title", ""))
    if not content_title:
        content_title = _clean_document_title(document_title) or _filename_title(filename)
    cover_title = _clean_title(values.get("cover_title", ""))

    warnings = [f"已忽略章节：{name}" for name in unknown]
    if not values.get("content_title") and content_title:
        warnings.append("未填写内容标题，已使用文档标题或文件名")
    if not cover_title:
        warnings.append("未填写封面标题，将根据内容标题生成建议")
    return ParsedTranscriptDocument(
        content_title=content_title,
        cover_title=cover_title,
        transcript=transcript,
        transcript_source=transcript_source,
        warnings=tuple(warnings),
    )


def _parse_markdown_sections(content: str) -> tuple[str, dict[str, list[str]], list[str]]:
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    document_title = ""
    sections: dict[str, list[str]] = {}
    unknown: list[str] = []
    current_key = ""
    current_name = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_name, current_lines
        if not current_name:
            return
        value = "\n".join(current_lines).strip()
        if current_key:
            sections.setdefault(current_key, []).append(value)
        elif value and current_name not in unknown:
            unknown.append(current_name)
        current_key = ""
        current_name = ""
        current_lines = []

    for line in content.splitlines():
        match = heading_pattern.match(line)
        if not match:
            if current_name:
                current_lines.append(line)
            continue
        flush()
        level = len(match.group(1))
        name = match.group(2).strip()
        if level == 1 and not document_title:
            document_title = name
            continue
        current_name = name
        current_key = SECTION_ALIASES.get(name, "")
    flush()
    return document_title, sections, unknown


def _parse_text_sections(content: str) -> tuple[str, dict[str, list[str]], list[str]]:
    marker_pattern = re.compile(r"^【(.+?)】\s*$")
    sections: dict[str, list[str]] = {}
    unknown: list[str] = []
    current_key = ""
    current_name = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_name, current_lines
        if not current_name:
            return
        value = "\n".join(current_lines).strip()
        if current_key:
            sections.setdefault(current_key, []).append(value)
        elif value and current_name not in unknown:
            unknown.append(current_name)
        current_key = ""
        current_name = ""
        current_lines = []

    for line in content.splitlines():
        match = marker_pattern.match(line.strip())
        if not match:
            if current_name:
                current_lines.append(line)
            continue
        flush()
        current_name = match.group(1).strip()
        current_key = SECTION_ALIASES.get(current_name, "")
    flush()
    return "", sections, unknown


def _single_section_value(sections: dict[str, list[str]], key: str) -> str:
    values = [value for value in sections.get(key, []) if not _is_placeholder(value)]
    if len(values) > 1:
        label = next((name for name, alias in SECTION_ALIASES.items() if alias == key), key)
        raise TranscriptDocumentError(f"检测到多个有效的“{label}”板块，请只保留一个")
    return values[0] if values else ""


def _clean_title(value: str) -> str:
    return "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())


def _clean_body(value: str) -> str:
    lines = [line.rstrip() for line in str(value or "").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        result.append(line)
        previous_blank = blank
    return "\n".join(result).strip()


def _is_placeholder(value: str) -> bool:
    normalized = re.sub(r"[\[\]【】()（）\s]", "", str(value or "")).lower()
    return normalized in PLACEHOLDERS


def _clean_document_title(value: str) -> str:
    title = re.sub(r"^\s*\d+(?:[.、_-]|\s)+\s*", "", str(value or "")).strip()
    return title


def _filename_title(filename: str) -> str:
    stem = Path(filename or "").stem
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[_-]*", "", stem)
    stem = re.sub(r"^\d+(?:[._、-]|\s)+", "", stem)
    return stem.strip(" _-")
