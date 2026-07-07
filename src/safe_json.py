from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def loads_json(text: str, source: str, *, repair_backslashes: bool = True) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if repair_backslashes:
            repaired = escape_invalid_json_backslashes(text)
            if repaired != text:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
        raise RuntimeError(_json_error_message(source, text, exc)) from exc


def read_json_file(path: Path, *, repair_backslashes: bool = True) -> Any:
    return loads_json(
        path.read_text(encoding="utf-8"),
        str(path),
        repair_backslashes=repair_backslashes,
    )


def escape_invalid_json_backslashes(text: str) -> str:
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)


def _json_error_message(source: str, text: str, exc: json.JSONDecodeError) -> str:
    preview = text[max(0, exc.pos - 90) : exc.pos + 90]
    preview = preview.replace("\r", "\\r").replace("\n", "\\n")
    return (
        f"Invalid JSON from {source}: {exc.msg} "
        f"at line {exc.lineno} column {exc.colno} char {exc.pos}. "
        f"Near: {preview}"
    )
