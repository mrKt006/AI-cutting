from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from errno import EACCES, EBUSY
from pathlib import Path
from typing import Any


def loads_json(text: str, source: str, *, repair_backslashes: bool = True) -> Any:
    text = text.lstrip("\ufeff")
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


def write_text_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    retries: int = 8,
    initial_delay: float = 0.025,
) -> None:
    """Replace a text file safely, retrying transient Windows file locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        for attempt in range(max(1, retries)):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                transient = (
                    isinstance(exc, PermissionError)
                    or exc.errno in {EACCES, EBUSY}
                    or getattr(exc, "winerror", None) in {5, 32}
                )
                if not transient or attempt + 1 >= max(1, retries):
                    raise
                time.sleep(initial_delay * (2**attempt))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json_file(
    path: Path,
    value: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 2,
    retries: int = 8,
    initial_delay: float = 0.025,
) -> None:
    write_text_atomic(
        path,
        json.dumps(value, ensure_ascii=ensure_ascii, indent=indent),
        retries=retries,
        initial_delay=initial_delay,
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
