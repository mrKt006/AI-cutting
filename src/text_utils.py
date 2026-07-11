from __future__ import annotations

import re


MARK_RE = re.compile(r"\*\*(.+?)\*\*")


def strip_keyword_marks(text: str) -> str:
    return MARK_RE.sub(r"\1", text)


def read_text(path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="ignore")


def normalize_script(text: str) -> str:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def split_script(text: str, target_len: int = 12, max_len: int = 18) -> list[str]:
    text = normalize_script(text)
    if not text:
        return []

    pieces: list[str] = []
    for line in text.splitlines():
        parts = re.split(r"(?<=[。！？!?；;，,、])", line)
        for part in parts:
            part = part.strip()
            if part:
                pieces.extend(_split_piece(part, target_len, max_len))
    return [piece for piece in pieces if strip_keyword_marks(piece).strip()]


def _split_piece(piece: str, target_len: int, max_len: int) -> list[str]:
    plain = strip_keyword_marks(piece)
    if len(plain) <= max_len:
        return [piece]

    result: list[str] = []
    current = ""
    visible = 0
    tokens = _tokenize_keep_words(piece)
    for index, token in enumerate(tokens):
        token_visible = len(strip_keyword_marks(token))
        if current and visible + token_visible > max_len and _is_safe_break_before(token):
            result.append(current.strip())
            current = ""
            visible = 0

        current += token
        visible += token_visible

        next_token = tokens[index + 1] if index < len(tokens) - 1 else ""
        if visible >= target_len and next_token and _is_safe_break_before(next_token):
            result.append(current.strip())
            current = ""
            visible = 0

    if current.strip():
        result.append(current.strip())
    return result


def _tokenize_keep_words(text: str) -> list[str]:
    pattern = re.compile(r"\*\*.+?\*\*|[A-Za-z0-9][A-Za-z0-9_+\-./#]*|\s+|.")
    return pattern.findall(text)


def _is_safe_break_before(token: str) -> bool:
    if not token:
        return True
    return not bool(re.match(r"[A-Za-z0-9_+\-./#]", token))
