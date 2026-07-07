from pathlib import Path
import re
import sys


SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "jobs",
    "input",
    "output",
    "models",
    "venv",
    ".venv",
}

TEXT_EXTS = {
    ".py",
    ".html",
    ".css",
    ".js",
    ".md",
    ".json",
    ".txt",
    ".ass",
    ".srt",
    ".bat",
    ".command",
}

SUSPICIOUS_PATTERNS = {
    "repeated question marks": re.compile(r"\?{3,}"),
    "replacement character": re.compile("\ufffd"),
    "common mojibake": re.compile(
        r"(Ã|Â|â€|â€™|â€œ|â€�|鏂|浠|澶|杈|绗|鎶|鍙|棣|鐏|婵|鈱|€)"
    ),
}

IGNORED_LINE_PATTERNS = [
    re.compile(r"\?\?"),  # JavaScript nullish coalescing is checked below.
]


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTS or path.name in {"README", ".gitignore"}


def is_skipped(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & SKIP_DIRS) or path.as_posix().startswith("web/static/style_previews/")


def should_ignore_match(line: str, label: str) -> bool:
    if label == "repeated question marks":
        return False
    if label == "common mojibake" and "??" in line and "value ??" in line:
        return True
    return False


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

    root = Path(".")
    self_path = Path(__file__).resolve()
    findings: list[tuple[str, str, int, str]] = []

    for path in root.rglob("*"):
        if not path.is_file() or is_skipped(path) or not is_text_file(path):
            continue
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            findings.append((path.as_posix(), "UTF-8 decode error", 0, str(exc)))
            continue

        for line_no, line in enumerate(text.splitlines(), 1):
            for label, pattern in SUSPICIOUS_PATTERNS.items():
                if pattern.search(line) and not should_ignore_match(line, label):
                    findings.append((path.as_posix(), label, line_no, line.strip()))

    for file_name, label, line_no, line in findings:
        location = f"{file_name}:{line_no}" if line_no else file_name
        print(f"{location}: {label}: {line}")

    if findings:
        print(f"\nFound {len(findings)} suspicious encoding issue(s).")
        return 1

    print("Encoding scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
